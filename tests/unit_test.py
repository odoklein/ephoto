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
    check("dossier orphelin repéré", storage.orphans(STORE, set(), min_age_seconds=-60) == ["bbbbbbbbbbbbbbbb"])

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

    # Background tint: a beige or cream wall has the luminance of the regulation grey, so a
    # luminance-only check let it through at 100 % (order #3559, refused by hand for « Fond »).
    from service.processing import photo_processor

    def wall(hex_rgb: str) -> np.ndarray:
        rgb = tuple(int(hex_rgb[i:i + 2], 16) for i in (1, 3, 5))
        return np.full((532, 414, 3), rgb[::-1], dtype=np.uint8)

    for hex_rgb, expected in (("#d4d7d3", True), ("#d9d9d6", True), ("#d8cdb4", False),
                              ("#e6dcc0", False), ("#bcd4ec", False), ("#ffffff", False)):
        stats = photo_processor.background_stats(wall(hex_rgb))
        check(f"fond {hex_rgb} {'accepté' if expected else 'refusé'}", stats.ok is expected, stats.detail)

    # ── Background unification ───────────────────────────────────────────────────────
    # A synthetic sitter on a wall that is brighter than the target and vignetted, with a
    # pocket of wall inside the hair, a half-transparent rim, and a coarse mask that
    # spills 6 px onto the wall the way the 256-pixel segmentation does.  No MediaPipe.
    from service.processing import background

    height, width = 532, 414
    yy, xx = np.mgrid[0:height, 0:width].astype(np.float32)
    vignette = 1.0 - 0.10 * ((xx - width / 2) ** 2 + (yy - height / 2) ** 2) / (width / 2) ** 2
    sheet = np.clip(np.stack([226 * vignette, 230 * vignette, 232 * vignette], axis=2), 0, 255)
    head = ((xx - 207) / 120) ** 2 + ((yy - 250) / 160) ** 2 <= 1.0
    body = (yy > 400) & (np.abs(xx - 207) < 170)
    neck = (yy > 330) & (np.abs(xx - 207) < 70)
    subject = head | neck | body
    pocket = ((xx - 150) / 9) ** 2 + ((yy - 120) / 9) ** 2 <= 1.0  # wall seen between curls
    truth = (subject & ~pocket).astype(np.float32)
    soft = cv2.GaussianBlur(truth, (0, 0), 1.2)  # a real edge is half hair, half wall
    hair = np.array([40, 35, 30], np.float32)
    portrait = (soft[..., None] * hair + (1 - soft[..., None]) * sheet).astype(np.uint8)
    face = (147.0, 150.0, 120.0, 190.0)
    portrait[160:330, 150:265] = (120, 150, 190)  # skin, so the skin model has a reference
    spill = cv2.dilate(subject.astype(np.uint8), np.ones((13, 13), np.uint8)).astype(np.float32)
    coarse = cv2.GaussianBlur(spill, (0, 0), 3.0)
    matte = background.unify(portrait, coarse, face)
    check("unification : découpe acceptée", matte is not None)
    if matte is not None:
        far = cv2.distanceTransform((truth < 0.5).astype(np.uint8), cv2.DIST_L2, 5) > 4
        far &= ~pocket
        wall_error = background.delta_e(matte.image)[far]
        check("unification : fond loin du sujet exactement à la couleur imposée",
              float(wall_error.max()) < 0.6, f"ΔE max {wall_error.max():.2f}")
        rim = (cv2.distanceTransform((truth < 0.5).astype(np.uint8), cv2.DIST_L2, 5) > 0) & ~far & ~pocket
        target_luma = float(background.TARGET_BGR @ np.array([0.114, 0.587, 0.299], np.float32))
        rim_luma = matte.image[rim].astype(np.float32) @ np.array([0.114, 0.587, 0.299], np.float32)
        check("unification : aucun liseré plus clair que la couleur imposée",
              float(rim_luma.max()) <= target_luma + 3, f"max {rim_luma.max():.1f} vs {target_luma:.1f}")
        pocket_core = cv2.erode(pocket.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
        check("unification : poche de mur dans les cheveux remplacée",
              float(background.delta_e(matte.image)[pocket_core].mean()) < 3.0,
              f"ΔE {background.delta_e(matte.image)[pocket_core].mean():.1f}")
        inner = cv2.erode(truth.astype(np.uint8), np.ones((15, 15), np.uint8)).astype(bool)
        check("unification : sujet intact", np.array_equal(matte.image[inner], portrait[inner]))
        check("unification : résidu mesuré nul", matte.residual_wall < background.RESIDUAL_WALL_MAX,
              f"{matte.residual_wall:.4f}")
    check("unification : refus sans mur visible",
          background.unify(portrait, np.ones((height, width), np.float32), face) is None)

    print(f"\néchecs : {failures or 'aucun'}")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(STORE, ignore_errors=True)
