"""Signature cleaning for the submission pipeline.

The extraction itself is *not* reimplemented here.  `signature_validator.py` at the
repository root already carries the tuned pipeline (illumination normalisation, ink
strength map, polarity handling for light pens on dark backgrounds, 4:1 letterboxing)
together with its own report, and it is in production use.  This module only adapts it
to in-memory bytes and translates its report into the dashboard's check list, so that
the two callers — the public page and this microservice — always run the same code.
"""
from __future__ import annotations

import re
import tempfile
from dataclasses import asdict
from pathlib import Path

from PIL import Image

from ..models import FAIL, PASS, Check, ProcessedImage

import signature_validator as validator

MAX_BYTES = 50_000

LABELS = {
    "background_clean": "Fond 100 % blanc",
    "dimensions_ok": f"Dimensions ≥ {validator.MIN_WIDTH}×{validator.MIN_HEIGHT} et ratio 4:1",
    "format_ok": "Format PNG",
    "weight_ok": f"Poids ≤ {MAX_BYTES // 1000} ko",
    "stroke_quality": "Fidélité du tracé",
}


def process(data: bytes, filename: str = "signature.png", max_bytes: int = MAX_BYTES) -> ProcessedImage:
    """Clean one signature and report the five conformity criteria."""
    suffix = Path(filename).suffix.lower()
    if suffix not in validator.SUPPORTED:
        suffix = ".png"
    with tempfile.TemporaryDirectory(prefix="signature-") as temporary:
        work = Path(temporary)
        source_dir, output_dir = work / "in", work / "out"
        source_dir.mkdir()
        source = source_dir / f"signature{suffix}"
        source.write_bytes(data)
        rows = validator.process_files([source], output_dir, margin=6, max_bytes=max_bytes)
        row = asdict(rows[0])
        exported = Path(row["output_file"]) if row["output_file"] else None
        if exported is None or not exported.exists():
            return ProcessedImage(
                data=b"", media_type="image/png", extension=".png", width=0, height=0,
                error=row["failures"] or "Aucune signature détectée",
            )
        cleaned = exported.read_bytes()
        with Image.open(exported) as loaded:
            width, height = loaded.size

    checks = [
        Check(key, LABELS[key], PASS if row[key] == "pass" else FAIL, _detail(key, row))
        for key in LABELS
    ]
    metadata = {
        "processing_mode": row["processing_mode"],
        "inverted": "inverted" in row["processing_mode"].split("+"),
        "presentation_bars_trimmed": "trimmed_bars" in row["processing_mode"].split("+"),
        "raw_density": row["raw_density"],
        "output_density": row["output_density"],
        "raw_components": row["raw_components"],
        "output_components": row["output_components"],
    }
    return ProcessedImage(
        data=cleaned, media_type="image/png", extension=".png",
        width=width, height=height, checks=checks, metadata=metadata,
    )


def _detail(key: str, row: dict) -> str:
    """Surface the validator's own explanation for the criterion that failed.

    Only `stroke_quality` carries a parenthesised reason, and the validator always emits
    it last, so the greedy match to the final bracket keeps reasons that themselves
    contain the `; ` separator intact.
    """
    if row[key] == "pass":
        return ""
    match = re.search(rf"{key}\((.*)\)$", str(row["failures"]))
    return match.group(1) if match else "non conforme"
