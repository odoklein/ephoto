"""HTTP application for real-time signature cleaning and validation."""
from __future__ import annotations

import base64
import tempfile
from dataclasses import asdict
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from signature_validator import SUPPORTED, process_files

ROOT = Path(__file__).resolve().parent
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

app = FastAPI(title="CERTIF ID Signature Check", docs_url=None, redoc_url=None)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Same-origin in Dokploy. Restrict this if the API is separated later.
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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


# The same container serves the UI, its reports, and the true Python API.
app.mount("/", StaticFiles(directory=ROOT, html=True), name="site")
