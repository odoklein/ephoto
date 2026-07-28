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
EYE_ASPECT_OPEN = 0.19  # eye aspect ratio below this reads as a closed or blinking eye
MOUTH_GAP_MAX = 0.15  # inner lip gap over mouth width
BACKGROUND_STD_MAX, BACKGROUND_MIN_LEVEL = 16.0, 120.0
SHARPNESS_MIN = 45.0
PADDING_LIMIT = 0.04  # share of the crop that may be invented when the source is tight
UNIFORM_BACKGROUND = (242, 242, 242)  # light neutral grey, accepted by ANTS

# MediaPipe Face Mesh indices (468-point topology).
CHIN, FOREHEAD = 152, 10
LEFT_EYE = (33, 160, 158, 133, 153, 144)
RIGHT_EYE = (362, 385, 387, 263, 373, 380)
MOUTH_INNER = (13, 14)
MOUTH_CORNERS = (61, 291)

_mesh_lock = threading.Lock()
_mesh = None
_mesh_unavailable = False


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
    eye_left = tuple(points[[LEFT_EYE[0], LEFT_EYE[3]]].mean(axis=0))
    eye_right = tuple(points[[RIGHT_EYE[0], RIGHT_EYE[3]]].mean(axis=0))

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
    # The frontal cascade box runs from mid-forehead to about the chin; the crown guess
    # is deliberately generous because refine_crown() measures it properly afterwards.
    chin_y = y + 0.94 * h
    crown_y = y - 0.35 * h

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

    return FaceGeometry(
        box=(int(x), int(y), int(w), int(h)), eye_left=eye_left, eye_right=eye_right,
        chin_y=chin_y, crown_y=crown_y, detector="haar", face_count=len(faces),
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
    """
    height, width = image.shape[:2]
    x, y, w, h = geometry.box
    guess = geometry.crown_y
    x0, x1 = max(0, x + w // 4), min(width, x + 3 * w // 4)
    search_top = max(0, int(guess - 0.40 * h))
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
        colour = [float(channel) for channel in _border_colour(image)]
        image = cv2.copyMakeBorder(
            image, pad_top, pad_bottom, pad_left, pad_right, cv2.BORDER_CONSTANT, value=colour
        )
        left, top = left + pad_left, top + pad_top
    crop = image[top : top + crop_h, left : left + crop_w]
    fraction = padded_area / float(max(crop_w * crop_h, 1))
    return crop, max(0.0, fraction)


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


def background_stats(image: np.ndarray) -> tuple[float, float]:
    """Mean level and dispersion of the frame border, where only background should be."""
    height, width = image.shape[:2]
    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    band = max(2, width // 12)
    top_band = max(2, height // 8)
    samples = np.concatenate([
        grey[:top_band].ravel(), grey[:, :band].ravel(), grey[:, -band:].ravel(),
    ])
    return float(samples.mean()), float(samples.std())


def flatten_background(image: np.ndarray, face_box: tuple[int, int, int, int] | None) -> np.ndarray:
    """Replace a busy background with a uniform light grey.

    `face_box` is expressed in this image's own coordinates, not the source frame's.
    GrabCut runs on a downscaled copy — the cut only needs to be accurate to a few
    source pixels once the mask is feathered — and the subject rectangle grows from the
    face down to the bottom edge so the shoulders stay inside it.
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
        return image

    mask = np.zeros(small.shape[:2], np.uint8)
    try:
        cv2.grabCut(small, mask, rect, np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64), 3, cv2.GC_INIT_WITH_RECT)
    except cv2.error:
        return image
    foreground = np.where((mask == cv2.GC_FGD) | (mask == cv2.GC_PR_FGD), 255, 0).astype(np.uint8)
    if foreground.mean() < 25:  # the cut kept almost nothing: leave the photo untouched
        return image
    alpha = cv2.GaussianBlur(cv2.resize(foreground, (width, height), interpolation=cv2.INTER_LINEAR), (0, 0), 2.0)
    alpha = (alpha.astype(np.float32) / 255.0)[..., None]
    plain = np.full_like(image, UNIFORM_BACKGROUND, dtype=np.uint8)
    return np.clip(image * alpha + plain * (1 - alpha), 0, 255).astype(np.uint8)


# ── Checks ──────────────────────────────────────────────────────────────────────────
def _band(value: float, low: float, high: float) -> str:
    return PASS if low <= value <= high else FAIL


def build_checks(
    geometry: FaceGeometry | None, export: np.ndarray, data: bytes,
    padding: float, background: tuple[float, float], sharpness: float,
    head_ratio: float, eye_level: float,
) -> list[Check]:
    height, width = export.shape[:2]
    exposure = float(np.median(cv2.cvtColor(export, cv2.COLOR_BGR2GRAY)))
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
        Check("background_ok", "Fond clair et uniforme",
              PASS if deviation <= BACKGROUND_STD_MAX and mean >= BACKGROUND_MIN_LEVEL else FAIL,
              f"niveau {mean:.0f}, écart-type {deviation:.1f}"),
        Check("exposure_ok", "Luminosité correcte", _band(exposure, 100, 210), f"médiane {exposure:.0f}"),
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
def process(data: bytes, override: CropOverride | None = None, flatten: str = "auto") -> ProcessedImage:
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

    replaced = False
    if flatten != "never":
        mean, deviation = background_stats(crop)
        if flatten == "always" or deviation > BACKGROUND_STD_MAX or mean < BACKGROUND_MIN_LEVEL:
            face_box = None
            if geometry is not None:
                x, y, w, h = geometry.box
                face_box = (int(x - box[0]), int(y - box[1]), w, h)  # crop coordinates
            candidate = flatten_background(crop, face_box)
            # A segmentation that did not actually even out the background is discarded:
            # shipping a badly cut photograph would be worse than the busy original.
            if flatten == "always" or background_stats(candidate)[1] < deviation:
                crop, replaced = candidate, True

    # Export at the sanctioned ratio: twice the minimum when the source can feed it.
    factor = 2 if crop.shape[0] >= 2 * OUT_HEIGHT else 1
    size = (OUT_WIDTH * factor, OUT_HEIGHT * factor)
    interpolation = cv2.INTER_AREA if crop.shape[0] > size[1] else cv2.INTER_CUBIC
    export = cv2.resize(crop, size, interpolation=interpolation)
    encoded, quality = imaging.encode_jpeg(export, MAX_BYTES)

    crop_h = box[3]
    head_ratio = geometry.head_height / crop_h if geometry and crop_h else 0.0
    eye_level = ((geometry.eye_centre[1] - box[1]) / crop_h) if geometry and crop_h else 0.0
    sharpness = imaging.laplacian_sharpness(export)
    checks = build_checks(
        geometry, export, encoded, padding, background_stats(export), sharpness, head_ratio, eye_level,
    )
    metadata = {
        "detector": geometry.detector if geometry else "none",
        "faces_found": geometry.face_count if geometry else 0,
        "head_ratio": round(head_ratio, 3),
        "eye_level": round(eye_level, 3),
        "tilt_degrees": round(geometry.roll_degrees, 2) if geometry else None,
        "padding_fraction": round(padding, 4),
        "background_replaced": replaced,
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
