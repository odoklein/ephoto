"""The review panel: queue, dossier page, manual creation and the reviewer's actions.

Re-crop and rotation are also reachable under /api/v1 for the WordPress plugin; the
same handler answers JSON there and redirects back to the dossier page here.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from . import database, workflow
from .api import file_response
from .config import settings
from .jobs import jobs
from .security import require_reviewer
from .workflow import Source

HERE = Path(__file__).resolve().parent
STATIC_DIR = HERE / "static"
templates = Jinja2Templates(directory=str(HERE / "templates"))


def _asset_version() -> str:
    """Content hash of the panel assets, so a deploy never serves yesterday's CSS."""
    digest = hashlib.sha256()
    for name in ("admin.css", "submission.js"):
        path = STATIC_DIR / name
        if path.is_file():
            digest.update(path.read_bytes())
    return digest.hexdigest()[:10]


CONTEXT = {"settings": settings, "asset_version": _asset_version()}
router = APIRouter()

QUEUES = {
    "open": database.OPEN_STATUSES,
    "accepted": (database.ACCEPTED,),
    "rejected": (database.REJECTED, database.ERROR),
}
EVENT_LABELS = {
    "received": "Dossier reçu",
    "duplicate_received": "Envoi en double ignoré",
    "processed": "Traitement automatique terminé",
    "processing_failed": "Échec du traitement automatique",
    "requeued": "Traitement relancé après redémarrage",
    "operation_interrupted": "Opération interrompue par un redémarrage",
    "photo_recropped": "Photo recadrée",
    "signature_rotated": "Signature réorientée",
    "transmission_failed": "Échec de transmission",
    "accepted": "Accepté et transmis",
    "rejected": "Refusé",
}


def _wants_json(request: Request) -> bool:
    return request.url.path.startswith("/api/") or "application/json" in request.headers.get("accept", "")


@router.get("/admin", include_in_schema=False)
def admin_root(_: str = Depends(require_reviewer)) -> RedirectResponse:
    return RedirectResponse("/admin/dashboard", status_code=302)


@router.get("/admin/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, show: str = "open", q: str = "", reviewer: str = Depends(require_reviewer)) -> Response:
    show = show if show in QUEUES else "open"
    return templates.TemplateResponse(
        request, "dashboard.html",
        {
            "records": database.listing(settings.database_path, QUEUES[show], query=q[:200]),
            "counts": database.counts(settings.database_path),
            "show": show, "q": q[:200], "reviewer": reviewer, **CONTEXT,
        },
    )


@router.get("/admin/nouveau", response_class=HTMLResponse)
def manual_form(request: Request, reviewer: str = Depends(require_reviewer)) -> Response:
    return templates.TemplateResponse(request, "new.html", {"reviewer": reviewer, **CONTEXT})


@router.post("/admin/nouveau", include_in_schema=False)
def manual_create(
    photo: UploadFile = File(...), signature: UploadFile = File(...),
    order_id: str = Form(default=""), name: str = Form(default=""), email: str = Form(default=""),
    reviewer: str = Depends(require_reviewer),
) -> RedirectResponse:
    """Create a submission by hand, for testing and for counter staff.

    Unlike the Make intake this waits for the pipelines — the reviewer is on the page,
    and landing on a half-processed file would only confuse them.  Duplicates are not
    folded: re-running the same files by hand is a deliberate act.
    """
    sources = [
        Source(upload.file.read(settings.max_upload_bytes + 1), upload.filename or fallback)
        for upload, fallback in ((photo, "photo.jpg"), (signature, "signature.png"))
    ]
    customer = {key: value.strip() for key, value in (("name", name), ("email", email)) if value.strip()}
    record, _ = workflow.intake(
        sources[0], sources[1], workflow.normalise_reference(order_id), customer,
        actor=reviewer, deduplicate=False,
    )
    jobs.run(workflow.process, record["id"])
    return RedirectResponse(f"/admin/submissions/{record['id']}", status_code=303)


@router.get("/admin/submissions/{submission_id}", response_class=HTMLResponse)
def submission_detail(request: Request, submission_id: str, reviewer: str = Depends(require_reviewer)) -> Response:
    record = workflow.load(submission_id)
    file_map = workflow.files(submission_id)
    history = database.events(settings.database_path, submission_id)
    return templates.TemplateResponse(
        request, "submission.html",
        {
            "record": record, "reviewer": reviewer, "files": file_map,
            "accept_blocker": workflow.accept_blocker(record, file_map),
            "history": history, "event_labels": EVENT_LABELS, **CONTEXT,
        },
    )


@router.get("/admin/files/{submission_id}/{kind}")
def admin_file(submission_id: str, kind: str, _: str = Depends(require_reviewer)) -> FileResponse:
    return file_response(submission_id, kind)


@router.post("/admin/submissions/{submission_id}/recrop", include_in_schema=False)
@router.post("/api/v1/submissions/{submission_id}/recrop")
def recrop(
    request: Request, submission_id: str,
    zoom: float = Form(1.0), dx: float = Form(0.0), dy: float = Form(0.0),
    reviewer: str = Depends(require_reviewer),
) -> Any:
    """Re-run the photo crop from the original with the reviewer's manual adjustment."""
    result = workflow.recrop(submission_id, zoom, dx, dy, reviewer)
    if _wants_json(request):
        return {"status": "ok", "submission_id": submission_id, "photo_score": result.score,
                "photo_report": result.as_report()}
    return RedirectResponse(f"/admin/submissions/{submission_id}#photo-review", status_code=303)


@router.post("/admin/submissions/{submission_id}/rotate-signature", include_in_schema=False)
@router.post("/api/v1/submissions/{submission_id}/rotate-signature")
def rotate_signature(
    request: Request, submission_id: str, rotation: int = Form(...),
    reviewer: str = Depends(require_reviewer),
) -> Any:
    """Rebuild the signature from its original with a reviewer-selected rotation."""
    result = workflow.rotate_signature(submission_id, rotation, reviewer)
    if _wants_json(request):
        return {"status": "ok", "submission_id": submission_id, "signature_score": result.score,
                "signature_report": result.as_report()}
    return RedirectResponse(f"/admin/submissions/{submission_id}#signature-review", status_code=303)


@router.post("/admin/submissions/{submission_id}/decision", include_in_schema=False)
def decision_form(
    submission_id: str, action: str = Form(...), note: str = Form(default=""),
    reviewer: str = Depends(require_reviewer),
) -> RedirectResponse:
    workflow.decide(submission_id, action, reviewer, note)
    return RedirectResponse("/admin/dashboard", status_code=303)
