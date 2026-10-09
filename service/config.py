"""Runtime configuration, read once from the environment.

Every secret fails closed: an unset key disables the endpoint that needs it instead of
falling back to an open default, because this service handles identity photographs.

Keys are scoped.  INGEST_API_KEY lives in Make scenarios and lets a caller deposit and
read dossiers; REVIEW_API_KEY lets a server (the WordPress plugin) act as a reviewer —
accept, reject, re-crop, download the images.  A leaked Make scenario therefore cannot
approve an identity file.  Setting both to the same value restores the old single-key
behaviour, deliberately and visibly.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FLATTEN_MODES = ("auto", "always", "never")
log = logging.getLogger("ephoto.config")


def _int(name: str, default: int, minimum: int | None = None) -> int:
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError:
        log.warning("%s=%r is not an integer; using %s", name, raw, default)
        value = default
    return max(value, minimum) if minimum is not None else value


def _float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    try:
        return float(raw) if raw else default
    except ValueError:
        log.warning("%s=%r is not a number; using %s", name, raw, default)
        return default


def _bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def _list(name: str) -> tuple[str, ...]:
    return tuple(item.strip().lower() for item in os.environ.get(name, "").split(",") if item.strip())


@dataclass(frozen=True)
class Settings:
    storage_dir: Path
    database_path: Path
    ingest_api_key: str
    review_api_key: str
    admin_user: str
    admin_password: str
    outbound_url: str
    outbound_token: str
    outbound_timeout: float
    max_upload_bytes: int
    # A 15 MB JPEG can still decode to tens of gigapixels; the budget is checked from the
    # header before any pixel is allocated.
    max_image_pixels: int
    purge_after_days: int
    # Undecided dossiers are kept until someone decides them unless this is set: deleting
    # a customer's file nobody looked at is a business decision, not a default.
    purge_open_after_days: int
    flatten_background: str  # auto | always | never
    public_base_url: str
    # Images are CPU- and memory-heavy: this bounds how many are processed at once.
    processing_workers: int
    # Hosts the intake may download from when Make sends URLs; empty = any public host.
    fetch_allowed_hosts: tuple[str, ...]
    # Deployment expects MediaPipe: /api/health reports "degraded" without it.
    require_face_mesh: bool
    log_level: str
    warnings: tuple[str, ...] = field(default=(), compare=False)

    @property
    def ingest_enabled(self) -> bool:
        return bool(self.ingest_api_key)

    @property
    def review_key_enabled(self) -> bool:
        return bool(self.review_api_key)

    @property
    def admin_enabled(self) -> bool:
        return bool(self.admin_user and self.admin_password)

    @property
    def outbound_enabled(self) -> bool:
        return bool(self.outbound_url)


def load_settings() -> Settings:
    storage = Path(os.environ.get("STORAGE_DIR", ROOT / "storage")).resolve()
    warnings: list[str] = []

    flatten = os.environ.get("FLATTEN_BACKGROUND", "auto").strip().lower()
    if flatten not in FLATTEN_MODES:
        warnings.append(f"FLATTEN_BACKGROUND={flatten!r} inconnu ; « auto » utilisé")
        flatten = "auto"

    public_base_url = os.environ.get("PUBLIC_BASE_URL", "").strip().rstrip("/")
    outbound_url = os.environ.get("MAKE_WEBHOOK_URL", "").strip()
    if outbound_url and not outbound_url.lower().startswith("https://"):
        warnings.append("MAKE_WEBHOOK_URL n'est pas en HTTPS : les images d'identité circuleraient en clair")

    ingest_key = os.environ.get("INGEST_API_KEY", "").strip()
    review_key = os.environ.get("REVIEW_API_KEY", "").strip()
    if ingest_key and review_key and ingest_key == review_key:
        warnings.append("REVIEW_API_KEY = INGEST_API_KEY : la clé Make peut aussi valider des dossiers")
    for name, value in (("INGEST_API_KEY", ingest_key), ("REVIEW_API_KEY", review_key)):
        if value and len(value) < 24:
            warnings.append(f"{name} fait moins de 24 caractères")

    return Settings(
        storage_dir=storage,
        database_path=storage / "ephoto.sqlite3",
        ingest_api_key=ingest_key,
        review_api_key=review_key,
        admin_user=os.environ.get("ADMIN_USER", "").strip(),
        admin_password=os.environ.get("ADMIN_PASSWORD", "").strip(),
        outbound_url=outbound_url,
        outbound_token=os.environ.get("MAKE_WEBHOOK_TOKEN", "").strip(),
        outbound_timeout=_float("MAKE_WEBHOOK_TIMEOUT", 20.0),
        max_upload_bytes=_int("MAX_UPLOAD_BYTES", 15 * 1024 * 1024, minimum=1024),
        max_image_pixels=_int("MAX_IMAGE_PIXELS", 60_000_000, minimum=1_000_000),
        # Identity photographs are personal data: they are not kept indefinitely.
        purge_after_days=_int("PURGE_AFTER_DAYS", 30),
        purge_open_after_days=_int("PURGE_OPEN_AFTER_DAYS", 0),
        # Preserve already-compliant studio photos.  The processor still replaces a
        # background that is visibly non-uniform, too dark or effectively white.
        flatten_background=flatten,
        public_base_url=public_base_url,
        processing_workers=_int("PROCESSING_WORKERS", 2, minimum=1),
        fetch_allowed_hosts=_list("FETCH_ALLOWED_HOSTS"),
        require_face_mesh=_bool("REQUIRE_FACE_MESH", False),
        log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
        warnings=tuple(warnings),
    )


settings = load_settings()
