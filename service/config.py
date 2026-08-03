"""Runtime configuration, read once from the environment.

Every secret fails closed: an unset key disables the endpoint that needs it instead of
falling back to an open default, because this service handles identity photographs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class Settings:
    storage_dir: Path
    database_path: Path
    ingest_api_key: str
    admin_user: str
    admin_password: str
    outbound_url: str
    outbound_token: str
    outbound_timeout: float
    max_upload_bytes: int
    purge_after_days: int
    flatten_background: str  # auto | always | never
    public_base_url: str

    @property
    def ingest_enabled(self) -> bool:
        return bool(self.ingest_api_key)

    @property
    def admin_enabled(self) -> bool:
        return bool(self.admin_user and self.admin_password)

    @property
    def outbound_enabled(self) -> bool:
        return bool(self.outbound_url)


def load_settings() -> Settings:
    storage = Path(os.environ.get("STORAGE_DIR", ROOT / "storage")).resolve()
    return Settings(
        storage_dir=storage,
        database_path=storage / "ephoto.sqlite3",
        ingest_api_key=os.environ.get("INGEST_API_KEY", "").strip(),
        admin_user=os.environ.get("ADMIN_USER", "").strip(),
        admin_password=os.environ.get("ADMIN_PASSWORD", "").strip(),
        outbound_url=os.environ.get("MAKE_WEBHOOK_URL", "").strip(),
        outbound_token=os.environ.get("MAKE_WEBHOOK_TOKEN", "").strip(),
        outbound_timeout=float(os.environ.get("MAKE_WEBHOOK_TIMEOUT", "20") or 20),
        max_upload_bytes=_int("MAX_UPLOAD_BYTES", 15 * 1024 * 1024),
        # Identity photographs are personal data: they are not kept indefinitely.
        purge_after_days=_int("PURGE_AFTER_DAYS", 30),
        # Preserve already-compliant studio photos.  The processor still replaces a
        # background that is visibly non-uniform, too dark or effectively white.
        flatten_background=os.environ.get("FLATTEN_BACKGROUND", "auto").strip().lower(),
        public_base_url=os.environ.get("PUBLIC_BASE_URL", "").rstrip("/"),
    )


settings = load_settings()
