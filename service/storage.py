"""On-disk layout for the images of one submission.

Originals are kept next to the processed exports so the reviewer can compare them and
so a rejected file can be re-cropped without asking the customer to upload again.  They
are also what makes processing restartable: a dossier still `processing` after a crash
is re-run from the originals written at intake.
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
import time
from pathlib import Path

KINDS = ("photo_original", "photo_clean", "signature_original", "signature_clean")
MEDIA_TYPES = {
    ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
    ".webp": "image/webp", ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff",
}
# secrets.token_hex(8): anything else is not one of ours, and is never joined to a path.
SUBMISSION_ID = re.compile(r"^[0-9a-f]{16}$")


def valid_id(submission_id: str) -> bool:
    return bool(SUBMISSION_ID.match(submission_id or ""))


def folder(root: Path, submission_id: str) -> Path:
    if not valid_id(submission_id):
        raise ValueError(f"invalid submission id: {submission_id!r}")
    return root / "submissions" / submission_id


def write(root: Path, submission_id: str, kind: str, data: bytes, extension: str) -> Path:
    """Replace the file of this kind atomically.

    The bytes go to a temporary file in the same folder and are renamed over the target,
    so a reader — the reviewer's browser, the outbound webhook — sees either the old
    image or the new one, never a half-written JPEG.  Any older file of the same kind
    with a different extension is removed, otherwise find() could keep returning it.
    """
    if kind not in KINDS:
        raise ValueError(f"unknown image kind: {kind}")
    target = folder(root, submission_id)
    target.mkdir(parents=True, exist_ok=True)
    path = target / f"{kind}{extension}"
    descriptor, temporary = tempfile.mkstemp(prefix=f".{kind}-", dir=target)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    for stale in target.glob(f"{kind}.*"):
        if stale != path:
            stale.unlink(missing_ok=True)
    return path


def find(root: Path, submission_id: str, kind: str) -> Path | None:
    if kind not in KINDS or not valid_id(submission_id):
        return None
    directory = folder(root, submission_id)
    if not directory.is_dir():
        return None
    for path in sorted(directory.glob(f"{kind}.*")):
        return path
    return None


def media_type(path: Path) -> str:
    return MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")


def purge(root: Path, submission_id: str) -> None:
    if valid_id(submission_id):
        shutil.rmtree(folder(root, submission_id), ignore_errors=True)


def orphans(root: Path, known: set[str], min_age_seconds: float = 3600) -> list[str]:
    """Folders on disk with no database row — left by a crash or a manual DB edit.

    The age floor matters: intake creates the row before the folder, but a folder
    listed here a moment after a row was read would otherwise be taken for an orphan.
    """
    base = root / "submissions"
    if not base.is_dir():
        return []
    cutoff = time.time() - min_age_seconds
    found = []
    for entry in base.iterdir():
        if entry.is_dir() and valid_id(entry.name) and entry.name not in known:
            try:
                if entry.stat().st_mtime < cutoff:
                    found.append(entry.name)
            except OSError:
                continue
    return found
