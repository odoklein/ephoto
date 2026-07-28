#!/usr/bin/env python3
"""Clean handwritten signatures and validate local Ephoto-style image criteria.

Ink is extracted as a continuous *strength* map rather than a hard binary stencil, so
the exported signature keeps its natural anti-aliased edges instead of collapsing into
a harsh one-bit line.  The only structural operation applied to ink is a single 3x3
closing that reconnects micro-gaps; there is no erosion, no dilation and no smoothing
of the handwriting shape.  Geometry is limited to crop, uniform scaling and
letterboxing, so strokes are never stretched.

The measurable checks in this program are local technical checks, not a guarantee of
acceptance by ANTS or certif-idphoto.fr, whose server-side rules may change.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np
from PIL import Image

try:  # BioGaze has no stable public Python image-processing API, so this is a check.
    import biogaze  # type: ignore  # noqa: F401
    BIOGAZE_AVAILABLE = True
except ImportError:
    BIOGAZE_AVAILABLE = False

SUPPORTED = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
MIN_WIDTH, MIN_HEIGHT = 521, 134
TARGET_RATIO = 4.0
# 521×134 is itself 3.888:1 (2.9% from exact 4:1), so tolerance is relative.
RATIO_TOLERANCE = 0.05
MAX_BYTES = 50_000  # deliberately conservative; override with --max-bytes if needed
DENSITY_LOSS_LIMIT = 0.30
# Extraction modes are tried in this order: the local one first because it survives
# shadows and uneven lighting, then progressively safer global fallbacks.  "triangle"
# is the rescue mode for very faint ink, where Otsu cuts far too high.
THRESHOLD_MODES = ("adaptive", "otsu", "triangle")
# A mode is accepted as soon as it retains this share of the raw ink baseline.
FALLBACK_DENSITY_RATIO = 0.90
# Ink must be at least this dark relative to the darkest ink of the same sheet before
# it is considered a stroke; it keeps paper grain out without cutting faint pens.
INK_FLOOR_RATIO = 0.20
# A stroke narrower than this in the exported PNG is no longer a readable pen trace.
MIN_OUTPUT_STROKE_PX = 1.15
# How far the 4:1 canvas may grow past 521×134 to keep thin strokes above that width.
MAX_CANVAS_SCALE = 2.0
# Longest side used when deciding whether a sheet is light ink on a dark background.
POLARITY_SAMPLE_PX = 512


@dataclass
class Geometry:
    """Scaling facts needed to judge whether the 4:1 export preserved the strokes."""

    scale: float
    source_stroke_px: float

    @property
    def output_stroke_px(self) -> float:
        return self.source_stroke_px * self.scale


@dataclass
class ReportRow:
    filename: str
    background_clean: str
    dimensions_ok: str
    format_ok: str
    weight_ok: str
    stroke_quality: str
    global_score: int
    failures: str
    output_file: str
    raw_density: float | None
    output_density: float | None
    raw_components: int | None
    output_components: int | None
    processing_mode: str = ""


def load_bgr(path: Path) -> np.ndarray:
    """Read Unicode Windows paths reliably."""
    raw = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(raw, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("OpenCV could not decode this image")
    return image


def normalise_polarity(image: np.ndarray) -> tuple[np.ndarray, bool]:
    """Return the sheet with ink darker than its background, and whether it was flipped.

    A white pen on a black sheet is the same handwriting with reversed contrast, so the
    whole extraction pipeline can stay polarity-agnostic by flipping such sheets once,
    here, instead of duplicating every threshold.  The decision is made on how far each
    tail departs from the *local* background rather than from the overall level: a blur
    wide enough to step over the strokes follows shadows and lighting gradients, which
    would otherwise outweigh a faint pen and decide the polarity on their own.  Whichever
    tail departs further is the ink, so a brighter one means a reversed sheet.  The test
    runs on a downscaled copy for speed, both tails are measured the same way, and
    inverting is exact (255 - v), so the call is symmetric, lossless and idempotent: a
    flipped sheet reads as normal on the second pass.
    """
    grey = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    scale = POLARITY_SAMPLE_PX / max(grey.shape[:2])
    if scale < 1:
        grey = cv2.resize(grey, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    grey = grey.astype(np.float32)
    local = cv2.GaussianBlur(grey, (0, 0), sigmaX=max(grey.shape[:2]) / 24.0)
    residual = grey - local
    darker = -float(np.percentile(residual, 0.5))
    lighter = float(np.percentile(residual, 99.5))
    if lighter > darker:
        return cv2.bitwise_not(image), True
    return image, False


def ink_strength(image: np.ndarray) -> np.ndarray:
    """Return a float ink map in 0..1: 0 is paper, 1 is the darkest ink of the sheet.

    Reverse-contrast sheets are flipped first, so the rest of this function always sees
    dark ink on light paper.  The darkest colour channel is then used instead of
    luminance so that blue or light coloured pens keep their contrast — on a flipped
    sheet that same minimum picks the brightest original channel, which is what a white
    or pale pen writes with.  Paper illumination is estimated with a large
    greyscale closing, which follows shadows, paper texture gradients and fingers in
    frame while stepping over the thin dark strokes themselves; dividing by it removes
    that illumination without the halos a blurred estimate leaves around ink.  The
    result is finally rescaled against the sheet's own ink peak, so faint low-contrast
    pens end up on the same scale as strong ones.
    """
    image, _ = normalise_polarity(image)
    darkest = np.min(image, axis=2)
    # Light denoise: removes sensor and paper grain, keeps stroke edges intact.
    smoothed = cv2.bilateralFilter(darkest, 5, 25, 25)
    size = max(15, (min(smoothed.shape[:2]) // 8) | 1)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    background = cv2.morphologyEx(smoothed, cv2.MORPH_CLOSE, kernel)
    background = cv2.GaussianBlur(background, (0, 0), sigmaX=size / 6.0, sigmaY=size / 6.0)
    ratio = smoothed.astype(np.float32) / np.maximum(background.astype(np.float32), 1.0)
    strength = np.clip(1.0 - ratio, 0.0, 1.0)

    paper = float(np.percentile(strength, 75))  # typical residual noise level
    peak = float(np.percentile(strength, 99.9))  # robust stand-in for the darkest ink
    if peak - paper < 0.01:  # nothing on this sheet is meaningfully darker than paper
        return np.zeros_like(strength)
    return np.clip((strength - paper) / (peak - paper), 0.0, 1.0)


def drop_specks(mask: np.ndarray, area: int) -> np.ndarray:
    """Remove paper specks while keeping thin, one-pixel-wide stroke fragments."""
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    cleaned = np.zeros_like(mask)
    min_area = max(3, area // 120_000)
    for label in range(1, count):
        _, _, w, h, blob = stats[label]
        # `or` rather than `and`: a purely horizontal hairline is 1 pixel tall and is
        # still ink, whereas a lone speck fails both extents.
        if blob >= min_area and (w >= 2 or h >= 2):
            cleaned[labels == label] = 255
    return cleaned


def threshold_ink(strength: np.ndarray, mode: str) -> np.ndarray:
    """Binarise the ink strength map with the requested, deliberately gentle method."""
    scaled = np.clip(strength * 255.0, 0, 255).astype(np.uint8)
    floor = np.uint8(round(255 * INK_FLOOR_RATIO))
    if mode == "adaptive":
        block = max(31, (min(scaled.shape[:2]) // 8) | 1)
        # Negative C means "above the local mean by 6/255", a low bar that keeps faint
        # and thin ink that a global cut would erase, while ignoring lighting drift.
        local = cv2.adaptiveThreshold(
            scaled, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, block, -6
        )
        return np.where((local > 0) & (scaled >= floor), 255, 0).astype(np.uint8)
    if mode == "otsu":
        _, mask = cv2.threshold(scaled, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    else:  # "triangle": the safe fallback, it cuts low and rescues very faint ink.
        _, mask = cv2.threshold(scaled, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_TRIANGLE)
    return np.where((mask > 0) & (scaled >= floor // 2), 255, 0).astype(np.uint8)


def basic_raw_mask(image: np.ndarray, strength: np.ndarray | None = None) -> np.ndarray:
    """Unfiltered ink candidate used as the raw fidelity baseline.

    Otsu on the illumination-normalised strength is an honest "obvious ink" reference:
    neither so tight that the cleaned output passes trivially, nor so loose that paper
    grain sets the bar.  No morphology is applied here.
    """
    if strength is None:
        strength = ink_strength(image)
    mask = threshold_ink(strength, "otsu")
    return drop_specks(mask, image.shape[0] * image.shape[1])


def ink_mask(image: np.ndarray, strength: np.ndarray | None = None, raw: np.ndarray | None = None) -> tuple[np.ndarray, str]:
    """Return the cleaned ink mask and the extraction mode that produced it.

    Each mode is scored against the raw baseline density and the first one that keeps
    enough ink wins; if none does, the densest candidate is used rather than silently
    exporting the thinnest one.
    """
    if strength is None:
        strength = ink_strength(image)
    if raw is None:
        raw = basic_raw_mask(image, strength)
    target = mask_stats(raw)[0] * FALLBACK_DENSITY_RATIO
    best: tuple[float, np.ndarray, str] | None = None
    for mode in THRESHOLD_MODES:
        mask = threshold_ink(strength, mode)
        # A one-pixel closing reconnects genuine pen pixels split by paper grain. It is
        # deliberately small and runs before component filtering, never as a broad
        # dilation that would alter the handwriting's shape.
        mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)
        mask = drop_specks(mask, image.shape[0] * image.shape[1])
        density = mask_stats(mask)[0]
        if best is None or density > best[0]:
            best = (density, mask, mode)
        if density >= target and density > 0:
            return mask, mode
    assert best is not None
    return best[1], best[2]


def stroke_width(mask: np.ndarray) -> float:
    """Estimate the pen width in pixels from the ink distance transform."""
    if not np.any(mask):
        return 0.0
    distance = cv2.distanceTransform(mask, cv2.DIST_L2, 3)
    return 2.0 * float(np.percentile(distance[mask > 0], 90))


def canvas_size(scale_needed: float) -> tuple[int, int]:
    """Smallest 4:1 canvas, at or above 521×134, that avoids crushing thin strokes."""
    base_w = MIN_WIDTH
    base_h = max(MIN_HEIGHT, round(base_w / TARGET_RATIO))
    grow = min(max(scale_needed, 1.0), MAX_CANVAS_SCALE)
    return round(base_w * grow), round(base_h * grow)


def crop_and_canvas(mask: np.ndarray, strength: np.ndarray, margin: int) -> tuple[np.ndarray, tuple[int, int, int, int], Geometry]:
    points = cv2.findNonZero(mask)
    if points is None:
        raise ValueError("No signature ink was detected")
    x, y, w, h = cv2.boundingRect(points)
    x0, y0 = max(0, x - margin), max(0, y - margin)
    x1, y1 = min(mask.shape[1], x + w + margin), min(mask.shape[0], y + h + margin)
    cropped_mask = mask[y0:y1, x0:x1]
    if cropped_mask.shape[0] < 2 or cropped_mask.shape[1] < 2:
        raise ValueError("Detected signature is too small")
    # Anti-aliased ink: the strength map keeps the soft pen edges, the mask decides
    # what counts as ink at all. The product is never thresholded again.
    ink = strength[y0:y1, x0:x1] * (cropped_mask > 0)

    pen = stroke_width(cropped_mask)
    # Preserve the original aspect ratio: scale once, then add white padding to 4:1.
    out_w, out_h = canvas_size(1.0)
    fit = min(out_w / cropped_mask.shape[1], out_h / cropped_mask.shape[0])
    if pen > 0 and fit < 1:
        # Grow the (still 4:1) canvas just enough to keep the pen above one pixel.
        out_w, out_h = canvas_size(MIN_OUTPUT_STROKE_PX / (pen * fit))
        fit = min(out_w / cropped_mask.shape[1], out_h / cropped_mask.shape[0])

    new_w = max(1, round(cropped_mask.shape[1] * fit))
    new_h = max(1, round(cropped_mask.shape[0] * fit))
    # INTER_AREA integrates ink coverage when shrinking, so a thin stroke fades evenly
    # instead of dropping out of the sampling grid the way Lanczos or nearest do.
    interpolation = cv2.INTER_AREA if fit < 1 else cv2.INTER_CUBIC
    resized = np.clip(cv2.resize(ink, (new_w, new_h), interpolation=interpolation), 0.0, 1.0)

    visible = resized[resized > 0.05]
    if visible.size:
        # Coverage averaging dilutes narrow strokes; rescaling against the stroke core
        # restores solid black ink without widening or re-binarising the handwriting.
        resized = np.clip(resized / max(float(np.percentile(visible, 92)), 0.25), 0.0, 1.0)
    resized[resized < 0.05] = 0.0  # guarantees a pure white, speck-free background

    canvas = np.zeros((out_h, out_w), dtype=np.float32)  # 0 = no ink
    left, top = (out_w - new_w) // 2, (out_h - new_h) // 2
    canvas[top : top + new_h, left : left + new_w] = resized
    image = (255.0 - canvas * 255.0).round().astype(np.uint8)  # black ink on white
    return image, (x0, y0, x1, y1), Geometry(fit, pen)


def png_bytes(image: np.ndarray) -> bytes:
    pil = Image.fromarray(image, mode="L")
    stream = io.BytesIO()
    pil.save(stream, format="PNG", optimize=True, compress_level=9)
    return stream.getvalue()


def mask_stats(mask: np.ndarray) -> tuple[float, int]:
    points = cv2.findNonZero(mask)
    if points is None:
        return 0.0, 0
    x, y, width, height = cv2.boundingRect(points)
    crop = mask[y : y + height, x : x + width]
    density = float(np.count_nonzero(crop)) / crop.size
    count, _, stats, _ = cv2.connectedComponentsWithStats(crop, connectivity=8)
    min_area = max(3, crop.size // 20_000)
    components = sum(stats[label, cv2.CC_STAT_AREA] >= min_area for label in range(1, count))
    return density, int(components)


def evaluate(name: str, output: Path, raw_mask: np.ndarray, raw_box: tuple[int, int, int, int], max_bytes: int, mode: str, geometry: Geometry) -> ReportRow:
    data = output.read_bytes()
    with Image.open(output) as loaded:
        fmt_ok = loaded.format == "PNG"
        width, height = loaded.size
        arr = np.asarray(loaded.convert("L"))
    bg = arr[arr > 245]
    background_ok = bool(bg.size and np.percentile(bg, 1) >= 250)
    dimensions_ok = (
        width >= MIN_WIDTH
        and height >= MIN_HEIGHT
        and abs((width / height) / TARGET_RATIO - 1) <= RATIO_TOLERANCE
    )
    weight_ok = len(data) <= max_bytes
    ink = (arr < 200).astype(np.uint8) * 255
    x0, y0, x1, y1 = raw_box
    raw_density, raw_components = mask_stats(raw_mask[y0:y1, x0:x1])
    output_density, output_components = mask_stats(ink)

    reasons: list[str] = []
    if ink.sum() < 20 or arr.min() > 40:
        reasons.append("no solid dark ink in the export")
    if not (raw_density > 0 and output_density >= raw_density * (1 - DENSITY_LOSS_LIMIT)):
        reasons.append(f"ink density {output_density:.4f} below 70% of raw {raw_density:.4f}")
    # Any additional component means a formerly continuous trace was broken.
    if output_components > raw_components:
        reasons.append(f"trace broken into {output_components} parts vs {raw_components} raw")
    # A signature that cannot reach the 4:1 canvas without going sub-pixel is reported
    # rather than exported as a degraded hairline.
    if geometry.source_stroke_px > 0 and geometry.output_stroke_px < MIN_OUTPUT_STROKE_PX:
        reasons.append(f"strokes reduced to {geometry.output_stroke_px:.2f}px by the 4:1 canvas")
    stroke_ok = not reasons

    checks = [background_ok, dimensions_ok, fmt_ok, weight_ok, stroke_ok]
    labels = ["background_clean", "dimensions_ok", "format_ok", "weight_ok", "stroke_quality"]
    failures = [label for label, passed in zip(labels, checks) if not passed]
    if not stroke_ok:
        failures[failures.index("stroke_quality")] = f"stroke_quality({'; '.join(reasons)})"
    return ReportRow(name, *("pass" if c else "fail" for c in checks), round(100 * sum(checks) / len(checks)), ";".join(failures), str(output), raw_density, output_density, raw_components, output_components, mode)


def process_files(inputs: Iterable[Path], output_dir: Path, margin: int, max_bytes: int) -> list[ReportRow]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[ReportRow] = []
    for source in inputs:
        target = output_dir / f"{source.stem}_ephoto.png"
        try:
            image, inverted = normalise_polarity(load_bgr(source))
            strength = ink_strength(image)
            raw = basic_raw_mask(image, strength)
            mask, mode = ink_mask(image, strength, raw)
            result, raw_box, geometry = crop_and_canvas(mask, strength, margin)
            target.write_bytes(png_bytes(result))
            # The export is black on white either way; the report still says which
            # sheets arrived as light ink on a dark background.
            label = f"{mode}+inverted" if inverted else mode
            rows.append(evaluate(source.name, target, raw, raw_box, max_bytes, label, geometry))
        except Exception as exc:
            rows.append(ReportRow(source.name, "fail", "fail", "fail", "fail", "fail", 0, str(exc), "", None, None, None, None, "error"))
    return rows


def write_reports(rows: list[ReportRow], report_dir: Path) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    payload = [asdict(row) for row in rows]
    (report_dir / "signature_report.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    with (report_dir / "signature_report.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(asdict(ReportRow("", "", "", "", "", "", 0, "", "", None, None, None, None)).keys()))
        writer.writeheader()
        writer.writerows(payload)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=Path("input"), help="Folder containing raw signatures")
    parser.add_argument("--output", type=Path, default=Path("output"), help="Folder for cleaned PNG files")
    parser.add_argument("--report-dir", type=Path, default=Path("reports"), help="Folder for JSON and CSV")
    parser.add_argument("--margin", type=int, default=6, help="Crop margin in input pixels")
    parser.add_argument("--max-bytes", type=int, default=MAX_BYTES, help="Maximum encoded PNG bytes")
    parser.add_argument("--require-biogaze", action="store_true", help="Fail early unless the biogaze package is installed")
    args = parser.parse_args()
    if args.require_biogaze and not BIOGAZE_AVAILABLE:
        print("ERROR: biogaze is not installed. Install dependencies from requirements.txt.", file=sys.stderr)
        return 2
    if not args.input.is_dir():
        print(f"ERROR: input folder does not exist: {args.input}", file=sys.stderr)
        return 2
    # Repository placeholder files (for example `.gitkeep`) are not submitted
    # signatures and must not appear as failed sample results.
    candidates = sorted(p for p in args.input.iterdir() if p.is_file() and not p.name.startswith("."))
    files = [p for p in candidates if p.suffix.lower() in SUPPORTED]
    rows = process_files(files, args.output, args.margin, args.max_bytes)
    # Never silently omit a supplied file: unsupported source formats stay visible.
    for source in candidates:
        if source not in files:
            rows.append(ReportRow(source.name, "fail", "fail", "fail", "fail", "fail", 0, "unsupported_input_format", "", None, None, None, None, "unsupported"))
    rows.sort(key=lambda row: row.filename.casefold())
    write_reports(rows, args.report_dir)
    compliant = sum(not r.failures for r in rows)
    total = len(rows)
    rate = 100 * compliant / total if total else 0
    print(f"BioGaze available: {'yes' if BIOGAZE_AVAILABLE else 'no'}")
    print(f"Fully compliant: {compliant}/{total} ({rate:.1f}%)")
    for row in rows:
        if row.failures:
            print(f"FAIL {row.filename}: {row.failures}")
    return 0 if total and compliant == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
