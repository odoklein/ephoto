"""HTTP application for real-time signature cleaning and validation."""
from __future__ import annotations

import asyncio
import base64
import io
import os
import tempfile
import warnings
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image

from signature_validator import SUPPORTED, process_files

ROOT = Path(__file__).resolve().parent
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_IMAGE_PIXELS = 60_000_000
# What the public page needs, and nothing else.  The whole repository used to be served
# from here: source code, deployment files, Make blueprints, and the portraits kept at
# the root for testing — real people's identity photographs, downloadable by name.
PUBLIC_PAGE = ROOT / "index.html"
PUBLIC_DIRECTORIES = ("input", "output", "reports")
# The page is open to anyone; the pipeline is CPU-heavy.  Bounding the concurrent runs
# keeps a burst of public uploads from starving the review service in the same process.
_slots = asyncio.Semaphore(max(1, int(os.environ.get("PUBLIC_MAX_CONCURRENCY", "2") or 2)))

app = FastAPI(title="CERTIF ID Signature Check", docs_url=None, redoc_url=None, openapi_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Same-origin in Dokploy. Restrict this if the API is separated later.
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


def _check_pixels(data: bytes) -> None:
    """Refuse decompression bombs from the header, before decoding a single pixel."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as image:
                width, height = image.size
    except Image.DecompressionBombError as error:
        raise HTTPException(413, "Image trop grande.") from error
    except Exception:
        return  # unreadable here: the pipeline reports it like any other failure
    if width * height > MAX_IMAGE_PIXELS:
        raise HTTPException(413, f"Image trop grande ({width}×{height} px).")


def _run_pipeline(filename: str, data: bytes) -> dict:
    with tempfile.TemporaryDirectory(prefix="signature-check-") as temporary:
        work = Path(temporary)
        input_dir, output_dir = work / "input", work / "output"
        input_dir.mkdir()
        source = input_dir / filename
        source.write_bytes(data)
        rows = process_files([source], output_dir, margin=6, max_bytes=50_000)
        row = asdict(rows[0])
        if row["output_file"]:
            cleaned = Path(row["output_file"]).read_bytes()
            row["cleaned_data_url"] = "data:image/png;base64," + base64.b64encode(cleaned).decode("ascii")
            row["output_file"] = ""
        return row


async def process_upload(file: UploadFile) -> dict:
    filename = Path(file.filename or "signature").name
    suffix = Path(filename).suffix.lower()
    if suffix not in SUPPORTED:
        raise HTTPException(415, "Format non pris en charge. Utilisez JPG, PNG, WEBP, BMP ou TIFF.")
    data = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(data) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "Le fichier dépasse la limite de 10 Mo.")
    if not data:
        raise HTTPException(422, "Le fichier est vide.")
    _check_pixels(data)
    # Off the event loop: OpenCV work run inline here used to freeze every other request
    # of the process — the review panel and the Make intake included — for its duration.
    async with _slots:
        return await run_in_threadpool(_run_pipeline, filename, data)


@app.post("/api/process")
async def process_signature(file: UploadFile = File(...)) -> dict:
    return await process_upload(file)


@app.post("/api/process-batch")
async def process_batch(files: list[UploadFile] = File(...)) -> dict[str, list[dict]]:
    if not files:
        raise HTTPException(422, "Aucun fichier reçu.")
    if len(files) > 20:
        raise HTTPException(422, "Sélectionnez au maximum 20 fichiers.")
    rows: list[dict] = []
    for file in files:
        try:
            rows.append(await process_upload(file))
        except HTTPException as error:
            rows.append({
                "filename": Path(file.filename or "signature").name,
                "background_clean": "fail", "dimensions_ok": "fail", "format_ok": "fail",
                "weight_ok": "fail", "stroke_quality": "fail", "global_score": 0,
                "failures": error.detail, "output_file": "", "cleaned_data_url": "",
            })
    return {"rows": rows}


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def public_page() -> FileResponse:
    return FileResponse(PUBLIC_PAGE, media_type="text/html")


for _directory in PUBLIC_DIRECTORIES:
    app.mount(f"/{_directory}", StaticFiles(directory=ROOT / _directory, check_dir=False), name=_directory)
