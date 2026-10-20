"""Background unification: the client's colour, exactly, everywhere around the sitter.

A cut-out composited through a soft mask leaves three defects that a reviewer reads as
"some areas darker, some lighter":

* the original wall kept outright wherever the segmentation said "person" — pockets
  between curls, the gap between hair and ear;
* the original wall *mixed into* every edge pixel — a partly transparent hair pixel is a
  blend of hair and wall, so `image·α + target·(1−α)` keeps a dark fringe from a dark
  wall and a glowing halo from a white one;
* nothing replaced at all, when a wall that merely looked acceptable was kept with its
  own shade of grey and its vignette.

The pipeline here works at delivery resolution:

1. a coarse person mask (MediaPipe, or GrabCut without it) becomes a trimap: sure wall,
   protected subject (face, body core), and a refinement zone around the edge that runs
   deep into the hair, where pockets of wall hide;
2. the original wall is modelled as a smooth colour field B, learnt from the sure-wall
   pixels, and the sitter's local colour as a field F;
3. in the zone, alpha comes from where each pixel's colour lies between B and F — so a
   pocket of wall reads as wall whatever the network said — fused with an edge-aware
   (colour guided filter) refinement of the coarse mask where sitter and wall are too
   similar for colour to decide;
4. each edge pixel's true foreground colour is recovered (the wall's contribution is
   divided out) and re-composited over the target: αF + (1−α)·T.  Pure wall pixels end up
   *exactly* the target colour.

Everything is classical image processing — no model beyond the segmentation already in
use, no download, deterministic for a given input.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

TARGET_HEX = "#d4d7d3"
TARGET_RGB = (0xD4, 0xD7, 0xD3)
TARGET_BGR = np.array(TARGET_RGB[::-1], np.float32)

# Acceptance of the delivered file, in CIE76 ΔE against the target.  A flat grey through
# JPEG q90+ moves by well under 1; about 2.3 is a just-noticeable difference.
DELTA_E_MEAN_MAX = 2.0
DELTA_E_P95_MAX = 5.0
# Share of the edge zone still showing the old wall (pockets, fringes) that is tolerated.
RESIDUAL_WALL_MAX = 0.01
# In "auto" mode the original is kept only if it already *is* the client's colour.
KEEP_DELTA_E_MEAN_MAX, KEEP_DELTA_E_P95_MAX = 2.0, 4.0

# Trimap, as shares of the image's longer side unless stated.
SURE_BG_ALPHA, SURE_FG_ALPHA = 0.08, 0.92
BG_MARGIN = 0.008  # sure wall starts this far outside the coarse edge
HAIR_DEPTH = 0.05  # above the chin: refine this deep into the subject (curls, buns, fringes)
BODY_DEPTH = 0.022  # below it clothing may match the wall: stay close to the edge
FACE_MARGIN = 0.012  # the face is protected from this far inside the outline
SKIN_DELTA_E = 20.0  # an edge pixel this close (Lab) to the sitter's skin is never removed
GUIDE_RADIUS, GUIDE_EPS = 0.006, 2e-3
# Colour-model alpha is trusted in proportion to how far apart sitter and wall are (RGB).
CONTRAST_LOW, CONTRAST_HIGH = 15.0, 55.0
# Detached blobs smaller than this share of the frame are wall noise, not subject.
SPECK_MAX = 0.0005


@dataclass
class Matte:
    image: np.ndarray  # composited BGR, uint8
    alpha: np.ndarray  # final opacity of the sitter, 0–1
    wall: np.ndarray  # estimated original wall colour, float32 BGR
    residual_wall: float  # share of the refinement zone that still carries the old wall
    zone_fraction: float  # share of the frame that was refined (diagnostic)


# ── Colour measurement ──────────────────────────────────────────────────────────────
def _lab(image: np.ndarray) -> np.ndarray:
    """True CIE Lab (L 0–100) from BGR uint8 or float 0–255."""
    return cv2.cvtColor(np.asarray(image, np.float32) / 255.0, cv2.COLOR_BGR2Lab)


TARGET_LAB = _lab(TARGET_BGR.reshape(1, 1, 3))[0, 0]


def delta_e(image: np.ndarray) -> np.ndarray:
    """Per-pixel CIE76 distance to the target colour."""
    return np.linalg.norm(_lab(image) - TARGET_LAB, axis=2)


def target_match(image: np.ndarray, where: np.ndarray | None) -> tuple[float, float]:
    """Mean and 95th-percentile ΔE to the target over the selected pixels (or all)."""
    distances = delta_e(image)
    samples = distances[where] if where is not None else distances.ravel()
    if samples.size < 64:
        return float("nan"), float("nan")
    return float(samples.mean()), float(np.percentile(samples, 95))


# ── Filters ─────────────────────────────────────────────────────────────────────────
def _box(values: np.ndarray, radius: int) -> np.ndarray:
    size = 2 * radius + 1
    return cv2.boxFilter(values, cv2.CV_32F, (size, size), normalize=True, borderType=cv2.BORDER_REFLECT)


def guided_filter(guide: np.ndarray, source: np.ndarray, radius: int, eps: float) -> np.ndarray:
    """Colour guided filter (He, Sun & Tang): smooth `source`, but along `guide`'s edges.

    Applied to a mask it pulls a blocky, low-resolution segmentation onto the real
    outline of hair and shoulders.  `guide` is BGR in 0–1; the 3×3 colour covariance is
    inverted per pixel in closed form.
    """
    radius = max(1, int(radius))
    I = guide.astype(np.float32)
    p = source.astype(np.float32)
    mean = [_box(I[..., c], radius) for c in range(3)]
    mean_p = _box(p, radius)
    cov_p = [_box(I[..., c] * p, radius) - mean[c] * mean_p for c in range(3)]
    var = {(i, j): _box(I[..., i] * I[..., j], radius) - mean[i] * mean[j] for i in range(3) for j in range(i, 3)}
    a11, a12, a13 = var[0, 0] + eps, var[0, 1], var[0, 2]
    a22, a23, a33 = var[1, 1] + eps, var[1, 2], var[2, 2] + eps
    c11, c12, c13 = a22 * a33 - a23 * a23, a13 * a23 - a12 * a33, a12 * a23 - a13 * a22
    c22, c23, c33 = a11 * a33 - a13 * a13, a12 * a13 - a11 * a23, a11 * a22 - a12 * a12
    det = a11 * c11 + a12 * c12 + a13 * c13
    det = np.where(np.abs(det) < 1e-12, 1e-12, det)
    k0 = (c11 * cov_p[0] + c12 * cov_p[1] + c13 * cov_p[2]) / det
    k1 = (c12 * cov_p[0] + c22 * cov_p[1] + c23 * cov_p[2]) / det
    k2 = (c13 * cov_p[0] + c23 * cov_p[1] + c33 * cov_p[2]) / det
    offset = mean_p - k0 * mean[0] - k1 * mean[1] - k2 * mean[2]
    return (_box(k0, radius) * I[..., 0] + _box(k1, radius) * I[..., 1]
            + _box(k2, radius) * I[..., 2] + _box(offset, radius))


def smooth_fill(values: np.ndarray, weight: np.ndarray, sigmas: list[float]) -> np.ndarray:
    """Weighted local mean of `values`, widened scale by scale until every pixel has one.

    Normalised convolution: where `weight` is dense the estimate is local (the wall right
    behind a curl, with its own shading); where it is empty — under the sitter — it is
    filled from progressively wider neighbourhoods.  Wide scales run on a reduced copy:
    the field is smooth by construction, and a σ of 400 px at full size would be slow.
    """
    height, width = weight.shape
    channels = values.shape[2]
    out = np.zeros((height, width, channels), np.float32)
    filled = np.zeros((height, width), bool)
    weighted = values.astype(np.float32) * weight[..., None]
    for sigma in sigmas:
        factor = int(max(1, min(16, sigma // 6)))
        size = (max(1, width // factor), max(1, height // factor))
        num = cv2.resize(weighted, size, interpolation=cv2.INTER_AREA) if factor > 1 else weighted
        den = cv2.resize(weight, size, interpolation=cv2.INTER_AREA) if factor > 1 else weight
        num = cv2.GaussianBlur(num, (0, 0), max(0.5, sigma / factor))
        den = cv2.GaussianBlur(den, (0, 0), max(0.5, sigma / factor))
        if factor > 1:
            num = cv2.resize(num, (width, height), interpolation=cv2.INTER_LINEAR)
            den = cv2.resize(den, (width, height), interpolation=cv2.INTER_LINEAR)
        num = num.reshape(height, width, channels)
        ready = (den > 1e-3) & ~filled
        out[ready] = num[ready] / den[ready][:, None]
        filled |= ready
        if filled.all():
            break
    if not filled.all():
        total = max(float(weight.sum()), 1e-6)
        out[~filled] = weighted.reshape(-1, channels).sum(axis=0) / total
    return out


def _smoothstep(low: float, high: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - low) / (high - low), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


# ── Matting and compositing ─────────────────────────────────────────────────────────
def _protected(coarse: np.ndarray, face_box: tuple[float, float, float, float] | None, unit: int) -> np.ndarray:
    """Subject pixels that are never refined: the face, and the body core.

    The core is the coarse subject eroded — deeply above the chin, where hair can hold
    pockets of wall well inside the outline, shallowly below it, where a grey jumper on a
    grey wall gives the colour model nothing to work with.

    The face ellipse is protected only *inside* the mask's outline.  Protected outright,
    it reached past the jaw onto the strip of wall that the 256-pixel mask spills over
    the neck, forced it to "subject", and shipped it at the old wall's brightness — the
    light rim along the jaw and the shoulders.  At the outline, the trust term already
    stops colour from deciding where skin and wall look alike.
    """
    height, width = coarse.shape
    inside = cv2.distanceTransform((coarse > SURE_FG_ALPHA).astype(np.uint8), cv2.DIST_L2, 5)
    protect = inside > BODY_DEPTH * unit
    if face_box is None:
        return protect
    x, y, w, h = face_box
    chin = int(np.clip(y + h, 0, height))
    protect[:chin] = inside[:chin] > HAIR_DEPTH * unit
    face = np.zeros((height, width), np.uint8)
    centre = (int(round(x + w / 2)), int(round(y + h / 2)))
    axes = (max(1, int(round(0.55 * w))), max(1, int(round(0.55 * h))))
    cv2.ellipse(face, centre, axes, 0, 0, 360, 1, -1)
    return protect | (face.astype(bool) & (inside > FACE_MARGIN * unit))


def unify(image: np.ndarray, coarse: np.ndarray, face_box: tuple[float, float, float, float] | None = None) -> Matte | None:
    """Replace the wall behind the sitter by the target colour; None if nothing is safe.

    `coarse` is the segmentation's 0–1 subject probability at `image`'s size; `face_box`
    is (x, y, w, h) in the same coordinates.
    """
    height, width = coarse.shape
    unit = max(height, width)
    I = image.astype(np.float32)

    outside = cv2.distanceTransform((coarse < SURE_BG_ALPHA).astype(np.uint8), cv2.DIST_L2, 5)
    sure_bg = outside > BG_MARGIN * unit
    if sure_bg.mean() < 0.02:
        return None  # no wall left to learn from: refuse rather than guess
    protect = _protected(coarse, face_box, unit) & ~sure_bg
    zone = ~protect & ~sure_bg

    # The original wall, with its own shading, and how noisy it is.
    wall = smooth_fill(I, sure_bg.astype(np.float32), [s * unit for s in (0.012, 0.04, 0.12, 0.4)])
    wall_distance = np.linalg.norm(I - wall, axis=2)
    noise = float(np.median(wall_distance[sure_bg]))
    tolerance = float(np.clip(3.0 * noise, 6.0, 20.0))

    # The coarse mask, snapped onto the image's real edges: soft where hair is soft.
    guide = I / 255.0
    edge_alpha = np.clip(guided_filter(guide, coarse, GUIDE_RADIUS * unit, GUIDE_EPS), 0.0, 1.0)

    # The sitter's own skin (Lab), to recognise ears, neck and cheeks at the outline.
    lab = _lab(I)
    skin = _skin_reference(image, face_box)
    to_skin = np.linalg.norm(lab - skin, axis=2) if skin is not None else np.full(coarse.shape, np.inf, np.float32)

    # The sitter's colour right next to each pixel, learnt from confident subject pixels
    # that do not look like wall — so that a pocket the network got wrong does not teach
    # it the wall's colour, and so that at a cheek it is skin and at a curl it is hair.
    # Skin-coloured pixels always count: on a textured wall the noise tolerance grows
    # large enough to exclude an ear's skin, and the model then knew only hair and wall.
    distinct = (wall_distance > 2.0 * tolerance) | (to_skin < SKIN_DELTA_E)
    subject_weight = ((edge_alpha >= 0.95) & distinct & ~sure_bg).astype(np.float32)
    subject = smooth_fill(I, subject_weight, [s * unit for s in (0.004, 0.012, 0.04, 0.15)])
    subject_distance = np.linalg.norm(I - subject, axis=2)

    # Local two-colour model: a pixel's opacity is where its colour lies between the wall
    # behind it (0) and the sitter beside it (1).  A strand that is half hair, half wall
    # reads 0.5 — the blurry 256-pixel mask said 0.9, which is how 90 % of a half-wall
    # pixel used to reach the output as a bright rim.  A pocket of wall reads 0.
    colour_alpha = _smoothstep(0.25, 0.75, wall_distance / np.maximum(wall_distance + subject_distance, 1e-3))
    colour_alpha[wall_distance <= tolerance] = 0.0
    # Colour is only trusted where sitter and wall differ: dark hair on a light wall,
    # fully; pale skin or a grey jumper on a grey wall, not at all.
    trust = np.clip(
        (np.linalg.norm(subject - wall, axis=2) - CONTRAST_LOW) / (CONTRAST_HIGH - CONTRAST_LOW), 0.0, 1.0,
    )
    # It only ever *lowers* the mask's opacity: it removes wall, it never invents subject.
    refined = trust * np.minimum(edge_alpha, colour_alpha) + (1.0 - trust) * edge_alpha
    # A pixel that matches the wall within the wall's own noise, beside a sitter who is
    # clearly another colour, is wall — fully.  Blending by trust left such pixels at
    # α 0.1–0.25, because the sitter's colour next to a pocket is learnt partly from rim
    # pixels that are themselves mostly wall, which lowers the trust.
    refined[(wall_distance <= tolerance) & (trust >= 0.5)] = 0.0
    # Skin is the third colour the local wall/hair model does not know: an ear between
    # dark hair and a beige wall reads as "closer to the wall than to the hair" and was
    # eaten.  Anything closer to the sitter's own skin than to the wall keeps the mask's
    # opacity — ears are an ANTS criterion, a missing one fails the photo.
    if skin is not None:
        skin_like = (to_skin < SKIN_DELTA_E) & (to_skin < np.linalg.norm(lab - _lab(wall), axis=2))
        refined = np.where(skin_like, edge_alpha, refined)
    alpha = edge_alpha.copy()
    alpha[zone] = refined[zone]
    alpha[protect], alpha[sure_bg] = 1.0, 0.0
    alpha = _drop_specks(alpha, zone)

    # Composite.  Where the sitter is opaque the pixel is kept as is.  Where it is partly
    # transparent its foreground colour is recovered by dividing the wall out of it, and
    # where it is mostly wall the local subject colour stands in, so that the old wall
    # never reaches the output.
    opacity = alpha[..., None]
    unmixed = np.clip(wall + (I - wall) / np.maximum(opacity, 0.05), 0.0, 255.0)
    # Unmixing is trusted from low opacity up: a thin dark curl over a light wall keeps its
    # own colour instead of fading into a ghost of the nearby skin.  Only the faintest
    # pixels, where dividing by α would amplify noise, borrow the local subject colour.
    blend = _smoothstep(0.08, 0.45, alpha)[..., None]
    foreground = blend * unmixed + (1.0 - blend) * subject
    foreground = np.where(opacity >= 0.98, I, foreground)
    out = opacity * foreground + (1.0 - opacity) * TARGET_BGR
    result = np.clip(np.rint(out), 0, 255).astype(np.uint8)
    result[alpha <= 0.0] = np.rint(TARGET_BGR).astype(np.uint8)

    return Matte(
        image=result, alpha=alpha, wall=wall,
        residual_wall=residual_wall(result, wall, zone, zone & (trust >= 0.5), tolerance),
        zone_fraction=float(zone.mean()),
    )


def _skin_reference(image: np.ndarray, face_box: tuple[float, float, float, float] | None) -> np.ndarray | None:
    """The sitter's own skin colour (Lab), from the cheeks-and-nose band of the face box.

    Relative to the person, never a fixed skin range: complexion spans the whole scale.
    """
    if face_box is None:
        return None
    height, width = image.shape[:2]
    x, y, w, h = face_box
    x0, x1 = int(max(0, x + 0.25 * w)), int(min(width, x + 0.75 * w))
    y0, y1 = int(max(0, y + 0.50 * h)), int(min(height, y + 0.68 * h))
    if x1 - x0 < 6 or y1 - y0 < 6:
        return None
    return np.median(_lab(image[y0:y1, x0:x1]).reshape(-1, 3), axis=0)


def _drop_specks(alpha: np.ndarray, zone: np.ndarray) -> np.ndarray:
    """Clear detached subject blobs in the zone: wall texture the colour model took for hair."""
    solid = (alpha > 0.5).astype(np.uint8)
    count, labels, stats, _ = cv2.connectedComponentsWithStats(solid, connectivity=8)
    if count <= 2:
        return alpha
    main = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    limit = SPECK_MAX * alpha.size
    small = [label for label in range(1, count) if label != main and stats[label, cv2.CC_STAT_AREA] < limit]
    if small:
        drop = np.isin(labels, small) & zone
        alpha = alpha.copy()
        alpha[drop] = 0.0
    return alpha


def residual_wall(
    result: np.ndarray, wall: np.ndarray, zone: np.ndarray, judged: np.ndarray, tolerance: float,
) -> float:
    """Share of the refinement zone whose output still looks like the old wall, not the target.

    Counted only where it can be judged: where the old wall differs visibly from the
    target, and where sitter and wall differ enough (`judged`) — a grey babygrow on a
    grey wall looks like the wall because it is the same colour, not because wall leaked.
    """
    if not zone.any():
        return 0.0
    out = result.astype(np.float32)
    to_old = np.linalg.norm(out - wall, axis=2)
    to_target = np.linalg.norm(out - TARGET_BGR, axis=2)
    distinct = np.linalg.norm(wall - TARGET_BGR, axis=2) > 2.0 * tolerance
    leaking = (judged & distinct & (to_old < tolerance) & (to_target > tolerance)).astype(np.uint8)
    # What a reviewer sees as "a darker patch" is an area, not a lone pixel on the
    # outline: an opening keeps blobs and drops scattered single pixels.
    leaking = cv2.morphologyEx(leaking, cv2.MORPH_OPEN, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)))
    return float(leaking.sum()) / float(zone.sum())
