"""HTTP in both directions: the accepted-file webhook out, and source downloads in."""
from __future__ import annotations

import base64
import ipaddress
import socket
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx

from . import storage
from .config import settings

MAX_REDIRECTS = 3
FETCH_TIMEOUT = httpx.Timeout(20.0, connect=5.0)
# Shops and CDNs label images inconsistently, but an HTML page — a login wall, an expired
# share link — is never a photograph and is refused before it reaches the decoder.
REFUSED_CONTENT_TYPES = ("text/", "application/json", "application/xml", "application/xhtml")


class FetchError(ValueError):
    """A source URL that could not, or must not, be downloaded."""


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
    """POST the accepted files onward. Returns (delivered, human-readable status).

    The submission id doubles as an idempotency key: if Make answered after our timeout,
    the reviewer's retry carries the same key and the scenario can drop the duplicate
    instead of ordering a second ePhoto.
    """
    if not settings.outbound_enabled:
        return False, "MAKE_WEBHOOK_URL non configuré"
    headers = {
        "Content-Type": "application/json",
        "Idempotency-Key": str(payload.get("submission_id", "")),
        "X-Submission-Id": str(payload.get("submission_id", "")),
    }
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


# ── Source downloads ────────────────────────────────────────────────────────────────
def _host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    return not allowed or any(host == item or host.endswith("." + item) for item in allowed)


def check_url(url: str, allowed_hosts: tuple[str, ...] = ()) -> None:
    """Refuse URLs the service must not fetch on a caller's behalf.

    The intake downloads whatever URL the order carries, and order fields are partly
    typed by customers.  Without this, `http://169.254.169.254/…` or an admin port on
    the Docker network is one form field away.  Every address the name resolves to must
    be public; FETCH_ALLOWED_HOSTS narrows it further to the shop's own domains.

    The name is resolved again when httpx connects, so a hostile DNS server could still
    answer differently the second time; the host allowlist is the control that closes it.
    """
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise FetchError("URL non supportée (http ou https attendu)")
    host = parts.hostname.lower().rstrip(".")
    if not _host_allowed(host, allowed_hosts):
        raise FetchError(f"hôte {host} hors de FETCH_ALLOWED_HOSTS")
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80))
    except (socket.gaierror, UnicodeError) as error:
        raise FetchError(f"hôte {host} introuvable") from error
    for info in infos:
        address = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
            address = address.ipv4_mapped
        if not address.is_global:
            raise FetchError(f"hôte {host} résout vers une adresse non publique ({address})")


def fetch(url: str, max_bytes: int, allowed_hosts: tuple[str, ...] | None = None) -> bytes:
    """Download a source image referenced by URL, with a hard size ceiling.

    Redirects are followed by hand so that every hop goes through check_url(): letting
    httpx follow them would vet the first address and trust wherever it points next.
    """
    allowed = settings.fetch_allowed_hosts if allowed_hosts is None else allowed_hosts
    current = url
    with httpx.Client(timeout=FETCH_TIMEOUT, follow_redirects=False) as client:
        for _ in range(MAX_REDIRECTS + 1):
            check_url(current, allowed)
            try:
                with client.stream("GET", current) as response:
                    if response.is_redirect:
                        location = response.headers.get("location", "")
                        if not location:
                            raise FetchError("redirection sans destination")
                        current = urljoin(current, location)
                        continue
                    if not response.is_success:
                        raise FetchError(f"téléchargement refusé ({response.status_code})")
                    content_type = response.headers.get("content-type", "").lower()
                    if content_type.startswith(REFUSED_CONTENT_TYPES):
                        raise FetchError(f"le lien ne renvoie pas une image ({content_type.split(';')[0]})")
                    declared = response.headers.get("content-length", "")
                    if declared.isdigit() and int(declared) > max_bytes:
                        raise FetchError("fichier distant trop volumineux")
                    chunks, total = [], 0
                    for chunk in response.iter_bytes():
                        total += len(chunk)
                        if total > max_bytes:
                            raise FetchError("fichier distant trop volumineux")
                        chunks.append(chunk)
                    return b"".join(chunks)
            except httpx.HTTPError as error:
                raise FetchError(f"téléchargement impossible ({error.__class__.__name__})") from error
    raise FetchError("trop de redirections")
