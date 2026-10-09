"""Application assembly: middleware, routers, lifecycle, then the public tool at the root.

    service/
      api.py        Make + WordPress JSON routes, health
      admin.py      review panel (HTML) and the reviewer's actions
      workflow.py   the rules: intake, processing, decision, retention
      jobs.py       bounded pool running the image pipelines
      database.py   SQLite rows, migrations, audit trail
      storage.py    images on disk
      security.py   keys, Basic auth, brute-force limiter
      web.py        headers, CSRF origin check, body cap, access log, error pages

Route order matters here.  The public signature tool (`app.py`) is mounted last, at the
root; every route declared before the mount takes precedence over it.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles

from app import app as public_app  # the public signature tool

from . import __version__, database, workflow
from .admin import CONTEXT, STATIC_DIR, router as admin_router, templates
from .api import router as api_router
from .config import settings
from .jobs import jobs
from .processing import photo_processor
from .web import HardeningMiddleware, configure_logging, install_error_pages

configure_logging(settings.log_level)
log = logging.getLogger("ephoto")
HOUSEKEEPING_SECONDS = 3600
# Two uploads at the size limit, base64-inflated, plus form overhead.
MAX_REQUEST_BYTES = max(64 * 1024 * 1024, 3 * settings.max_upload_bytes)


async def _housekeeping() -> None:
    """Retention runs hourly, not on dashboard loads: deleting is not a page view's job."""
    while True:
        await asyncio.sleep(HOUSEKEEPING_SECONDS)
        try:
            await run_in_threadpool(workflow.purge)
        except Exception:  # noqa: BLE001 - the loop must survive a bad hour
            log.exception("housekeeping failed")


@asynccontextmanager
async def lifespan(_: FastAPI):
    for warning in settings.warnings:
        log.warning("configuration: %s", warning)
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    started_at = database.init(settings.database_path)
    if started_at != database.SCHEMA_VERSION:
        log.info("database migrated from schema %s to %s", started_at, database.SCHEMA_VERSION)
    detector = photo_processor.available_detector()
    if detector != "mediapipe":
        log.log(logging.ERROR if settings.require_face_mesh else logging.WARNING,
                "face detector is %r, not mediapipe: landmark checks will be undecided", detector)
    jobs.start(settings.processing_workers)
    workflow.recover()
    await run_in_threadpool(workflow.purge)
    housekeeping = asyncio.create_task(_housekeeping())
    log.info("service %s ready (detector %s, %d worker(s))", __version__, detector, settings.processing_workers)
    try:
        yield
    finally:
        housekeeping.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await housekeeping
        await run_in_threadpool(jobs.shutdown)


api = FastAPI(
    title="CERTIF ID — préparation ANTS", version=__version__, docs_url=None, redoc_url=None,
    openapi_url=None, lifespan=lifespan,
)
api.add_middleware(HardeningMiddleware, public_base_url=settings.public_base_url, max_body_bytes=MAX_REQUEST_BYTES)
install_error_pages(api, templates, CONTEXT)
api.include_router(api_router)
api.include_router(admin_router)
api.mount("/admin/static", StaticFiles(directory=STATIC_DIR), name="admin-static")
# Declared last so every route above wins.  The public tool serves an explicit list of
# files and folders (see app.py), never the repository itself.
api.mount("/", public_app)

app = api
