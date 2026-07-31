"""Identity-photograph framing and conformity control (ANTS / ICAO rules).

The geometry follows the French 35 × 45 mm print: the head measures 32 to 36 mm from
chin to crown, which is 0.71 to 0.80 of the frame height, and the eyes sit in the upper
third.  Everything here is classical computer vision — landmarks, morphology, statistics
— so the service stays free of paid inference APIs.

Landmarks come from MediaPipe Face Mesh when it is installed.  When it is not, the
module degrades to the Haar cascades bundled with OpenCV, which still give a face box
and open-eye detection but cannot judge the mouth: those criteria are then reported as
`unknown` rather than silently passed, which is exactly what the human review panel is
there to settle.
"""
from __future__ import annotations

import argparse
import math
import sys
import threading
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np

if __package__ in (None, ""):  # allows `python service/processing/photo_processor.py`
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
    from service.models import FAIL, PASS, UNKNOWN, Check, ProcessedImage
    from service.processing import imaging
else:
    from ..models import FAIL, PASS, UNKNOWN, Check, ProcessedImage
    from . import imaging

# ── Conformity constants ────────────────────────────────────────────────────────────
OUT_WIDTH, OUT_HEIGHT = 414, 532  # ANTS minimum, i.e. 35 × 45 mm at ~300 dpi
PHOTO_RATIO = OUT_WIDTH / OUT_HEIGHT
RATIO_TOLERANCE = 0.02
MAX_BYTES = 2_000_000
# 32–36 mm of head on a 45 mm frame, with the crop aimed at the middle of the band.
HEAD_RATIO_MIN, HEAD_RATIO_MAX, HEAD_RATIO_TARGET = 0.70, 0.80, 0.75
# Space kept above the crown: ~3.5 mm of the 45 mm print. The crop is anchored here
# rather than on the eye line, because anchoring on the eyes while also sizing on the
# head ratio leaves no margin at all above the skull and clips it on real photographs.
TOP_MARGIN_TARGET = 0.08
# Eye line measured from the top of the frame: 3.5 mm of margin plus roughly 45 % of a
# 34 mm head puts it near 0.42. Checked afterwards, never used to position the crop.
EYE_LINE_MIN, EYE_LINE_MAX = 0.33, 0.50
TILT_MAX_DEG = 5.0
# The crown is above the highest face landmark; this share of the face height is the
# usual anthropometric approximation for the skull, and the reviewer confirms it.
CROWN_EXTENSION = 0.28
# The eye line sits at this share of the chin-to-crown distance, counted from the chin.
# The cascade fallback derives its crown from it: the chin edge of the box and the eyes
# are the only two points those cascades actually give, so anchoring the skull on them
# keeps the fallback geometry internally consistent.
EYE_TO_CHIN_RATIO = 0.53
# How far above the anthropometric guess refine_crown() may travel, as a share of the
# estimated head height. The scan follows the outline of the *hair*, which on a voluminous
# cut stands well above the skull ANTS actually measures; the reach upwards is therefore
# bounded, while the correction downwards — an over-generous guess — stays free.
CROWN_REFINE_LIMIT = 0.10
EYE_ASPECT_OPEN = 0.19  # eye aspect ratio below this reads as a closed or blinking eye
MOUTH_GAP_MAX = 0.15  # inner lip gap over mouth width
# Calibrated on the sample photographs: a studio grey with a soft vignette measures ~23,
# a textured wall or a bedsheet 30 to 58. Revisit both figures as the sample grows.
BACKGROUND_STD_MAX, BACKGROUND_MIN_LEVEL = 25.0, 130.0
# "Le fond doit être uni, de couleur claire (bleu clair ou gris clair par exemple). Le
# fond blanc est interdit." — service-public.fr F10619. A background brighter than this
# reads as that forbidden white, so it is refused at the top of the band as well as at
# the bottom.
BACKGROUND_MAX_LEVEL = 245.0
SHARPNESS_MIN = 45.0
# "Elle ne doit être ni surexposée ni sous-exposée [...] correctement contrastée"
# (service-public.fr F10619). Graded on clipping and contrast rather than on absolute skin
# brightness: complexion legitimately spans the scale, and an absolute threshold refuses
# dark skin for being dark. Every correctly lit sample sits at 78–124 median, under 0.6 %
# clipped at either end, and 128–180 of spread.
EXPOSURE_CLIP_MAX = 2.0  # per cent of face pixels pinned at either end of the scale
EXPOSURE_SPREAD_MIN = 60.0  # p95 − p5 across the face
EXPOSURE_MEDIAN_MIN, EXPOSURE_MEDIAN_MAX = 40.0, 225.0  # catastrophes only
# Share of the crop that may be invented when the source is framed too tightly. Kept
# low: replicated pixels stay visible, and an ANTS agent reads them as a background flaw.
PADDING_LIMIT = 0.02
# BGR. A light blue-grey, which is one of the two shades service-public.fr names outright
# ("bleu clair ou gris clair"). Deliberately not the near-white 242 grey this used to be:
# that reads as the white background the same page forbids.
UNIFORM_BACKGROUND = (227, 217, 206)
# The segmentation alpha is soft over several pixels. Steepening it and then feathering it
# back by roughly a pixel gives a cut edge that neither haloes nor looks scissored.
EDGE_SHARPNESS, EDGE_FEATHER = 6.0, 1.2
# Share of the face box the cut must keep: below this the segmentation ate the subject,
# and the original photograph is preferable to a mutilated one.
SUBJECT_INTACT_MIN = 0.90

# MediaPipe Face Mesh indices (468-point topology).
CHIN, FOREHEAD = 152, 10
LEFT_EYE = (33, 160, 158, 133, 153, 144)
RIGHT_EYE = (362, 385, 387, 263, 373, 380)
MOUTH_INNER = (13, 14)
MOUTH_CORNERS = (61, 291)

_mesh_lock = threading.Lock()
_mesh = None
_mesh_unavailable = False

_segmenter_lock = threading.Lock()
_segmenter = None
_segmenter_unavailable = False


@dataclass
class FaceGeometry:
    """Everything the crop and the checks need, in source-image pixels."""

    box: tuple[int, int, int, int]
    eye_left: tuple[float, float]
    eye_right: tuple[float, float]
    chin_y: float
    crown_y: float
    detector: str
    face_count: int = 1
    eyes_open: str = UNKNOWN
    mouth_closed: str = UNKNOWN
    measures: dict = field(default_factory=dict)

    @property
    def head_height(self) -> float:
        return max(self.chin_y - self.crown_y, 1.0)

    @property
    def eye_centre(self) -> tuple[float, float]:
        return (
            (self.eye_left[0] + self.eye_right[0]) / 2.0,
            (self.eye_left[1] + self.eye_right[1]) / 2.0,
        )

    @property
    def roll_degrees(self) -> float:
        dx = self.eye_right[0] - self.eye_left[0]
        dy = self.eye_right[1] - self.eye_left[1]
        return abs(math.degrees(math.atan2(dy, dx)))


@dataclass
class CropOverride:
    """Manual adjustment from the review panel, as fractions of the computed crop."""

    zoom: float = 1.0
    dx: float = 0.0
    dy: float = 0.0

    @property
    def active(self) -> bool:
        return abs(self.zoom - 1) > 1e-3 or abs(self.dx) > 1e-3 or abs(self.dy) > 1e-3


# ── Face detection ──────────────────────────────────────────────────────────────────
def _face_mesh():
    """One cached Face Mesh, or None when MediaPipe is not installed.

    The instance is stateful and not thread-safe, so callers hold `_mesh_lock`; building
    it costs a few hundred milliseconds, which is why it is not created per request.
    """
    global _mesh, _mesh_unavailable
    if _mesh is not None or _mesh_unavailable:
        return _mesh
    try:
        import mediapipe as mp  # noqa: PLC0415 - optional heavy dependency
    except Exception:  # ImportError, or a protobuf/ABI mismatch on exotic platforms
        _mesh_unavailable = True
        return None
    _mesh = mp.solutions.face_mesh.FaceMesh(
        static_image_mode=True, max_num_faces=2, refine_landmarks=True,
        min_detection_confidence=0.5,
    )
    return _mesh


def _point(values: np.ndarray) -> tuple[float, float]:
    """A landmark as two plain floats.

    Kept out of numpy on purpose: the eye coordinates end up in the report metadata, and
    `np.float32` — unlike `np.float64`, which subclasses `float` — is not JSON
    serialisable, so leaving it in place makes json.dumps() reject the whole submission.
    """
    return float(values[0]), float(values[1])


def _eye_aspect_ratio(points: np.ndarray) -> float:
    """Soukupová-Čech eye aspect ratio: lid separation over eye width."""
    vertical = np.linalg.norm(points[1] - points[5]) + np.linalg.norm(points[2] - points[4])
    horizontal = np.linalg.norm(points[0] - points[3])
    return float(vertical / (2.0 * horizontal)) if horizontal > 0 else 0.0


def _mediapipe_geometry(image: np.ndarray) -> FaceGeometry | None:
    mesh = _face_mesh()
    if mesh is None:
        return None
    height, width = image.shape[:2]
    with _mesh_lock:
        result = mesh.process(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
    if not result.multi_face_landmarks:
        return None
    faces = result.multi_face_landmarks
    # The largest face is the subject; extra faces are reported by the single_face check.
    def spread(face) -> float:
        xs = [point.x for point in face.landmark]
        ys = [point.y for point in face.landmark]
        return (max(xs) - min(xs)) * (max(ys) - min(ys))

    face = max(faces, key=spread)
    points = np.array([(point.x * width, point.y * height) for point in face.landmark], np.float32)

    chin_y = float(points[CHIN][1])
    forehead_y = float(points[FOREHEAD][1])
    face_height = max(chin_y - forehead_y, 1.0)
    crown_y = forehead_y - CROWN_EXTENSION * face_height

    left_ear = _eye_aspect_ratio(points[list(LEFT_EYE)])
    right_ear = _eye_aspect_ratio(points[list(RIGHT_EYE)])
    eye_left = _point(points[[LEFT_EYE[0], LEFT_EYE[3]]].mean(axis=0))
    eye_right = _point(points[[RIGHT_EYE[0], RIGHT_EYE[3]]].mean(axis=0))

    gap = float(np.linalg.norm(points[MOUTH_INNER[0]] - points[MOUTH_INNER[1]]))
    mouth_width = float(np.linalg.norm(points[MOUTH_CORNERS[0]] - points[MOUTH_CORNERS[1]]))
    mouth_ratio = gap / mouth_width if mouth_width > 0 else 0.0

    xs, ys = points[:, 0], points[:, 1]
    box = (int(xs.min()), int(ys.min()), int(np.ptp(xs)), int(np.ptp(ys)))
    return FaceGeometry(
        box=box, eye_left=eye_left, eye_right=eye_right, chin_y=chin_y, crown_y=crown_y,
        detector="mediapipe", face_count=len(faces),
        eyes_open=PASS if min(left_ear, right_ear) >= EYE_ASPECT_OPEN else FAIL,
        mouth_closed=PASS if mouth_ratio <= MOUTH_GAP_MAX else FAIL,
        measures={
            "eye_aspect_left": round(left_ear, 3),
            "eye_aspect_right": round(right_ear, 3),
            "mouth_gap_ratio": round(mouth_ratio, 3),
        },
    )


def _cascade(name: str):
    """A bundled Haar cascade, or None on builds that no longer ship them.

    OpenCV 5 removed both `CascadeClassifier` and the cascade XML files, so this is
    checked at call time: on such a build the photo simply has no automatic geometry
    and every landmark criterion is reported as undecided.
    """
    if not hasattr(cv2, "CascadeClassifier"):
        return None
    path = Path(cv2.data.haarcascades) / name
    if not path.is_file():
        return None
    classifier = cv2.CascadeClassifier(str(path))
    return None if classifier.empty() else classifier


def _cascade_geometry(image: np.ndarray) -> FaceGeometry | None:
    """Fallback when MediaPipe is absent: bundled Haar cascades, no model download."""
    detector = _cascade("haarcascade_frontalface_default.xml")
    if detector is None:
        return None
    grey = cv2.equalizeHist(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY))
    height, width = grey.shape[:2]
    faces = detector.detectMultiScale(
        grey, 1.1, 5, minSize=(max(60, width // 10), max(60, height // 10))
    )
    if len(faces) == 0:
        return None
    x, y, w, h = max(faces, key=lambda face: face[2] * face[3])
    # Haar fires on random texture: only a detection comparable in size to the subject
    # counts as a second person, otherwise a patterned background raises a false alarm.
    companions = sum(1 for face in faces if face[2] * face[3] >= 0.40 * w * h)
    # The frontal cascade box runs from mid-forehead to about the chin.
    chin_y = y + 0.98 * h

    eye_detector = _cascade("haarcascade_eye.xml")
    eyes = (
        eye_detector.detectMultiScale(grey[y : y + int(0.6 * h), x : x + w], 1.1, 6)
        if eye_detector is not None else []
    )
    if len(eyes) >= 2:
        first, second = sorted(eyes, key=lambda eye: eye[2] * eye[3], reverse=True)[:2]
        centres = sorted(
            ((x + ex + ew / 2, y + ey + eh / 2) for ex, ey, ew, eh in (first, second)),
            key=lambda point: point[0],
        )
        eye_left, eye_right = centres
        # This cascade only fires on open eyes, so two hits is positive evidence.
        eyes_open = PASS
    else:
        eye_left = (x + 0.30 * w, y + 0.40 * h)
        eye_right = (x + 0.70 * w, y + 0.40 * h)
        eyes_open = UNKNOWN

    # Crown measured up from the eye line rather than taken as a fixed share of the
    # cascade box. The old `y - 0.35h` was inflating the head by about a fifth, which put
    # the eye line at 50.3 % of the frame — just outside the ANTS band — on *every*
    # photograph this path graded. The clamp keeps a stray eye detection from producing an
    # absurd skull.
    eye_y = (eye_left[1] + eye_right[1]) / 2.0
    head = min(max((chin_y - eye_y) / EYE_TO_CHIN_RATIO, 0.9 * h), 1.5 * h)
    crown_y = chin_y - head

    return FaceGeometry(
        box=(int(x), int(y), int(w), int(h)), eye_left=eye_left, eye_right=eye_right,
        chin_y=chin_y, crown_y=crown_y, detector="haar", face_count=companions,
        eyes_open=eyes_open, mouth_closed=UNKNOWN,
        measures={"eyes_detected": int(len(eyes))},
    )


def refine_crown(image: np.ndarray, geometry: FaceGeometry) -> float:
    """Locate the top of the hair against the background, above the face box.

    Chin-to-crown is *the* ANTS dimension, and no landmark model reaches past the
    forehead, so the anthropometric guess is only a starting point.  Scanning downwards
    from above until the central band stops matching the background colour measures the
    skull instead of assuming it.  The answer is clamped around the guess: on a busy
    background the scan degrades to the estimate rather than to nonsense.

    The scan finds the top of the *hair*, not of the skull, so it may only travel
    CROWN_REFINE_LIMIT of a head above the guess.  Unbounded, a voluminous cut was read as
    a fifth of extra skull, which stretched the crop past the edge of the source and
    pushed the eye line out of the ANTS band.
    """
    height, width = image.shape[:2]
    x, y, w, h = geometry.box
    guess = geometry.crown_y
    x0, x1 = max(0, x + w // 4), min(width, x + 3 * w // 4)
    search_top = max(0, int(guess - CROWN_REFINE_LIMIT * geometry.head_height))
    search_bottom = min(height, int(y + 0.25 * h))  # certainly forehead by then
    if x1 - x0 < 8 or search_bottom - search_top < 8:
        return guess

    # Background reference: the two upper corners, which a portrait leaves empty.
    corner_h, corner_w = max(2, height // 8), max(2, width // 6)
    corners = np.concatenate([
        image[:corner_h, :corner_w].reshape(-1, 3), image[:corner_h, -corner_w:].reshape(-1, 3),
    ]).astype(np.int16)
    background = np.median(corners, axis=0)
    spread = float(np.median(np.abs(corners - background).sum(axis=1)))
    # A textured wall already differs from its own median; ask the head to differ more.
    tolerance = max(60.0, 2.5 * spread)

    strip = image[search_top:search_bottom, x0:x1].astype(np.int16)
    distance = np.abs(strip - background).sum(axis=2)
    solid = (distance > tolerance).mean(axis=1) > 0.6
    if not solid.any():
        return guess
    # First row of a run that keeps going: a lone dark row is a shadow, not a skull.
    run = np.convolve(solid.astype(np.float32), np.ones(5) / 5.0, mode="same") > 0.8
    if not run.any():
        return guess
    measured = float(search_top + int(np.argmax(run)))
    return min(max(measured, search_top), float(y))


def detect_face(image: np.ndarray) -> FaceGeometry | None:
    geometry = _mediapipe_geometry(image) or _cascade_geometry(image)
    if geometry is not None:
        crown = refine_crown(image, geometry)
        geometry.measures["crown_shift_px"] = round(crown - geometry.crown_y, 1)
        geometry.crown_y = crown
    return geometry


def available_detector() -> str:
    """Which landmark source this deployment actually has: mediapipe, haar or none.

    Surfaced by /api/health because it decides how many criteria can be measured at all.
    """
    if _face_mesh() is not None:
        return "mediapipe"
    if _cascade("haarcascade_frontalface_default.xml") is not None:
        return "haar"
    return "none"


# ── Framing ─────────────────────────────────────────────────────────────────────────
def crop_box(image: np.ndarray, geometry: FaceGeometry | None, override: CropOverride) -> tuple[float, float, float, float]:
    """The ANTS-framed rectangle in source coordinates; it may fall outside the image."""
    height, width = image.shape[:2]
    if geometry is None:
        # No face: a centred crop of the right ratio still gives the reviewer something
        # to adjust by hand instead of an empty result.
        crop_h = min(float(height), width / PHOTO_RATIO)
        crop_w = crop_h * PHOTO_RATIO
        left, top = (width - crop_w) / 2, (height - crop_h) / 2
    else:
        crop_h = geometry.head_height / HEAD_RATIO_TARGET
        crop_w = crop_h * PHOTO_RATIO
        eye_x, _ = geometry.eye_centre
        left = eye_x - crop_w / 2
        top = geometry.crown_y - TOP_MARGIN_TARGET * crop_h
    if override.active:
        zoom = max(0.5, min(override.zoom, 2.0))
        new_w, new_h = crop_w * zoom, crop_h * zoom
        left -= (new_w - crop_w) / 2 - override.dx * crop_w
        top -= (new_h - crop_h) / 2 - override.dy * crop_h
        crop_w, crop_h = new_w, new_h
    return left, top, crop_w, crop_h


def extract(image: np.ndarray, box: tuple[float, float, float, float]) -> tuple[np.ndarray, float]:
    """Cut the rectangle out, extending the sheet with the background colour if needed.

    Returns the crop and the share of it that had to be invented, so that a photograph
    framed too tightly to satisfy ANTS is flagged instead of quietly padded.
    """
    height, width = image.shape[:2]
    left, top, crop_w, crop_h = (round(value) for value in box)
    pad_left, pad_top = max(0, -left), max(0, -top)
    pad_right, pad_bottom = max(0, left + crop_w - width), max(0, top + crop_h - height)
    padded_area = (crop_w * crop_h) - (
        max(0, min(left + crop_w, width) - max(left, 0)) * max(0, min(top + crop_h, height) - max(top, 0))
    )
    if pad_left or pad_top or pad_right or pad_bottom:
        # Edge replication rather than a flat fill: on a plain background it continues it
        # exactly, and on a textured one it smears the texture instead of leaving the
        # visible grey band a constant colour produces.
        image = cv2.copyMakeBorder(image, pad_top, pad_bottom, pad_left, pad_right, cv2.BORDER_REPLICATE)
        left, top = left + pad_left, top + pad_top
    crop = image[top : top + crop_h, left : left + crop_w]
    fraction = padded_area / float(max(crop_w * crop_h, 1))
    return crop, max(0.0, fraction)


def visible_head_ratio(geometry: FaceGeometry, box: tuple[float, float, float, float], source: np.ndarray) -> float:
    """Share of the frame filled by the part of the head the source actually holds.

    Comparing the measured head against the crop it produced is circular — crop_box()
    sizes that crop as `head / HEAD_RATIO_TARGET`, so the answer is always the target and
    the criterion grades nothing.  What does vary is how much of the head the photograph
    contains: a shot cropped above the skull or below the chin yields less than a whole
    one, and that is what puts the export outside the ANTS band.
    """
    left, top, crop_w, crop_h = box
    if crop_h <= 0:
        return 0.0
    height = source.shape[0]
    visible_top = max(geometry.crown_y, 0.0, top)
    visible_bottom = min(geometry.chin_y, float(height), top + crop_h)
    return max(0.0, visible_bottom - visible_top) / crop_h


def _border_colour(image: np.ndarray) -> np.ndarray:
    """Median colour of the outer frame, i.e. the studio background of the shot."""
    height, width = image.shape[:2]
    strip = max(2, min(height, width) // 40)
    samples = np.concatenate([
        image[:strip].reshape(-1, 3), image[-strip:].reshape(-1, 3),
        image[:, :strip].reshape(-1, 3), image[:, -strip:].reshape(-1, 3),
    ])
    return np.median(samples, axis=0)


# ── Image quality ───────────────────────────────────────────────────────────────────
def normalise_exposure(image: np.ndarray) -> np.ndarray:
    """Even out lighting: local contrast first, then a global pull to a neutral level."""
    lab = cv2.cvtColor(image, cv2.COLOR_BGR2LAB)
    lightness, a, b = cv2.split(lab)
    lightness = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8)).apply(lightness)
    median = float(np.median(lightness))
    if median and not 110 <= median <= 200:
        # Multiplicative, so it corrects the exposure without crushing the highlights.
        lightness = np.clip(lightness.astype(np.float32) * (160.0 / median), 0, 255).astype(np.uint8)
    return cv2.cvtColor(cv2.merge((lightness, a, b)), cv2.COLOR_LAB2BGR)


def background_stats(image: np.ndarray, alpha: np.ndarray | None = None) -> tuple[float, float]:
    """Mean level and dispersion of the background.

    Given the cut-out alpha, the statistic is read exactly where the subject is not, which
    is the only reliable way to do it: every geometric approximation eventually measures
    the sitter.  A strip across the top reads a bun or an afro as background dispersion —
    crop_box() anchors the *crown*, and hair legitimately stands above it, since ANTS
    measures the skull and not the haircut.  Side bands fare no better on a wide curly
    cut.  Both mistakes turned flawless cut-outs into σ 28 to 79.

    Without a mask — no détourage, or one that failed — it falls back to two side bands
    stopping at 60 % of the height, below which they would run into the shoulders and grade
    clothing as background.
    """
    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    height, width = grey.shape[:2]
    if alpha is not None:
        scaled = cv2.resize(alpha, (width, height), interpolation=cv2.INTER_LINEAR)
        samples = grey[scaled < 0.1]
        if samples.size >= 64:
            return float(samples.mean()), float(samples.std())
    band = max(2, width // 12)
    shoulder_line = max(2, int(0.60 * height))
    samples = np.concatenate([
        grey[:shoulder_line, :band].ravel(), grey[:shoulder_line, -band:].ravel(),
    ])
    return float(samples.mean()), float(samples.std())


@dataclass
class ExposureStats:
    """How the face itself is lit, read independently of the sitter's complexion."""

    median: float
    blown: float  # per cent of pixels pinned at the top of the scale
    crushed: float  # per cent pinned at the bottom
    spread: float  # p95 − p5

    @property
    def ok(self) -> bool:
        return (
            self.blown <= EXPOSURE_CLIP_MAX
            and self.crushed <= EXPOSURE_CLIP_MAX
            and self.spread >= EXPOSURE_SPREAD_MIN
            and EXPOSURE_MEDIAN_MIN <= self.median <= EXPOSURE_MEDIAN_MAX
        )

    @property
    def detail(self) -> str:
        return (f"médiane {self.median:.0f}, écrêtage {self.blown:.1f}/{self.crushed:.1f} %,"
                f" étendue {self.spread:.0f}")


def face_exposure(image: np.ndarray, face_box: tuple[int, int, int, int] | None) -> ExposureStats:
    """How the face is exposed, in this image's own coordinates.

    Measured over the whole frame instead, the criterion grades the background: once
    flatten_background() has painted its plain colour over more than half the pixels, the
    median *is* that colour and the photograph fails on brightness however well exposed
    the subject is.  The box is taken in by a fifth on each side so that its corners —
    which are background, not skin — cannot drag the reading towards whatever is behind
    the sitter either.  Without a face box there is nothing better than the full frame.
    """
    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    if face_box is not None:
        height, width = grey.shape[:2]
        x, y, w, h = face_box
        x0, y0 = max(0, int(x + 0.2 * w)), max(0, int(y + 0.2 * h))
        x1, y1 = min(width, int(x + 0.8 * w)), min(height, int(y + 0.8 * h))
        if x1 - x0 >= 8 and y1 - y0 >= 8:
            grey = grey[y0:y1, x0:x1]
    low, high = np.percentile(grey, (5, 95))
    return ExposureStats(
        median=float(np.median(grey)),
        blown=float((grey >= 250).mean() * 100),
        crushed=float((grey <= 5).mean() * 100),
        spread=float(high - low),
    )


def _selfie_segmenter():
    """One cached selfie-segmentation graph, or None when MediaPipe is not installed.

    Like the Face Mesh it is stateful, costly to build and not thread-safe, so it is
    cached behind `_segmenter_lock`.  The weights travel inside the wheel: no download
    happens at runtime, which matters on a sealed container.
    """
    global _segmenter, _segmenter_unavailable
    if _segmenter is not None or _segmenter_unavailable:
        return _segmenter
    try:
        import mediapipe as mp  # noqa: PLC0415 - optional heavy dependency
    except Exception:
        _segmenter_unavailable = True
        return None
    # model_selection 0 is the general 256×256 model; 1 is a 144×256 landscape variant
    # meant for video calls, and it loses hair detail on a portrait crop.
    _segmenter = mp.solutions.selfie_segmentation.SelfieSegmentation(model_selection=0)
    return _segmenter


def person_mask(image: np.ndarray) -> np.ndarray | None:
    """Soft 0–1 alpha over the subject, from MediaPipe's selfie segmentation.

    None when MediaPipe is absent or the cut is implausible, so that the caller falls
    back to GrabCut instead of compositing a mask that kept the wall and dropped the
    sitter.
    """
    segmenter = _selfie_segmenter()
    if segmenter is None:
        return None
    height, width = image.shape[:2]
    with _segmenter_lock:
        result = segmenter.process(cv2.cvtColor(image, cv2.COLOR_BGR2RGB))
    mask = getattr(result, "segmentation_mask", None)
    if mask is None:
        return None
    alpha = cv2.resize(np.asarray(mask, np.float32), (width, height), interpolation=cv2.INTER_LINEAR)
    # On an ANTS crop the sitter fills most of the frame; anything outside this band is a
    # cut that failed rather than an unusual portrait.
    if not 0.15 <= float((alpha > 0.5).mean()) <= 0.95:
        return None
    return alpha


def _subject_intact(alpha: np.ndarray, face_box: tuple[int, int, int, int]) -> bool:
    """True when the cut kept the face itself, whatever it did with the rest."""
    height, width = alpha.shape[:2]
    x, y, w, h = (int(value) for value in face_box)
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(width, x + w), min(height, y + h)
    if x1 - x0 < 8 or y1 - y0 < 8:
        return True  # nothing to verify against; the coverage band already had its say
    return float(alpha[y0:y1, x0:x1].mean()) >= SUBJECT_INTACT_MIN


def flatten_background(
    image: np.ndarray, face_box: tuple[int, int, int, int] | None,
) -> tuple[np.ndarray, np.ndarray | None]:
    """Cut the subject out and set it on the regulation background.

    ANTS wants a plain, light background with no shadow cast on it, so the sitter is
    composited onto UNIFORM_BACKGROUND rather than the original being merely evened out.
    Segmentation comes from MediaPipe when it is installed — it follows hair and
    shoulders, which a rectangle-initialised GrabCut cannot — and falls back to GrabCut
    otherwise.  `face_box` is in this image's own coordinates, not the source frame's.

    Returns the composited image and the alpha it used, so that background_ok can be
    graded where the subject actually is not.  On failure it hands back the very same
    array it was given, and no alpha.
    """
    alpha = person_mask(image)
    if alpha is None:
        alpha = _grabcut_alpha(image, face_box)
    if alpha is None:
        return image, None
    if face_box is not None and not _subject_intact(alpha, face_box):
        return image, None
    alpha = np.clip((alpha - 0.5) * EDGE_SHARPNESS + 0.5, 0.0, 1.0)
    alpha = cv2.GaussianBlur(alpha, (0, 0), EDGE_FEATHER)
    plain = np.full_like(image, UNIFORM_BACKGROUND, dtype=np.uint8)
    composited = np.clip(image * alpha[..., None] + plain * (1 - alpha[..., None]), 0, 255).astype(np.uint8)
    return composited, alpha


def _grabcut_alpha(image: np.ndarray, face_box: tuple[int, int, int, int] | None) -> np.ndarray | None:
    """Fallback segmentation: GrabCut on a downscaled copy, as a 0–1 alpha.

    The cut only needs to be accurate to a few source pixels once the mask is feathered,
    and the subject rectangle grows from the face down to the bottom edge so that the
    shoulders stay inside it.
    """
    height, width = image.shape[:2]
    scale = 320.0 / max(height, width)
    small = cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA) if scale < 1 else image.copy()
    small_h, small_w = small.shape[:2]

    if face_box is not None:
        factor = scale if scale < 1 else 1.0
        x, y, w, h = (int(value * factor) for value in face_box)
        x0, y0 = max(1, x - w // 2), max(1, y - h // 3)
        x1, y1 = min(small_w - 1, x + w + w // 2), small_h - 1
        rect = (x0, y0, x1 - x0, y1 - y0)
    else:
        rect = (small_w // 6, small_h // 8, small_w * 2 // 3, small_h * 7 // 8)
    if rect[2] < 10 or rect[3] < 10:
        return None

    mask = np.zeros(small.shape[:2], np.uint8)
    try:
        cv2.grabCut(small, mask, rect, np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64), 3, cv2.GC_INIT_WITH_RECT)
    except cv2.error:
        return None
    foreground = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    if foreground.mean() < 25:  # the cut kept almost nothing: leave the photo untouched
        return None
    return cv2.resize(foreground, (width, height), interpolation=cv2.INTER_LINEAR).astype(np.float32) / 255.0


# ── Checks ──────────────────────────────────────────────────────────────────────────
def _band(value: float, low: float, high: float) -> str:
    return PASS if low <= value <= high else FAIL


def build_checks(
    geometry: FaceGeometry | None, export: np.ndarray, data: bytes,
    padding: float, background: tuple[float, float], sharpness: float,
    head_ratio: float, eye_level: float, exposure: ExposureStats,
) -> list[Check]:
    height, width = export.shape[:2]
    ratio_ok = (
        width >= OUT_WIDTH and height >= OUT_HEIGHT
        and abs((width / height) / PHOTO_RATIO - 1) <= RATIO_TOLERANCE
    )
    mean, deviation = background
    checks = [
        Check("face_detected", "Visage détecté", PASS if geometry else FAIL,
              "" if geometry else "aucun visage exploitable"),
        Check("single_face", "Une seule personne",
              UNKNOWN if not geometry else PASS if geometry.face_count == 1 else FAIL,
              "" if not geometry or geometry.face_count == 1 else f"{geometry.face_count} visages"),
        Check("eyes_open", "Yeux ouverts", geometry.eyes_open if geometry else UNKNOWN,
              _measure_detail(geometry, ("eye_aspect_left", "eye_aspect_right"))),
        Check("mouth_closed", "Bouche fermée", geometry.mouth_closed if geometry else UNKNOWN,
              _measure_detail(geometry, ("mouth_gap_ratio",))),
        Check("head_tilt_ok", f"Tête droite (≤ {TILT_MAX_DEG:.0f}°)",
              UNKNOWN if not geometry or geometry.eyes_open == UNKNOWN
              else PASS if geometry.roll_degrees <= TILT_MAX_DEG else FAIL,
              f"{geometry.roll_degrees:.1f}°" if geometry else ""),
        Check("head_size_ok", f"Hauteur du visage {HEAD_RATIO_MIN:.0%}–{HEAD_RATIO_MAX:.0%} du cadre",
              UNKNOWN if not geometry else _band(head_ratio, HEAD_RATIO_MIN, HEAD_RATIO_MAX),
              f"{head_ratio:.0%}" if geometry else ""),
        Check("eye_level_ok", "Ligne des yeux dans le tiers supérieur",
              UNKNOWN if not geometry else _band(eye_level, EYE_LINE_MIN, EYE_LINE_MAX),
              f"{eye_level:.0%} du haut" if geometry else ""),
        Check("framing_ok", "Cadrage complet dans la photo source",
              PASS if padding <= PADDING_LIMIT else FAIL,
              f"{padding:.1%} de marge ajoutée" if padding else ""),
        Check("background_ok", "Fond uni et clair, non blanc",
              PASS if deviation <= BACKGROUND_STD_MAX
              and BACKGROUND_MIN_LEVEL <= mean <= BACKGROUND_MAX_LEVEL else FAIL,
              f"niveau {mean:.0f}, écart-type {deviation:.1f}"),
        Check("exposure_ok", "Luminosité et contraste corrects",
              PASS if exposure.ok else FAIL, exposure.detail),
        Check("sharpness_ok", "Netteté suffisante", PASS if sharpness >= SHARPNESS_MIN else FAIL,
              f"variance {sharpness:.0f}"),
        Check("dimensions_ok", f"Dimensions ≥ {OUT_WIDTH}×{OUT_HEIGHT} au ratio 35:45",
              PASS if ratio_ok else FAIL, f"{width}×{height}"),
        Check("weight_ok", f"Poids ≤ {MAX_BYTES // 1_000_000} Mo",
              PASS if len(data) <= MAX_BYTES else FAIL, f"{len(data) / 1000:.0f} ko"),
        Check("format_ok", "Format JPEG", PASS, "image/jpeg"),
    ]
    return checks


def _measure_detail(geometry: FaceGeometry | None, keys: tuple[str, ...]) -> str:
    if geometry is None:
        return ""
    parts = [f"{key.rsplit('_', 1)[-1]} {geometry.measures[key]}" for key in keys if key in geometry.measures]
    return ", ".join(parts)


# ── Entry point ─────────────────────────────────────────────────────────────────────
def process(data: bytes, override: CropOverride | None = None, flatten: str = "always") -> ProcessedImage:
    """Crop, correct and grade one identity photograph."""
    override = override or CropOverride()
    try:
        source = imaging.decode(data)
    except ValueError as error:
        return ProcessedImage(b"", "image/jpeg", ".jpg", 0, 0, error=str(error))

    geometry = detect_face(source)
    corrected = normalise_exposure(source)
    box = crop_box(corrected, geometry, override)
    crop, padding = extract(corrected, box)

    face_in_crop = None
    if geometry is not None:
        x, y, w, h = geometry.box
        face_in_crop = (int(x - box[0]), int(y - box[1]), w, h)  # crop coordinates

    replaced, cut_alpha = False, None
    if flatten != "never":
        mean, deviation = background_stats(crop)
        # "always" is the deployed setting: ANTS wants a plain light background with no
        # shadow behind the sitter, and a photograph taken at home never has one. "auto"
        # remains for tuning, and only intervenes when the original is out of spec.
        if flatten == "always" or deviation > BACKGROUND_STD_MAX or not (
            BACKGROUND_MIN_LEVEL <= mean <= BACKGROUND_MAX_LEVEL
        ):
            candidate, alpha = flatten_background(crop, face_in_crop)
            # flatten_background() hands back the very same array when it could not cut
            # the subject out safely; shipping a mutilated photograph would be worse than
            # the busy original, and background_ok then reports the failure to the panel.
            if candidate is not crop:
                crop, replaced, cut_alpha = candidate, True, alpha

    # Export at the sanctioned ratio: twice the minimum when the source can feed it.
    factor = 2 if crop.shape[0] >= 2 * OUT_HEIGHT else 1
    size = (OUT_WIDTH * factor, OUT_HEIGHT * factor)
    interpolation = cv2.INTER_AREA if crop.shape[0] > size[1] else cv2.INTER_CUBIC
    export = cv2.resize(crop, size, interpolation=interpolation)
    encoded, quality = imaging.encode_jpeg(export, MAX_BYTES)

    crop_h = box[3]
    head_ratio = visible_head_ratio(geometry, box, source) if geometry else 0.0
    eye_level = ((geometry.eye_centre[1] - box[1]) / crop_h) if geometry and crop_h else 0.0
    sharpness = imaging.laplacian_sharpness(export)
    # The face box travels from crop pixels to export pixels with the export resize.
    face_in_export = None
    if face_in_crop is not None:
        scale_x, scale_y = export.shape[1] / crop.shape[1], export.shape[0] / crop.shape[0]
        x, y, w, h = face_in_crop
        face_in_export = (x * scale_x, y * scale_y, w * scale_x, h * scale_y)
    exposure = face_exposure(export, face_in_export)
    checks = build_checks(
        geometry, export, encoded, padding, background_stats(export, cut_alpha), sharpness,
        head_ratio, eye_level, exposure,
    )
    metadata = {
        "detector": geometry.detector if geometry else "none",
        "faces_found": geometry.face_count if geometry else 0,
        "head_ratio": round(head_ratio, 3),
        "eye_level": round(eye_level, 3),
        "tilt_degrees": round(geometry.roll_degrees, 2) if geometry else None,
        "padding_fraction": round(padding, 4),
        "background_replaced": replaced,
        "face_exposure": round(exposure.median, 1),
        "face_clipping": [round(exposure.blown, 2), round(exposure.crushed, 2)],
        "face_spread": round(exposure.spread, 1),
        "sharpness": round(sharpness, 1),
        "jpeg_quality": quality,
        "crop_box": [round(value) for value in box],
        "source_size": [source.shape[1], source.shape[0]],
        "override": {"zoom": override.zoom, "dx": override.dx, "dy": override.dy} if override.active else None,
        **(geometry.measures if geometry else {}),
    }
    return ProcessedImage(
        data=encoded, media_type="image/jpeg", extension=".jpg",
        width=export.shape[1], height=export.shape[0], checks=checks, metadata=metadata,
    )


def main() -> int:
    """Batch the folder given on the command line, for tuning and smoke tests."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("input"))
    parser.add_argument("--output", type=Path, default=Path("output_photos"))
    parser.add_argument("--flatten", choices=("auto", "always", "never"), default="auto")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    for path in sorted(p for p in args.input.iterdir() if p.is_file()):
        result = process(path.read_bytes(), flatten=args.flatten)
        if result.error:
            print(f"{path.name}: ERREUR {result.error}")
            continue
        (args.output / f"{path.stem}_ants.jpg").write_bytes(result.data)
        failed = [check.key for check in result.checks if check.status == FAIL]
        unknown = [check.key for check in result.checks if check.status == UNKNOWN]
        print(
            f"{path.name}: score {result.score}% via {result.metadata['detector']}"
            f" | échecs: {', '.join(failed) or 'aucun'}"
            f" | indéterminés: {', '.join(unknown) or 'aucun'}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
