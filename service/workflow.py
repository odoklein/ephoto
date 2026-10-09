"""What happens to a dossier, independently of how the request arrived.

Intake, processing, re-crop, rotation, decision and retention live here so that the
JSON API, the review panel and startup recovery all go through the same rules.  They
used to be spread across route handlers, and drifted: the panel disabled « Accepter »
on an unprocessed dossier while the API forwarded `photo: null` for the same file.

Errors are raised as WorkflowError with the HTTP status they map to; the web layer
renders them as JSON for machines and as a page for the reviewer's browser.

    processing ──(pipelines)──► pending ──accept──► accepted ──(retention)──► purged
        │                         │  └────reject──► rejected ──(retention)──► purged
        └──────(failure)────────► error ───────────────────────(retention)──► purged
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import math
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

from . import database, outbound, storage
from .config import settings
from .jobs import jobs
from .models import ProcessedImage
from .processing import imaging, photo_processor, signature_processor
from .processing.photo_processor import CropOverride

log = logging.getLogger("ephoto.workflow")

SYSTEM = "système"
ZOOM_RANGE = (0.5, 2.0)
SHIFT_RANGE = (-0.5, 0.5)
ROTATION_LIMIT = 360
REFERENCE_MAX = 128
NOTE_MAX = 2000
CUSTOMER_MAX_BYTES = 16_384

_intake_lock = threading.Lock()


class WorkflowError(Exception):
    """A request the workflow refuses, with the HTTP status it maps to."""

    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass
class Source:
    data: bytes
    filename: str


# ── Reading ─────────────────────────────────────────────────────────────────────────
def load(submission_id: str) -> dict:
    if not storage.valid_id(submission_id):
        raise WorkflowError(404, "Dossier inconnu.")
    record = database.get(settings.database_path, submission_id)
    if record is None:
        raise WorkflowError(404, "Dossier inconnu.")
    return record


def files(submission_id: str) -> dict[str, bool]:
    return {kind: storage.find(settings.storage_dir, submission_id, kind) is not None for kind in storage.KINDS}


def accept_blocker(record: dict, file_map: dict[str, bool] | None = None) -> str:
    """Why this dossier cannot be accepted right now, or "" when it can.

    The single source of truth for both the panel's button and the API's refusal.
    """
    status = record["status"]
    if status == database.PROCESSING:
        return "Traitement en cours : attendez la fin avant de décider."
    if status != database.PENDING:
        return f"Dossier déjà traité ({status})."
    if record.get("locked_since"):
        return "Une transmission ou un recalcul est en cours sur ce dossier."
    if record.get("error"):
        return f"Traitement incomplet : {record['error']}"
    file_map = file_map if file_map is not None else files(record["id"])
    if not (file_map.get("photo_clean") and file_map.get("signature_clean")):
        return "Photo ou signature préparée absente."
    if not settings.outbound_enabled:
        return "Transmission non configurée (MAKE_WEBHOOK_URL)."
    return ""


def status_payload(record: dict) -> dict:
    """The dossier as machine callers (Make, WordPress) see it."""
    file_map = files(record["id"])
    return {
        "submission_id": record["id"],
        "status": record["status"],
        "photo": record["photo_report"],
        "signature": record["signature_report"],
        "photo_score": record["photo_score"],
        "signature_score": record["signature_score"],
        "forward_status": record["forward_status"],
        "reviewer_note": record["reviewer_note"],
        "error": record["error"],
        "decided_at": record["decided_at"],
        "files": file_map,
        "accept_ready": not accept_blocker(record, file_map),
        "urls": {kind: f"/api/v1/files/{record['id']}/{kind}" for kind, present in file_map.items() if present},
    }


# ── Intake ──────────────────────────────────────────────────────────────────────────
def normalise_reference(value: Any) -> str:
    reference = str(value if value is not None else "").strip()
    if len(reference) > REFERENCE_MAX:
        raise WorkflowError(422, f"Référence de commande trop longue (maximum {REFERENCE_MAX} caractères).")
    return reference


def normalise_customer(value: Any) -> dict:
    """Customer data as a dict, whatever shape the caller sent.

    Templates read it with .get(); one dossier carrying a bare string here used to take
    the whole dashboard down for every reviewer.
    """
    if value in (None, ""):
        return {}
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            decoded = value
        value = decoded
    customer = value if isinstance(value, dict) else {"raw": value}
    if len(json.dumps(customer, ensure_ascii=False, default=str)) > CUSTOMER_MAX_BYTES:
        raise WorkflowError(422, "Données client trop volumineuses.")
    return customer


def check_image(source: Source, field: str) -> None:
    if not source.data:
        raise WorkflowError(422, f"Champ « {field} » vide.")
    if len(source.data) > settings.max_upload_bytes:
        raise WorkflowError(413, f"Champ « {field} » trop volumineux.")
    try:
        imaging.inspect(source.data, settings.max_image_pixels)
    except imaging.ImageRejected as error:
        raise WorkflowError(413 if error.too_large else 422, f"Champ « {field} » : {error}") from error


def fingerprint(photo: bytes, signature: bytes, reference: str) -> str:
    digest = hashlib.sha256()
    for part in (hashlib.sha256(photo).digest(), hashlib.sha256(signature).digest(), reference.encode("utf-8")):
        digest.update(part)
    return digest.hexdigest()


def intake(
    photo: Source, signature: Source, reference: str, customer: dict, *, actor: str, deduplicate: bool = True,
) -> tuple[dict, bool]:
    """File a new dossier — or find the live one already holding these files.

    Returns (record, created).  Make and WooCommerce both retry on timeout, and the same
    order can reach the service through the plugin and a Make scenario; without the
    fingerprint each retry became a second dossier, then a second paid ePhoto request.

    The originals are written before this returns, so the dossier can be processed —
    and re-processed after a crash — from disk alone.  Queuing it is the caller's job.
    """
    check_image(photo, "photo")
    check_image(signature, "signature")
    print_ = fingerprint(photo.data, signature.data, reference)
    path = settings.database_path
    with _intake_lock:
        if deduplicate:
            existing = database.find_duplicate(path, print_)
            if existing is not None:
                database.log_event(path, existing["id"], "duplicate_received", actor, source_ref=reference)
                log.info("intake %s: duplicate of %s, not re-created", reference or "-", existing["id"])
                return existing, False
        submission_id = secrets.token_hex(8)
        database.create(path, submission_id, reference, customer, print_)
    try:
        for kind, source, fallback in (("photo_original", photo, ".jpg"), ("signature_original", signature, ".png")):
            storage.write(settings.storage_dir, submission_id, kind, source.data,
                          imaging.sniff_extension(source.data, fallback))
    except Exception as error:
        database.update(path, submission_id, status=database.ERROR, error=f"Enregistrement impossible : {error}")
        log.exception("intake %s: cannot store originals", submission_id)
        raise WorkflowError(500, "Enregistrement des fichiers impossible.") from error
    database.log_event(path, submission_id, "received", actor, source_ref=reference,
                       photo_bytes=len(photo.data), signature_bytes=len(signature.data))
    log.info("intake %s: received (ref %s, by %s)", submission_id, reference or "-", actor)
    return database.get(path, submission_id), True


# ── Processing ──────────────────────────────────────────────────────────────────────
def process(submission_id: str) -> None:
    """Run both pipelines from the stored originals and file the result.

    Runs on the job pool.  The final status change is conditional: a dossier rejected
    while it was being processed stays rejected — it used to be resurrected as pending.
    """
    path, root = settings.database_path, settings.storage_dir
    started = time.monotonic()
    try:
        photo_path = storage.find(root, submission_id, "photo_original")
        signature_path = storage.find(root, submission_id, "signature_original")
        if photo_path is None or signature_path is None:
            raise FileNotFoundError("fichiers d'origine absents")

        photo = photo_processor.process(photo_path.read_bytes(), flatten=settings.flatten_background)
        if photo.data:
            storage.write(root, submission_id, "photo_clean", photo.data, photo.extension)
        signature = signature_processor.process(signature_path.read_bytes(), signature_path.name)
        if signature.data:
            storage.write(root, submission_id, "signature_clean", signature.data, signature.extension)

        results = {
            "photo_report": _report(photo), "signature_report": _report(signature),
            "photo_score": photo.score, "signature_score": signature.score,
            "error": photo.error or signature.error,
        }
        if not database.transition(path, submission_id, (database.PROCESSING,), status=database.PENDING, **results):
            database.update(path, submission_id, **results)
        seconds = round(time.monotonic() - started, 2)
        database.log_event(path, submission_id, "processed", SYSTEM, photo_score=photo.score,
                           signature_score=signature.score, error=results["error"], seconds=seconds,
                           detector=photo.metadata.get("detector", "none"))
        log.info("process %s: photo %s%% signature %s%% in %.1fs%s", submission_id, photo.score,
                 signature.score, seconds, f" — {results['error']}" if results["error"] else "")
    except Exception as error:  # a broken upload must not leave the row in limbo
        message = f"{type(error).__name__}: {error}"
        log.exception("process %s: failed", submission_id)
        database.transition(path, submission_id, (database.PROCESSING,), status=database.ERROR, error=message)
        database.log_event(path, submission_id, "processing_failed", SYSTEM, error=message)


def _report(result: ProcessedImage) -> str:
    return json.dumps(result.as_report(), ensure_ascii=False)


def recover() -> None:
    """Startup: release interrupted operations and re-queue unfinished processing."""
    path, root = settings.database_path, settings.storage_dir
    for submission_id, operation in database.release_interrupted(path):
        database.log_event(path, submission_id, "operation_interrupted", SYSTEM, operation=operation)
        log.warning("recover %s: %s interrupted by a restart", submission_id, operation or "operation")
    for submission_id in database.with_status(path, database.PROCESSING):
        if storage.find(root, submission_id, "photo_original") and storage.find(root, submission_id, "signature_original"):
            database.log_event(path, submission_id, "requeued", SYSTEM)
            jobs.submit(process, submission_id)
            log.info("recover %s: processing re-queued", submission_id)
        else:
            database.transition(path, submission_id, (database.PROCESSING,), status=database.ERROR,
                                error="Traitement interrompu avant l'enregistrement des fichiers d'origine.")
            log.warning("recover %s: originals missing, marked as error", submission_id)


# ── Reviewer adjustments ────────────────────────────────────────────────────────────
def _hold(record: dict, operation: str) -> None:
    if record["status"] != database.PENDING:
        raise WorkflowError(409, accept_blocker(record) or f"Dossier déjà traité ({record['status']}).")
    if not database.claim(settings.database_path, record["id"], operation):
        raise WorkflowError(409, "Une transmission ou un recalcul est déjà en cours sur ce dossier.")


def _in_range(name: str, value: float, bounds: tuple[float, float]) -> None:
    if not math.isfinite(value) or not bounds[0] <= value <= bounds[1]:
        raise WorkflowError(422, f"« {name} » doit être compris entre {bounds[0]} et {bounds[1]}.")


def recrop(submission_id: str, zoom: float, dx: float, dy: float, reviewer: str) -> ProcessedImage:
    """Re-run the photo crop from the original with the reviewer's manual adjustment."""
    _in_range("zoom", zoom, ZOOM_RANGE)
    _in_range("dx", dx, SHIFT_RANGE)
    _in_range("dy", dy, SHIFT_RANGE)
    record = load(submission_id)
    source = storage.find(settings.storage_dir, submission_id, "photo_original")
    if source is None:
        raise WorkflowError(404, "Photo d'origine absente.")
    _hold(record, "recrop")
    try:
        result = jobs.run(photo_processor.process, source.read_bytes(), CropOverride(zoom, dx, dy),
                          settings.flatten_background)
        if result.data:
            storage.write(settings.storage_dir, submission_id, "photo_clean", result.data, result.extension)
        database.update(
            settings.database_path, submission_id, photo_report=_report(result), photo_score=result.score,
            reviewer=reviewer, error=result.error or record["signature_report"].get("error", ""),
        )
        database.log_event(settings.database_path, submission_id, "photo_recropped", reviewer,
                           zoom=zoom, dx=dx, dy=dy, score=result.score)
        return result
    finally:
        database.release(settings.database_path, submission_id)


def rotate_signature(submission_id: str, rotation: int, reviewer: str) -> ProcessedImage:
    """Rebuild the signature from its original with a reviewer-selected rotation."""
    if abs(rotation) > ROTATION_LIMIT:
        raise WorkflowError(422, f"Angle hors limites (±{ROTATION_LIMIT}°).")
    record = load(submission_id)
    source = storage.find(settings.storage_dir, submission_id, "signature_original")
    if source is None:
        raise WorkflowError(404, "Signature d'origine absente.")
    _hold(record, "rotation")
    try:
        run = functools.partial(signature_processor.process, rotation_degrees=rotation)
        result = jobs.run(run, source.read_bytes(), source.name)
        if not result.data or result.error:
            raise WorkflowError(422, f"Rotation impossible : {result.error or 'sortie vide'}")
        storage.write(settings.storage_dir, submission_id, "signature_clean", result.data, result.extension)
        database.update(
            settings.database_path, submission_id, signature_report=_report(result),
            signature_score=result.score, reviewer=reviewer,
            error=record["photo_report"].get("error", "") or result.error,
        )
        database.log_event(settings.database_path, submission_id, "signature_rotated", reviewer,
                           rotation=rotation, score=result.score)
        return result
    finally:
        database.release(settings.database_path, submission_id)


# ── Decision ────────────────────────────────────────────────────────────────────────
def decide(submission_id: str, action: str, reviewer: str, note: str) -> dict:
    """Accept (and transmit) or reject a submission; returns the updated record."""
    path = settings.database_path
    action = (action or "").strip().lower()
    note = (note or "").strip()
    if action not in ("accept", "reject"):
        raise WorkflowError(422, "Action inconnue : accept ou reject.")
    if len(note) > NOTE_MAX:
        raise WorkflowError(422, f"Commentaire trop long (maximum {NOTE_MAX} caractères).")
    record = load(submission_id)
    if record["status"] in database.DECIDED_STATUSES:
        raise WorkflowError(409, f"Dossier déjà traité ({record['status']}).")

    if action == "reject":
        if not note:
            raise WorkflowError(422, "Le motif du refus est obligatoire.")
        if not database.transition(path, submission_id, database.OPEN_STATUSES, require_unlocked=True,
                                   status=database.REJECTED, reviewer=reviewer, reviewer_note=note,
                                   decided_at=database.now()):
            raise WorkflowError(409, "Le dossier vient de changer d'état ou une opération est en cours ; rechargez la fiche.")
        database.log_event(path, submission_id, "rejected", reviewer, note=note)
        log.info("decide %s: rejected by %s", submission_id, reviewer)
        return load(submission_id)

    blocker = accept_blocker(record)
    if blocker:
        raise WorkflowError(409, blocker)
    if not database.claim(path, submission_id, "transmission"):
        raise WorkflowError(409, "Une transmission ou un recalcul est déjà en cours sur ce dossier.")
    try:
        record = load(submission_id)  # re-read under the lock: the reports may have just changed
        record["decided_at"] = database.now()
        delivered, report = outbound.send(outbound.build_payload(record, reviewer))
        if not delivered:
            # Never archive a file that was not transmitted: it stays in the queue with the
            # reason visible, so the reviewer can retry once Make is reachable again.
            database.update(path, submission_id, forward_status=report, reviewer=reviewer)
            database.log_event(path, submission_id, "transmission_failed", reviewer, result=report)
            log.warning("decide %s: transmission failed — %s", submission_id, report)
            raise WorkflowError(502, f"Transmission impossible : {report}")
        database.transition(path, submission_id, (database.PENDING,), status=database.ACCEPTED,
                            reviewer=reviewer, reviewer_note=note, decided_at=record["decided_at"],
                            forward_status=report, locked_since="", lock_operation="")
        database.log_event(path, submission_id, "accepted", reviewer, note=note, result=report)
        log.info("decide %s: accepted by %s, %s", submission_id, reviewer, report)
        return load(submission_id)
    finally:
        database.release(path, submission_id)


# ── Retention ───────────────────────────────────────────────────────────────────────
def purge() -> int:
    """Delete what the retention policy says must go: decided dossiers, then orphans.

    Identity photographs are personal data.  Decided dossiers go after
    PURGE_AFTER_DAYS; undecided ones only if PURGE_OPEN_AFTER_DAYS is set; image
    folders without a database row whenever they are found.
    """
    path, root = settings.database_path, settings.storage_dir
    doomed = database.expired(path, settings.purge_after_days)
    if settings.purge_open_after_days > 0:
        doomed += database.expired(path, settings.purge_open_after_days, (database.PENDING,))
    for submission_id in doomed:
        storage.purge(root, submission_id)
        database.delete(path, submission_id)
    orphans = storage.orphans(root, database.ids(path))
    for submission_id in orphans:
        storage.purge(root, submission_id)
    if doomed or orphans:
        log.info("purge: %d dossier(s) and %d orphan folder(s) removed", len(doomed), len(orphans))
    return len(doomed)
