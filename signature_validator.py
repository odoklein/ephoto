#!/usr/bin/env python3
"""Clean handwritten signatures and validate local Ephoto-style image criteria.

This utility deliberately uses only geometric transforms (crop, uniform scaling and
letterboxing); it never applies morphology, erosion, dilation or smoothing to ink
pixels.  That makes it suitable for preserving fine strokes while still normalising
the output canvas.

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


def load_bgr(path: Path) -> np.ndarray:
    """Read Unicode Windows paths reliably."""
    raw = np.fromfile(str(path), dtype=np.uint8)
    image = cv2.imdecode(raw, cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("OpenCV could not decode this image")
    return image


def basic_raw_mask(image: np.ndarray) -> np.ndarray:
    """Basic, unfiltered ink candidate used as the raw fidelity baseline."""
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    background = cv2.GaussianBlur(gray, (0, 0), sigmaX=35, sigmaY=35)
    normalised = cv2.divide(gray, background, scale=255)
    _, candidate = cv2.threshold(normalised, 0, 255, cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    # Ink is substantially darker than a photographed sheet. The cap stops broad
    # finger/shadow regions becoming part of the raw-stroke reference.
    dark = gray < min(175, int(np.percentile(gray, 30)))
    return np.where((candidate > 0) & dark, 255, 0).astype(np.uint8)


def ink_mask(image: np.ndarray) -> np.ndarray:
    """Return an 8-bit mask whose white pixels are detected ink.

    A heavily blurred background estimate removes paper illumination and shadows,
    followed by Otsu thresholding of the illumination-normalised luminance.  The
    adaptive threshold is unioned only where it agrees with a genuinely dark pixel,
    which avoids turning paper grain into ink.  No morphology is used.
    """
    mask = basic_raw_mask(image)
    # A one-pixel closing reconnects genuine pen pixels split by paper grain. It is
    # deliberately small and runs before component filtering, never as a broad
    # dilation that would alter the handwriting's shape.
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=1)

    # Keep components that are plausible ink, rejecting tiny paper-speck components.
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    cleaned = np.zeros_like(mask)
    min_area = max(3, image.shape[0] * image.shape[1] // 80_000)
    for label in range(1, count):
        x, y, w, h, area = stats[label]
        if area >= min_area and w >= 2 and h >= 2:
            cleaned[labels == label] = 255
    return cleaned


def crop_and_canvas(mask: np.ndarray, margin: int) -> tuple[np.ndarray, tuple[int, int, int, int]]:
    points = cv2.findNonZero(mask)
    if points is None:
        raise ValueError("No signature ink was detected")
    x, y, w, h = cv2.boundingRect(points)
    x0, y0 = max(0, x - margin), max(0, y - margin)
    x1, y1 = min(mask.shape[1], x + w + margin), min(mask.shape[0], y + h + margin)
    cropped = mask[y0:y1, x0:x1]
    if cropped.shape[0] < 2 or cropped.shape[1] < 2:
        raise ValueError("Detected signature is too small")

    # Preserve the original aspect ratio: scale once, then add white padding to 4:1.
    out_w = MIN_WIDTH
    out_h = max(MIN_HEIGHT, round(out_w / TARGET_RATIO))
    fit = min(out_w / cropped.shape[1], out_h / cropped.shape[0])
    new_w, new_h = max(1, round(cropped.shape[1] * fit)), max(1, round(cropped.shape[0] * fit))
    resized = cv2.resize(cropped, (new_w, new_h), interpolation=cv2.INTER_LANCZOS4)
    canvas = np.zeros((out_h, out_w), dtype=np.uint8)  # white ink-mask background = 0
    left, top = (out_w - new_w) // 2, (out_h - new_h) // 2
    canvas[top : top + new_h, left : left + new_w] = resized
    return 255 - canvas, (x0, y0, x1, y1)  # conventional image: black ink on white


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


def evaluate(name: str, output: Path, raw_mask: np.ndarray, raw_box: tuple[int, int, int, int], max_bytes: int) -> ReportRow:
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
    density_ok = raw_density > 0 and output_density >= raw_density * (1 - DENSITY_LOSS_LIMIT)
    # Any additional component means a formerly continuous trace was broken.
    fragments_ok = output_components <= raw_components
    # Valid stroke requires solid dark detail plus density and continuity fidelity.
    stroke_ok = bool(ink.sum() >= 20 and arr.min() <= 40 and density_ok and fragments_ok)
    checks = [background_ok, dimensions_ok, fmt_ok, weight_ok, stroke_ok]
    labels = ["background_clean", "dimensions_ok", "format_ok", "weight_ok", "stroke_quality"]
    failures = [label for label, passed in zip(labels, checks) if not passed]
    return ReportRow(name, *("pass" if c else "fail" for c in checks), round(100 * sum(checks) / len(checks)), ";".join(failures), str(output), raw_density, output_density, raw_components, output_components)


def process_files(inputs: Iterable[Path], output_dir: Path, margin: int, max_bytes: int) -> list[ReportRow]:
    output_dir.mkdir(parents=True, exist_ok=True)
    rows: list[ReportRow] = []
    for source in inputs:
        target = output_dir / f"{source.stem}_ephoto.png"
        try:
            image = load_bgr(source)
            raw = basic_raw_mask(image)
            result, raw_box = crop_and_canvas(ink_mask(image), margin)
            target.write_bytes(png_bytes(result))
            rows.append(evaluate(source.name, target, raw, raw_box, max_bytes))
        except Exception as exc:
            rows.append(ReportRow(source.name, "fail", "fail", "fail", "fail", "fail", 0, str(exc), "", None, None, None, None))
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
    candidates = sorted(p for p in args.input.iterdir() if p.is_file())
    files = [p for p in candidates if p.suffix.lower() in SUPPORTED]
    rows = process_files(files, args.output, args.margin, args.max_bytes)
    # Never silently omit a supplied file: unsupported source formats stay visible.
    for source in candidates:
        if source not in files:
            rows.append(ReportRow(source.name, "fail", "fail", "fail", "fail", "fail", 0, "unsupported_input_format", "", None, None, None, None))
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
