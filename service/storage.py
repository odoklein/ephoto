"""On-disk layout for the images of one submission.

Originals are kept next to the processed exports so the reviewer can compare them and
so a rejected file can be re-cropped without asking the customer to upload again.
"""
from __future__ import annotations

import shutil
from pathlib import Path

KINDS = ("photo_original", "photo_clean", "signature_original", "signature_clean")
MEDIA_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff",
}


def folder(root: Path, submission_id: str) -> Path:
    return root / "submissions" / submission_id


def write(root: Path, submission_id: str, kind: str, data: bytes, extension: str) -> Path:
    if kind not in KINDS:
        raise ValueError(f"unknown image kind: {kind}")
    target = folder(root, submission_id)
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{kind}{extension}"
    path.write_bytes(data)
    return path


def find(root: Path, submission_id: str, kind: str) -> Path | None:
    directory = folder(root, submission_id)
    if not directory.is_dir():
        return None
    for path in sorted(directory.glob(f"{kind}.*")):
        return path
    return None


def media_type(path: Path) -> str:
    return MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def purge(root: Path, submission_id: str) -> None:
    shutil.rmtree(folder(root, submission_id), ignore_errors=True)
