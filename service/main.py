"""FastAPI entry point: Make.com intake, human review panel, outbound validation.

Route order matters here.  The public signature tool (`app.py`) is mounted last, at the
root, exactly as it is deployed today; every route declared before the mount takes
precedence over it, which is also how the storage folder is kept out of its static
handler.
"""
from __future__ import annotations

import base64
import binascii
import json
import re
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from app import app as legacy_app  # the untouched public signature tool

from . import database, outbound, storage
from .config import ROOT, settings
from .processing import imaging, photo_processor, signature_processor
from .processing.photo_processor import CropOverride
from .security import require_ingest_key, require_reviewer

templates = Jinja2Templates(directory=str(Path(__file__).resolve().parent / "templates"))
DATA_URL = re.compile(r"^data:(?P<type>[\w./+-]+);base64,(?P<payload>.+)$", re.DOTALL)


@asynccontextmanager
async def lifespan(_: FastAPI):
    settings.storage_dir.mkdir(parents=True, exist_ok=True)
    database.init(settings.database_path)
    purge_expired()
    yield


api = FastAPI(
    title="CERTIF ID — préparation ANTS", version="1.0", docs_url=None, redoc_url=None,
    lifespan=lifespan,
)


# ── Housekeeping ────────────────────────────────────────────────────────────────────
def purge_expired() -> int:
    """Drop decided submissions past the retention window, images included."""
    removed = 0
    for submission_id in database.expired(settings.database_path, settings.purge_after_days):
        storage.purge(settings.storage_dir, submission_id)
        database.delete(settings.database_path, submission_id)
        removed += 1
    return removed


@api.get("/api/health")
def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "ingest_configured": settings.ingest_enabled,
        "admin_configured": settings.admin_enabled,
        "outbound_configured": settings.outbound_enabled,
        "face_detector": photo_processor.available_detector(),
        "counts": database.counts(settings.database_path),
    }


# ── Intake ──────────────────────────────────────────────────────────────────────────
def _decode_source(spec: Any, field: str) -> tuple[bytes, str]:
    """Accept the shapes Make.com actually sends: data URL, raw base64, URL, or dict."""
    if isinstance(spec, dict):
        filename = str(spec.get("filename") or f"{field}.jpg")
        if spec.get("base64"):
            return _from_base64(str(spec["base64"]), field), filename
        if spec.get("url"):
            return outbound.fetch(str(spec["url"]), settings.max_upload_bytes), filename
        raise HTTPException(422, f"Champ « {field} » sans base64 ni url.")
    if isinstance(spec, str) and spec.strip():
        value = spec.strip()
        if value.lower().startswith(("http://", "https://")):
            return outbound.fetch(value, settings.max_upload_bytes), f"{field}.jpg"
        return _from_base64(value, field), f"{field}.jpg"
    raise HTTPException(422, f"Champ « {field} » manquant.")


def _from_base64(value: str, field: str) -> bytes:
    match = DATA_URL.match(value)
    payload = match.group("payload") if match else value
    try:
        data = base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError) as error:
        raise HTTPException(422, f"Champ « {field} » : base64 invalide ({error}).") from error
    if not data:
        raise HTTPException(422, f"Champ « {field} » vide.")
    if len(data) > settings.max_upload_bytes:
        raise HTTPException(413, f"Champ « {field} » trop volumineux.")
    return data


def _process(submission_id: str, photo: tuple[bytes, str], signature: tuple[bytes, str]) -> None:
    """Run both pipelines and file the result. Executed off the event loop."""
    path = settings.database_path
    try:
        photo_data, photo_name = photo
        signature_data, signature_name = signature
        storage.write(settings.storage_dir, submission_id, "photo_original", photo_data,
                      imaging.sniff_extension(photo_data, Path(photo_name).suffix.lower() or ".jpg"))
        storage.write(settings.storage_dir, submission_id, "signature_original", signature_data,
                      imaging.sniff_extension(signature_data, Path(signature_name).suffix.lower() or ".png"))

        photo_result = photo_processor.process(photo_data, flatten=settings.flatten_background)
        if photo_result.data:
            storage.write(settings.storage_dir, submission_id, "photo_clean", photo_result.data, photo_result.extension)
        signature_result = signature_processor.process(signature_data, signature_name)
        if signature_result.data:
            storage.write(settings.storage_dir, submission_id, "signature_clean", signature_result.data, signature_result.extension)

        database.update(
            path, submission_id,
            status=database.PENDING,
            photo_report=json.dumps(photo_result.as_report(), ensure_ascii=False),
            signature_report=json.dumps(signature_result.as_report(), ensure_ascii=False),
            photo_score=photo_result.score, signature_score=signature_result.score,
            error=photo_result.error or signature_result.error,
        )
    except Exception as error:  # a broken upload must not leave the row in limbo
        database.update(path, submission_id, status=database.ERROR, error=f"{type(error).__name__}: {error}")


@api.post("/api/v1/ingest", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_ingest_key)])
async def ingest(
    background: BackgroundTasks,
    request: Request,
    photo: UploadFile | None = File(default=None),
    signature: UploadFile | None = File(default=None),
    order_id: str = Form(default=""),
    customer: str = Form(default=""),
) -> JSONResponse:
    """Receive one customer file from Make and queue it for review.

    Both transports are supported because Make sends whichever is easiest to wire:
    JSON with base64/URL fields, or a multipart form with the two files attached.
    """
    if photo is not None and signature is not None:
        photo_source = (await photo.read(settings.max_upload_bytes + 1), photo.filename or "photo.jpg")
        signature_source = (await signature.read(settings.max_upload_bytes + 1), signature.filename or "signature.png")
        for data, _ in (photo_source, signature_source):
            if len(data) > settings.max_upload_bytes:
                raise HTTPException(413, "Fichier trop volumineux.")
        try:
            customer_data = json.loads(customer) if customer else {}
        except json.JSONDecodeError:
            customer_data = {"raw": customer}
        reference = order_id
    else:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            raise HTTPException(415, "Envoyez un JSON ou un formulaire multipart avec photo + signature.") from error
        if not isinstance(payload, dict):
            raise HTTPException(422, "Le corps JSON doit être un objet.")
        photo_source = _decode_source(payload.get("photo"), "photo")
        signature_source = _decode_source(payload.get("signature"), "signature")
        customer_data = payload.get("customer") or {}
        reference = str(payload.get("order_id") or payload.get("source_ref") or "")

    submission_id = secrets.token_hex(8)
    database.create(settings.database_path, submission_id, reference, customer_data)
    background.add_task(_process, submission_id, photo_source, signature_source)
    return JSONResponse(
        {
            "submission_id": submission_id,
            "status": database.PROCESSING,
            "review_url": f"{settings.public_base_url}/admin/submissions/{submission_id}",
        },
        status_code=status.HTTP_202_ACCEPTED,
    )


@api.get("/api/v1/submissions/{submission_id}", dependencies=[Depends(require_ingest_key)])
def submission_status(submission_id: str) -> dict:
    """Polling endpoint for Make: status and both conformity reports."""
    record = database.get(settings.database_path, submission_id)
    if record is None:
        raise HTTPException(404, "Dossier inconnu.")
    return {
        "submission_id": record["id"],
        "status": record["status"],
        "photo": record["photo_report"],
        "signature": record["signature_report"],
        "forward_status": record["forward_status"],
        "reviewer_note": record["reviewer_note"],
    }


# ── Decision ────────────────────────────────────────────────────────────────────────
def decide(submission_id: str, action: str, reviewer: str, note: str) -> dict:
    """Accept (and transmit) or reject a submission; returns the updated record."""
    record = database.get(settings.database_path, submission_id)
    if record is None:
        raise HTTPException(404, "Dossier inconnu.")
    if record["status"] not in database.OPEN_STATUSES:
        raise HTTPException(409, f"Dossier déjà traité ({record['status']}).")
    if action not in ("accept", "reject"):
        raise HTTPException(422, "Action inconnue : accept ou reject.")

    if action == "reject":
        database.update(
            settings.database_path, submission_id, status=database.REJECTED,
            reviewer=reviewer, reviewer_note=note, decided_at=database.now(),
        )
        return database.get(settings.database_path, submission_id)

    record["decided_at"] = database.now()
    delivered, report = outbound.send(outbound.build_payload(record, reviewer))
    if not delivered:
        # Never archive a file that was not transmitted: it stays in the queue with the
        # reason visible, so the reviewer can retry once Make is reachable again.
        database.update(settings.database_path, submission_id, forward_status=report, reviewer=reviewer)
        raise HTTPException(502, f"Transmission impossible : {report}")
    database.update(
        settings.database_path, submission_id, status=database.ACCEPTED, reviewer=reviewer,
        reviewer_note=note, decided_at=record["decided_at"], forward_status=report,
    )
    return database.get(settings.database_path, submission_id)


@api.post("/api/v1/validate/{submission_id}")
def validate(submission_id: str, payload: dict = Body(default={}), reviewer: str = Depends(require_reviewer)) -> dict:
    """Controller action, API form: {"action": "accept" | "reject", "reason": "..."}."""
    record = decide(
        submission_id,
        str(payload.get("action", "")).lower(),
        str(payload.get("reviewer") or reviewer),
        str(payload.get("reason") or payload.get("note") or ""),
    )
    return {
        "submission_id": record["id"], "status": record["status"],
        "forward_status": record["forward_status"], "decided_at": record["decided_at"],
    }


# ── Review panel ────────────────────────────────────────────────────────────────────
@api.get("/admin", include_in_schema=False)
def admin_root(_: str = Depends(require_reviewer)) -> RedirectResponse:
    return RedirectResponse("/admin/dashboard", status_code=302)


@api.get("/admin/dashboard", response_class=HTMLResponse)
def dashboard(
    request: Request, show: str = "open", q: str = "", reviewer: str = Depends(require_reviewer)
) -> Response:
    purge_expired()
    statuses = {
        "open": database.OPEN_STATUSES,
        "accepted": (database.ACCEPTED,),
        "rejected": (database.REJECTED, database.ERROR),
    }.get(show, database.OPEN_STATUSES)
    return templates.TemplateResponse(
        request, "dashboard.html",
        {
            "records": database.listing(settings.database_path, statuses, query=q),
            "counts": database.counts(settings.database_path),
            "show": show, "q": q, "reviewer": reviewer, "settings": settings,
        },
    )


@api.get("/admin/nouveau", response_class=HTMLResponse)
def manual_form(request: Request, reviewer: str = Depends(require_reviewer)) -> Response:
    return templates.TemplateResponse(request, "new.html", {"reviewer": reviewer, "settings": settings})


@api.post("/admin/nouveau", include_in_schema=False)
def manual_create(
    photo: UploadFile = File(...), signature: UploadFile = File(...),
    order_id: str = Form(default=""), name: str = Form(default=""), email: str = Form(default=""),
    reviewer: str = Depends(require_reviewer),
) -> RedirectResponse:
    """Create a submission by hand, for testing and for counter staff.

    Unlike the Make intake this runs the pipelines inline — the reviewer is waiting on
    the page, so landing on a half-processed file would only confuse them.
    """
    sources = []
    for upload, fallback in ((photo, "photo.jpg"), (signature, "signature.png")):
        data = upload.file.read(settings.max_upload_bytes + 1)
        if not data:
            raise HTTPException(422, f"Fichier « {upload.filename or fallback} » vide.")
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(413, f"Fichier « {upload.filename or fallback} » trop volumineux.")
        sources.append((data, upload.filename or fallback))

    submission_id = secrets.token_hex(8)
    customer = {key: value for key, value in (("name", name), ("email", email)) if value}
    database.create(settings.database_path, submission_id, order_id, customer)
    _process(submission_id, sources[0], sources[1])
    return RedirectResponse(f"/admin/submissions/{submission_id}", status_code=303)


@api.get("/admin/submissions/{submission_id}", response_class=HTMLResponse)
def submission_detail(request: Request, submission_id: str, reviewer: str = Depends(require_reviewer)) -> Response:
    record = database.get(settings.database_path, submission_id)
    if record is None:
        raise HTTPException(404, "Dossier inconnu.")
    return templates.TemplateResponse(
        request, "submission.html",
        {"record": record, "reviewer": reviewer, "files": _file_map(submission_id), "settings": settings},
    )


def _file_map(submission_id: str) -> dict[str, bool]:
    return {kind: storage.find(settings.storage_dir, submission_id, kind) is not None for kind in storage.KINDS}


@api.get("/admin/files/{submission_id}/{kind}")
def file_bytes(submission_id: str, kind: str, _: str = Depends(require_reviewer)) -> FileResponse:
    if kind not in storage.KINDS:
        raise HTTPException(404, "Type de fichier inconnu.")
    path = storage.find(settings.storage_dir, submission_id, kind)
    if path is None:
        raise HTTPException(404, "Fichier absent.")
    return FileResponse(path, media_type=storage.media_type(path))


@api.post("/admin/submissions/{submission_id}/recrop", include_in_schema=False)
def recrop(
    submission_id: str, zoom: float = Form(1.0), dx: float = Form(0.0), dy: float = Form(0.0),
    reviewer: str = Depends(require_reviewer),
) -> RedirectResponse:
    """Re-run the photo crop from the original with the reviewer's manual adjustment."""
    record = database.get(settings.database_path, submission_id)
    if record is None:
        raise HTTPException(404, "Dossier inconnu.")
    source = storage.find(settings.storage_dir, submission_id, "photo_original")
    if source is None:
        raise HTTPException(404, "Photo d'origine absente.")
    result = photo_processor.process(
        source.read_bytes(), CropOverride(zoom=zoom, dx=dx, dy=dy), flatten=settings.flatten_background
    )
    if result.data:
        storage.write(settings.storage_dir, submission_id, "photo_clean", result.data, result.extension)
    database.update(
        settings.database_path, submission_id,
        photo_report=json.dumps(result.as_report(), ensure_ascii=False),
        photo_score=result.score, reviewer=reviewer,
    )
    return RedirectResponse(f"/admin/submissions/{submission_id}", status_code=303)


@api.post("/admin/submissions/{submission_id}/decision", include_in_schema=False)
def decision_form(
    submission_id: str, action: str = Form(...), note: str = Form(default=""),
    reviewer: str = Depends(require_reviewer),
) -> RedirectResponse:
    decide(submission_id, action, reviewer, note)
    return RedirectResponse("/admin/dashboard", status_code=303)


# ── Guards, then the untouched public tool ──────────────────────────────────────────
@api.get("/storage/{rest:path}", include_in_schema=False)
@api.get("/service/{rest:path}", include_in_schema=False)
def blocked(rest: str) -> Response:
    """The legacy static handler serves the repository root; these paths must not leak.

    `storage/` holds identity photographs and the database, `service/` the source code,
    so both are answered here — before the mount below ever sees the request.
    """
    raise HTTPException(404, "Not found")


# Declared last so every route above wins: the public page keeps its exact behaviour.
api.mount("/", legacy_app)

app = api
