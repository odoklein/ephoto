"""Machine-facing routes: Make.com intake and status, WordPress review calls, health."""
from __future__ import annotations

import base64
import binascii
import json
import re
from typing import Any

from fastapi import APIRouter, Body, Depends, HTTPException, Request, status
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from starlette.datastructures import UploadFile

from . import __version__, database, outbound, storage, workflow
from .config import settings
from .jobs import jobs
from .processing import photo_processor
from .security import require_ingest_key, require_reader, require_reviewer
from .workflow import Source, WorkflowError

router = APIRouter()
DATA_URL = re.compile(r"^data:(?P<type>[\w./+-]+);base64,(?P<payload>.+)$", re.DOTALL)


@router.get("/api/health")
def health() -> JSONResponse:
    """Liveness and configuration, without business figures.

    503 when the database cannot be read — what the container healthcheck acts on.
    "degraded" when REQUIRE_FACE_MESH is set and MediaPipe is not the active detector:
    the service still answers, but the photo checks have quietly lost their landmarks.
    """
    detector = photo_processor.available_detector()
    try:
        database.counts(settings.database_path)
        database_ok = True
    except Exception:  # noqa: BLE001 - reported, not raised
        database_ok = False
    degraded = settings.require_face_mesh and detector != "mediapipe"
    body = {
        "status": "error" if not database_ok else "degraded" if degraded else "ok",
        "version": __version__,
        "face_detector": detector,
        "ingest_configured": settings.ingest_enabled,
        "review_key_configured": settings.review_key_enabled,
        "admin_configured": settings.admin_enabled,
        "outbound_configured": settings.outbound_enabled,
        "queue": jobs.pending,
    }
    return JSONResponse(body, status_code=200 if database_ok else 503)


# ── Intake ──────────────────────────────────────────────────────────────────────────
def _from_base64(value: str, field: str) -> bytes:
    match = DATA_URL.match(value)
    payload = match.group("payload") if match else value
    try:
        data = base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError) as error:
        raise WorkflowError(422, f"Champ « {field} » : base64 invalide ({error}).") from error
    if not data:
        raise WorkflowError(422, f"Champ « {field} » vide.")
    if len(data) > settings.max_upload_bytes:
        raise WorkflowError(413, f"Champ « {field} » trop volumineux.")
    return data


def _download(url: str, field: str) -> bytes:
    try:
        return outbound.fetch(url, settings.max_upload_bytes)
    except outbound.FetchError as error:
        too_large = "volumineux" in str(error)
        raise WorkflowError(413 if too_large else 422, f"Champ « {field} » : {error}.") from error


def source_from_spec(spec: Any, field: str) -> Source:
    """Accept the shapes Make.com actually sends: data URL, raw base64, URL, or dict.

    Blocking (it may download), so the async route calls it on the threadpool.
    """
    if isinstance(spec, dict):
        filename = str(spec.get("filename") or f"{field}.jpg")
        if spec.get("base64"):
            return Source(_from_base64(str(spec["base64"]), field), filename)
        if spec.get("url"):
            return Source(_download(str(spec["url"]), field), filename)
        raise WorkflowError(422, f"Champ « {field} » sans base64 ni url.")
    if isinstance(spec, str) and spec.strip():
        value = spec.strip()
        if value.lower().startswith(("http://", "https://")):
            return Source(_download(value, field), f"{field}.jpg")
        return Source(_from_base64(value, field), f"{field}.jpg")
    raise WorkflowError(422, f"Champ « {field} » manquant.")


async def _upload(form_value: Any, field: str) -> Source:
    if not isinstance(form_value, UploadFile):
        raise WorkflowError(422, f"Champ « {field} » manquant : envoyez photo et signature.")
    data = await form_value.read(settings.max_upload_bytes + 1)
    if len(data) > settings.max_upload_bytes:
        raise WorkflowError(413, f"Champ « {field} » trop volumineux.")
    return Source(data, form_value.filename or f"{field}.jpg")


@router.post("/api/v1/ingest", status_code=status.HTTP_202_ACCEPTED)
async def ingest(request: Request, actor: str = Depends(require_ingest_key)) -> JSONResponse:
    """Receive one customer file and queue it for review.

    Both transports are supported because Make sends whichever is easiest to wire:
    JSON with base64/URL fields, or a multipart form with the two files attached.
    202 for a new dossier; 200 with `duplicate: true` when these exact files for this
    order already have a live one — a retry after a timeout, or a second intake path.
    """
    if request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
        form = await request.form(max_files=2, max_fields=20)
        photo = await _upload(form.get("photo"), "photo")
        signature = await _upload(form.get("signature"), "signature")
        reference, customer = form.get("order_id") or "", form.get("customer") or ""
    else:
        try:
            payload = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError, ValueError) as error:
            raise HTTPException(415, "Envoyez un JSON ou un formulaire multipart avec photo + signature.") from error
        if not isinstance(payload, dict):
            raise WorkflowError(422, "Le corps JSON doit être un objet.")
        photo = await run_in_threadpool(source_from_spec, payload.get("photo"), "photo")
        signature = await run_in_threadpool(source_from_spec, payload.get("signature"), "signature")
        reference = payload.get("order_id") or payload.get("source_ref") or ""
        customer = payload.get("customer")

    record, created = await run_in_threadpool(
        workflow.intake, photo, signature, workflow.normalise_reference(reference),
        workflow.normalise_customer(customer), actor=actor,
    )
    if created:
        jobs.submit(workflow.process, record["id"])
    return JSONResponse(
        {
            "submission_id": record["id"],
            "status": record["status"],
            "review_url": f"{settings.public_base_url}/admin/submissions/{record['id']}",
            "duplicate": not created,
        },
        status_code=status.HTTP_202_ACCEPTED if created else status.HTTP_200_OK,
    )


@router.get("/api/v1/submissions/{submission_id}")
def submission_status(submission_id: str, _: str = Depends(require_reader)) -> dict:
    """Polling endpoint for Make and WordPress: status and both conformity reports."""
    return workflow.status_payload(workflow.load(submission_id))


# ── Review over the API (WordPress) ─────────────────────────────────────────────────
@router.post("/api/v1/validate/{submission_id}")
def validate(submission_id: str, payload: dict = Body(default={}), reviewer: str = Depends(require_reviewer)) -> dict:
    """Controller action, API form: {"action": "accept" | "reject", "reason": "..."}.

    The reviewer is the authenticated one.  A `reviewer` field in the body used to
    override it, which let any caller sign a decision with someone else's name.
    """
    record = workflow.decide(
        submission_id,
        str(payload.get("action", "")),
        reviewer,
        str(payload.get("reason") or payload.get("note") or ""),
    )
    return {
        "submission_id": record["id"], "status": record["status"],
        "forward_status": record["forward_status"], "decided_at": record["decided_at"],
    }


def file_response(submission_id: str, kind: str) -> FileResponse:
    if kind not in storage.KINDS:
        raise HTTPException(404, "Type de fichier inconnu.")
    workflow.load(submission_id)
    path = storage.find(settings.storage_dir, submission_id, kind)
    if path is None:
        raise HTTPException(404, "Fichier absent.")
    return FileResponse(path, media_type=storage.media_type(path))


@router.get("/api/v1/files/{submission_id}/{kind}")
def api_file(submission_id: str, kind: str, _: str = Depends(require_reviewer)) -> FileResponse:
    return file_response(submission_id, kind)
