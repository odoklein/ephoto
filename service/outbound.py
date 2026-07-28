"""Outgoing webhook to Make.com / Ephoto.io once a reviewer accepts a submission."""
from __future__ import annotations

import base64
from pathlib import Path

import httpx

from . import storage
from .config import settings


def _encoded(root: Path, submission_id: str, kind: str) -> dict | None:
    path = storage.find(root, submission_id, kind)
    if path is None:
        return None
    return {
        "filename": path.name,
        "media_type": storage.media_type(path),
        "base64": base64.b64encode(path.read_bytes()).decode("ascii"),
    }


def build_payload(record: dict, reviewer: str) -> dict:
    """Images travel inline as base64: no public URL is minted for identity documents."""
    root = settings.storage_dir
    return {
        "submission_id": record["id"],
        "source_ref": record["source_ref"],
        "customer": record["customer"],
        "reviewer": reviewer,
        "decided_at": record["decided_at"],
        "photo": _encoded(root, record["id"], "photo_clean"),
        "signature": _encoded(root, record["id"], "signature_clean"),
        "reports": {
            "photo": record["photo_report"],
            "signature": record["signature_report"],
        },
    }


def send(payload: dict) -> tuple[bool, str]:
    """POST the accepted files onward. Returns (delivered, human-readable status)."""
    if not settings.outbound_enabled:
        return False, "MAKE_WEBHOOK_URL non configuré"
    headers = {"Content-Type": "application/json"}
    if settings.outbound_token:
        headers["Authorization"] = f"Bearer {settings.outbound_token}"
    try:
        response = httpx.post(
            settings.outbound_url, json=payload, headers=headers, timeout=settings.outbound_timeout
        )
    except httpx.HTTPError as error:  # network, DNS, timeout
        return False, f"échec réseau: {error.__class__.__name__}"
    if response.is_success:
        return True, f"transmis ({response.status_code})"
    return False, f"refusé par Make ({response.status_code})"


def fetch(url: str, max_bytes: int) -> bytes:
    """Download a source image referenced by URL, with a hard size ceiling."""
    if not url.lower().startswith(("http://", "https://")):
        raise ValueError("URL non supportée")
    with httpx.stream("GET", url, timeout=30, follow_redirects=True) as response:
        response.raise_for_status()
        chunks, total = [], 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("Fichier distant trop volumineux")
            chunks.append(chunk)
    return b"".join(chunks)
