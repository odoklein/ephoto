"""Focused checks of the building blocks the smoke test only exercises end to end.

Self-contained like the smoke test: temporary storage, no network (URL checks use IP
literals, which resolve without DNS).

    py tests/unit_test.py
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path

import cv2
import numpy as np

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

PROJECT = Path(__file__).resolve().parents[1]
STORE = Path(tempfile.mkdtemp(prefix="ephoto-unit-"))
failures: list[str] = []

# The original single-table schema, as production files carry it at user_version 0.
LEGACY_SCHEMA = """
CREATE TABLE submissions (
    id TEXT PRIMARY KEY, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, status TEXT NOT NULL,
    source_ref TEXT NOT NULL DEFAULT '', customer TEXT NOT NULL DEFAULT '{}',
    photo_report TEXT NOT NULL DEFAULT '{}', signature_report TEXT NOT NULL DEFAULT '{}',
    photo_score INTEGER NOT NULL DEFAULT 0, signature_score INTEGER NOT NULL DEFAULT 0,
    reviewer TEXT NOT NULL DEFAULT '', reviewer_note TEXT NOT NULL DEFAULT '',
    decided_at TEXT NOT NULL DEFAULT '', forward_status TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT ''
);
CREATE INDEX submissions_status ON submissions(status, created_at);
INSERT INTO submissions (id, created_at, updated_at, status, source_ref, customer)
VALUES ('aaaaaaaaaaaaaaaa', '2026-01-01T00:00:00+00:00', '2026-01-01T00:00:00+00:00', 'pending', 'WC-1', '"texte"');
"""


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}{'' if condition else '  → ' + detail}")
    if not condition:
        failures.append(label)


def raises(function, exception: type[BaseException]) -> bool:
    try:
        function()
    except exception:
        return True
    except Exception:  # noqa: BLE001 - the wrong exception is a failure, not a crash
        return False
    return False


def main() -> int:
    os.environ.update(STORAGE_DIR=str(STORE), INGEST_API_KEY="unit-ingest", REVIEW_API_KEY="unit-review")
    sys.path.insert(0, str(PROJECT))
    os.chdir(PROJECT)

    from service import database, outbound, storage, workflow
    from service.processing import imaging
    from service.security import FailureLimiter, reviewer_name
    import signature_validator as validator

    # ── Migrations ───────────────────────────────────────────────────────────────────
    legacy = STORE / "legacy.sqlite3"
    with sqlite3.connect(legacy) as connection:
        connection.executescript(LEGACY_SCHEMA)
    started = database.init(legacy)
    check("migration depuis le schéma d'origine", started == 0)
    record = database.get(legacy, "aaaaaaaaaaaaaaaa")
    check("ligne existante conservée", record is not None and record["source_ref"] == "WC-1")
    check("nouvelles colonnes présentes", record is not None and record["locked_since"] == "" and record["fingerprint"] == "")
    check("client non structuré lu comme dict", record is not None and record["customer"] == {"raw": "texte"})
    check("migration idempotente", database.init(legacy) == database.SCHEMA_VERSION)
    with sqlite3.connect(legacy) as connection:
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    check("version de schéma enregistrée", version == database.SCHEMA_VERSION, str(version))

    # ── Locks and conditional transitions ────────────────────────────────────────────
    check("verrou pris une seule fois",
          database.claim(legacy, "aaaaaaaaaaaaaaaa", "transmission")
          and not database.claim(legacy, "aaaaaaaaaaaaaaaa", "recrop"))
    check("refus impossible pendant une transmission",
          not database.transition(legacy, "aaaaaaaaaaaaaaaa", database.OPEN_STATUSES, require_unlocked=True,
                                  status=database.REJECTED))
    interrupted = database.release_interrupted(legacy)
    check("verrou orphelin libéré au démarrage", interrupted == [("aaaaaaaaaaaaaaaa", "transmission")], str(interrupted))
    check("transmission interrompue signalée",
          "interrompue" in database.get(legacy, "aaaaaaaaaaaaaaaa")["forward_status"])
    check("colonne inconnue refusée", raises(lambda: database.update(legacy, "aaaaaaaaaaaaaaaa", **{"id = 'x' --": 1}), ValueError))
    database.log_event(legacy, "aaaaaaaaaaaaaaaa", "received", "test", note="é")
    check("journal d'audit lisible", database.events(legacy, "aaaaaaaaaaaaaaaa")[0]["detail"] == {"note": "é"})
    database.delete(legacy, "aaaaaaaaaaaaaaaa")
    check("purge supprime aussi l'historique", database.events(legacy, "aaaaaaaaaaaaaaaa") == [])

    # ── Storage ──────────────────────────────────────────────────────────────────────
    storage.write(STORE, "bbbbbbbbbbbbbbbb", "photo_clean", b"png", ".png")
    storage.write(STORE, "bbbbbbbbbbbbbbbb", "photo_clean", b"jpeg", ".jpg")
    found = storage.find(STORE, "bbbbbbbbbbbbbbbb", "photo_clean")
    check("réécriture atomique, ancienne extension retirée", found is not None and found.suffix == ".jpg"
          and len(list(storage.folder(STORE, "bbbbbbbbbbbbbbbb").iterdir())) == 1)
    check("identifiant invalide jamais joint à un chemin", raises(lambda: storage.folder(STORE, "../etc"), ValueError))
    check("dossier orphelin repéré", storage.orphans(STORE, set(), min_age_seconds=0) == ["bbbbbbbbbbbbbbbb"])

    # ── Outbound URL guard (SSRF) ────────────────────────────────────────────────────
    for url in ("http://127.0.0.1/a.jpg", "http://10.0.0.5/a.jpg", "http://169.254.169.254/latest/meta-data",
                "http://[::1]/a.jpg", "http://[::ffff:127.0.0.1]/a.jpg", "http://192.168.1.1/x#.jpg",
                "ftp://93.184.216.34/a.jpg", "file:///etc/passwd", "http:///nohost"):
        check(f"URL refusée : {url}", raises(lambda url=url: outbound.check_url(url), outbound.FetchError))
    check("URL publique acceptée", not raises(lambda: outbound.check_url("https://93.184.216.34/a.jpg"), Exception))
    check("hôte hors liste refusé",
          raises(lambda: outbound.check_url("https://93.184.216.34/a.jpg", ("shop.example.fr",)), outbound.FetchError))

    # ── Input normalisation ──────────────────────────────────────────────────────────
    check("client JSON en chaîne décodé", workflow.normalise_customer('{"email": "a@b.fr"}') == {"email": "a@b.fr"})
    check("client texte encapsulé", workflow.normalise_customer("Camille") == {"raw": "Camille"})
    check("client liste encapsulé", workflow.normalise_customer([1, 2]) == {"raw": [1, 2]})
    check("client vide", workflow.normalise_customer(None) == {})
    check("client trop volumineux refusé",
          raises(lambda: workflow.normalise_customer({"x": "a" * 20_000}), workflow.WorkflowError))
    check("référence trop longue refusée",
          raises(lambda: workflow.normalise_reference("x" * 200), workflow.WorkflowError))
    check("empreinte stable", workflow.fingerprint(b"a", b"b", "r") == workflow.fingerprint(b"a", b"b", "r")
          and workflow.fingerprint(b"a", b"b", "r") != workflow.fingerprint(b"a", b"b", "s"))

    # ── Authentication helpers ───────────────────────────────────────────────────────
    limiter = FailureLimiter(limit=3, window=60)
    for _ in range(3):
        limiter.fail("1.2.3.4")
    check("limiteur bloque après les échecs", limiter.blocked("1.2.3.4") and not limiter.blocked("5.6.7.8"))
    limiter.reset("1.2.3.4")
    check("limiteur réinitialisé après succès", not limiter.blocked("1.2.3.4"))
    check("nom de contrôleur assaini", reviewer_name("<script>alert(1)</script>Jean") == "scriptalert1scriptJean")
    check("nom de contrôleur borné", len(reviewer_name("x" * 500)) == 64)
    check("nom de contrôleur par défaut", reviewer_name("") == "api")

    # ── Image header inspection ──────────────────────────────────────────────────────
    ok, small = cv2.imencode(".png", np.full((40, 60, 3), 200, np.uint8))
    check("dimensions lues depuis l'en-tête", imaging.inspect(small.tobytes(), 1_000_000) == (60, 40))
    check("image trop grande refusée",
          raises(lambda: imaging.inspect(small.tobytes(), 1_000), imaging.ImageRejected))
    check("octets illisibles refusés", raises(lambda: imaging.inspect(b"not an image", 10**9), imaging.ImageRejected))

    # ── Signature orientation policy ─────────────────────────────────────────────────
    sheet = np.zeros((300, 900), np.uint8)
    cv2.polylines(sheet, [np.array([[60, 220], [300, 80], [520, 230], [840, 90]])], False, 255, 5)
    cv2.line(sheet, (60, 250), (840, 250), 255, 4)
    upright = validator.detect_orientation(sheet)
    check("signature horizontale jamais tournée", upright.degrees == 0, str(upright))
    check("signature retournée : gardée telle quelle",
          validator.detect_orientation(cv2.rotate(sheet, cv2.ROTATE_180)).degrees == 0)
    sideways = validator.detect_orientation(cv2.rotate(sheet, cv2.ROTATE_90_CLOCKWISE))
    check("signature verticale remise à l'horizontale", sideways.degrees in (90, 270), str(sideways))
    tilted = validator.rotate_image(sheet, 30, cv2.INTER_NEAREST)
    check("aucun redressement automatique d'angle libre", validator.detect_orientation(tilted).degrees == 0)
    check("masque tourné sans valeurs intermédiaires", set(np.unique(tilted)) <= {0, 255})

    # Regression on the real samples: upright signatures came out at 225°, 218°, 176°…
    # when a PCA deskew decided their orientation.
    samples = sorted(p for p in (PROJECT / "input").iterdir() if p.suffix.lower() in validator.SUPPORTED)
    rows = {row.filename: row for row in validator.process_files(samples, STORE / "sig", margin=6, max_bytes=50_000)}
    for name, expected in (("Black-Minimalist-Signature-Typography-Studio-Logo.png", (0,)), ("IMG_0645.jpeg", (0,)),
                           ("IMG_5856.jpeg", (0,)), ("image (1) copie.jpg", (0,)), ("signature (1).jpg", (0,)),
                           ("image (1).jpg", (90, 270))):
        row = rows.get(name)
        check(f"orientation de l'exemple {name}", row is not None and row.orientation_degrees in expected
              and not row.failures, json.dumps(row.__dict__ if row else {}, default=str)[:200])

    print(f"\néchecs : {failures or 'aucun'}")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(STORE, ignore_errors=True)
