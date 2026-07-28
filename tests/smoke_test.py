"""End-to-end smoke test of the submission microservice.

Self-contained: it draws its own portrait and its own signatures (both contrast
directions), writes to a temporary storage folder and answers its own webhook, so it
touches neither the production data nor the internet.

    py tests/smoke_test.py
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import cv2
import numpy as np

PROJECT = Path(__file__).resolve().parents[1]
STORE = Path(tempfile.mkdtemp(prefix="ephoto-smoke-"))

received: list[dict] = []
failures: list[str] = []


# ── Fixtures ────────────────────────────────────────────────────────────────────────
def draw_portrait(width: int = 900, height: int = 1200) -> bytes:
    """A synthetic head-and-shoulders shot the Haar cascade recognises as a face."""
    image = np.zeros((height, width, 3), np.uint8)
    for y in range(height):  # uneven background, so the flattening path is exercised
        image[y, :] = (150 + 60 * y / height, 150 + 50 * y / height, 145 + 55 * y / height)
    cv2.circle(image, (int(0.15 * width), int(0.2 * height)), 180, (90, 110, 140), -1)

    cx, cy = width // 2, int(0.44 * height)
    head_w, head_h = int(0.20 * width), int(0.26 * height)
    skin = (150, 180, 214)
    cv2.ellipse(image, (cx, int(0.98 * height)), (int(0.36 * width), int(0.22 * height)), 0, 180, 360, (90, 80, 120), -1)
    cv2.ellipse(image, (cx, cy - int(0.12 * head_h)), (int(head_w * 1.12), int(head_h * 1.08)), 0, 0, 360, (40, 42, 58), -1)
    cv2.ellipse(image, (cx, cy), (head_w, head_h), 0, 0, 360, skin, -1)
    cv2.ellipse(image, (cx, cy + int(0.95 * head_h)), (int(0.34 * head_w), int(0.22 * head_h)), 0, 0, 180, skin, -1)

    eye_dx, eye_y = int(0.42 * head_w), cy - int(0.18 * head_h)
    eye_w, eye_h = int(0.19 * head_w), int(0.09 * head_h)
    for sign in (-1, 1):
        ex = cx + sign * eye_dx
        cv2.ellipse(image, (ex, eye_y), (eye_w, eye_h), 0, 0, 360, (245, 245, 245), -1)
        cv2.circle(image, (ex, eye_y), int(eye_h * 0.85), (60, 55, 50), -1)
        cv2.circle(image, (ex, eye_y), int(eye_h * 0.4), (20, 18, 16), -1)
        cv2.ellipse(image, (ex, eye_y - int(1.9 * eye_h)), (int(eye_w * 1.15), int(eye_h * 0.55)), 0, 180, 360, (45, 45, 60), -1)
        cv2.ellipse(image, (ex, eye_y), (eye_w, eye_h), 0, 0, 360, (110, 120, 150), 2)
    cv2.line(image, (cx, eye_y + int(0.15 * head_h)), (cx - 6, cy + int(0.28 * head_h)), (120, 150, 185), 3)
    cv2.ellipse(image, (cx, cy + int(0.30 * head_h)), (int(0.10 * head_w), 4), 0, 0, 180, (100, 120, 160), -1)
    cv2.line(image, (cx - int(0.22 * head_w), cy + int(0.52 * head_h)), (cx + int(0.22 * head_w), cy + int(0.52 * head_h)), (95, 105, 150), 3)
    return cv2.imencode(".png", cv2.GaussianBlur(image, (3, 3), 0))[1].tobytes()


def draw_signature(inverted: bool = False) -> bytes:
    """A handwriting-like trace, dark on light or — when inverted — light on dark."""
    canvas = np.full((420, 1100, 3), 250, np.uint8)
    ink = (35, 30, 30)
    cv2.ellipse(canvas, (300, 250), (170, 90), 12, 30, 330, ink, 4, cv2.LINE_AA)
    cv2.ellipse(canvas, (520, 220), (90, 130), -20, 0, 300, ink, 4, cv2.LINE_AA)
    cv2.polylines(canvas, [np.array([[130, 300], [420, 130], [700, 280], [960, 120]])], False, ink, 4, cv2.LINE_AA)
    cv2.polylines(canvas, [np.array([[600, 300], [720, 180], [780, 300]])], False, ink, 3, cv2.LINE_AA)
    if inverted:
        canvas = cv2.bitwise_not(canvas)
    return cv2.imencode(".png", canvas)[1].tobytes()


class Hook(BaseHTTPRequestHandler):
    """Stands in for the Make.com webhook."""

    def do_POST(self) -> None:
        body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
        received.append({"headers": dict(self.headers), "json": json.loads(body)})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"ok":true}')

    def log_message(self, *args) -> None:
        pass


def check(label: str, condition: bool, detail: str = "") -> None:
    print(f"{'PASS' if condition else 'FAIL'}  {label}{'' if condition else '  → ' + detail}")
    if not condition:
        failures.append(label)


# ── Run ─────────────────────────────────────────────────────────────────────────────
def main() -> int:
    server = HTTPServer(("127.0.0.1", 0), Hook)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    os.environ.update(
        STORAGE_DIR=str(STORE),
        INGEST_API_KEY="test-ingest-key",
        ADMIN_USER="controleur",
        ADMIN_PASSWORD="secret-review",
        MAKE_WEBHOOK_URL=f"http://127.0.0.1:{server.server_port}/hook",
        MAKE_WEBHOOK_TOKEN="hook-token",
        PUBLIC_BASE_URL="https://signature.example.fr",
    )
    sys.path.insert(0, str(PROJECT))
    os.chdir(PROJECT)

    from fastapi.testclient import TestClient

    from service.main import app

    photo_bytes = draw_portrait()
    signature_bytes = draw_signature()
    inverted_signature = draw_signature(inverted=True)
    api_key = {"X-API-Key": "test-ingest-key"}
    auth = ("controleur", "secret-review")

    with TestClient(app) as client:
        # Authentication
        check("ingest sans clé refusé", client.post("/api/v1/ingest", json={}).status_code == 401)
        check("ingest mauvaise clé refusé",
              client.post("/api/v1/ingest", json={}, headers={"X-API-Key": "nope"}).status_code == 401)
        check("dashboard sans auth refusé", client.get("/admin/dashboard").status_code == 401)
        check("dashboard mauvais mot de passe refusé",
              client.get("/admin/dashboard", auth=("controleur", "wrong")).status_code == 401)
        check("dossier de stockage non servi", client.get("/storage/ephoto.sqlite3").status_code == 404)
        check("code source non servi", client.get("/service/main.py").status_code == 404)

        # The public signature tool keeps working under the new entry point
        root = client.get("/")
        check("page publique servie", root.status_code == 200 and "Signature Check" in root.text)
        check("santé de l'API", client.get("/api/health").json()["status"] == "ok")

        # JSON intake, the shape Make.com sends
        response = client.post("/api/v1/ingest", headers=api_key, json={
            "order_id": "WC-10245",
            "customer": {"first_name": "Camille", "last_name": "Roux", "email": "camille@example.fr"},
            "photo": "data:image/png;base64," + base64.b64encode(photo_bytes).decode(),
            "signature": {"filename": "sig.png", "base64": base64.b64encode(signature_bytes).decode()},
        })
        check("ingest JSON accepté (202)", response.status_code == 202, response.text[:200])
        first = response.json()["submission_id"]
        check("review_url absolu", response.json()["review_url"].startswith("https://signature.example.fr/admin/"))

        state = client.get(f"/api/v1/submissions/{first}", headers=api_key).json()
        check("traitement terminé", state["status"] == "pending", json.dumps(state)[:300])
        detector = client.get("/api/health").json()["face_detector"]
        if detector == "none":
            # OpenCV 5 dropped the cascades and MediaPipe is absent: geometry cannot be
            # measured at all. requirements.txt pins opencv < 5 precisely to avoid this.
            print("INFO  aucun détecteur de visage disponible — contrôles géométriques ignorés")
        else:
            check("visage détecté", state["photo"]["metadata"]["detector"] == detector,
                  json.dumps(state["photo"]["metadata"])[:200])
            check("hauteur de tête dans la bande ANTS",
                  0.70 <= state["photo"]["metadata"]["head_ratio"] <= 0.80,
                  str(state["photo"]["metadata"]["head_ratio"]))
        check("photo au ratio ANTS",
              (state["photo"]["width"], state["photo"]["height"]) in ((414, 532), (828, 1064)),
              f"{state['photo']['width']}×{state['photo']['height']}")
        check("photo sous 2 Mo", state["photo"]["bytes"] <= 2_000_000, str(state["photo"]["bytes"]))
        check("signature 521×134", (state["signature"]["width"], state["signature"]["height"]) == (521, 134),
              f"{state['signature']['width']}×{state['signature']['height']}")
        check("signature conforme", state["signature"]["compliant"] is True, json.dumps(state["signature"])[:300])

        # Review panel
        board = client.get("/admin/dashboard", auth=auth)
        check("dashboard affiche le dossier", board.status_code == 200 and first in board.text)
        check("dashboard montre le client", "camille@example.fr" in board.text)
        detail = client.get(f"/admin/submissions/{first}", auth=auth)
        check("fiche dossier rendue", detail.status_code == 200 and "Accepter et transmettre" in detail.text)
        check("aucun résidu de gabarit", "{{" not in detail.text and ">None<" not in detail.text)
        check("checklists affichées",
              "Hauteur du visage" in detail.text and "Fidélité du tracé" in detail.text)
        for kind, expected in (("photo_original", "image/png"), ("photo_clean", "image/jpeg"),
                               ("signature_original", "image/png"), ("signature_clean", "image/png")):
            response = client.get(f"/admin/files/{first}/{kind}", auth=auth)
            check(f"fichier {kind} servi en {expected}",
                  response.status_code == 200 and response.headers["content-type"] == expected and len(response.content) > 500,
                  f"{response.status_code} {response.headers.get('content-type')}")
        check("fichiers protégés", client.get(f"/admin/files/{first}/photo_clean").status_code == 401)

        # Manual re-crop
        before = client.get(f"/api/v1/submissions/{first}", headers=api_key).json()["photo"]["metadata"]
        recrop = client.post(f"/admin/submissions/{first}/recrop", auth=auth,
                             data={"zoom": "1.2", "dx": "0.05", "dy": "-0.03"}, follow_redirects=False)
        after = client.get(f"/api/v1/submissions/{first}", headers=api_key).json()["photo"]["metadata"]
        check("recadrage manuel appliqué",
              recrop.status_code == 303 and after["crop_box"] != before["crop_box"],
              f"{before['crop_box']} → {after['crop_box']}")
        if detector != "none":  # without landmarks there is no head ratio to compare
            check("zoom arrière réduit la tête", after["head_ratio"] < before["head_ratio"],
                  f"{before['head_ratio']} → {after['head_ratio']}")

        # Accept, and transmit to the webhook
        decision = client.post(f"/admin/submissions/{first}/decision", auth=auth,
                               data={"action": "accept", "note": "OK"}, follow_redirects=False)
        check("acceptation redirige", decision.status_code == 303, decision.text[:200])
        check("webhook Make reçu", len(received) == 1)
        if received:
            sent = received[0]["json"]
            check("jeton transmis", received[0]["headers"].get("Authorization") == "Bearer hook-token")
            check("payload complet", {"submission_id", "customer", "photo", "signature", "reports"} <= set(sent))
            check("photo transmise en base64", len(base64.b64decode(sent["photo"]["base64"])) > 1000)
            check("signature transmise en PNG", sent["signature"]["media_type"] == "image/png")
            check("client transmis", sent["customer"]["email"] == "camille@example.fr")
        check("dossier archivé accepté",
              client.get(f"/api/v1/submissions/{first}", headers=api_key).json()["status"] == "accepted")
        check("double décision refusée",
              client.post(f"/api/v1/validate/{first}", json={"action": "accept"}, auth=auth).status_code == 409)

        # Multipart intake, inverted signature, rejection
        response = client.post(
            "/api/v1/ingest", headers=api_key,
            files={"photo": ("face.png", photo_bytes, "image/png"),
                   "signature": ("sig.png", inverted_signature, "image/png")},
            data={"order_id": "WC-10246", "customer": json.dumps({"email": "leo@example.fr"})},
        )
        check("ingest multipart accepté", response.status_code == 202, response.text[:200])
        second = response.json()["submission_id"]
        state = client.get(f"/api/v1/submissions/{second}", headers=api_key).json()
        check("signature inversée détectée", state["signature"]["metadata"].get("inverted") is True,
              json.dumps(state["signature"].get("metadata"))[:200])
        check("signature inversée conforme", state["signature"]["compliant"] is True)

        rejected = client.post(f"/api/v1/validate/{second}", json={"action": "reject", "reason": "photo floue"}, auth=auth)
        check("refus enregistré", rejected.status_code == 200 and rejected.json()["status"] == "rejected")
        check("aucun envoi après refus", len(received) == 1)
        check("vue des refusés", second in client.get("/admin/dashboard?show=rejected", auth=auth).text)

        # Manual creation from the panel: same pipelines, but processed inline
        check("formulaire manuel protégé", client.get("/admin/nouveau").status_code == 401)
        form = client.get("/admin/nouveau", auth=auth)
        check("formulaire manuel rendu", form.status_code == 200 and 'name="signature"' in form.text)
        manual = client.post(
            "/admin/nouveau", auth=auth, follow_redirects=False,
            files={"photo": ("p.png", photo_bytes, "image/png"),
                   "signature": ("s.png", signature_bytes, "image/png")},
            data={"order_id": "COMPTOIR-7", "name": "Alex Comptoir", "email": "alex@example.fr"},
        )
        check("dossier manuel créé", manual.status_code == 303, manual.text[:200])
        third = manual.headers["location"].rsplit("/", 1)[-1]
        state = client.get(f"/api/v1/submissions/{third}", headers=api_key).json()
        check("dossier manuel déjà traité à l'arrivée", state["status"] == "pending", json.dumps(state)[:200])
        check("dossier manuel complet",
              state["photo"]["width"] > 0 and state["signature"]["width"] == 521,
              json.dumps(state)[:200])
        check("fiche manuelle affiche le client",
              "alex@example.fr" in client.get(f"/admin/submissions/{third}", auth=auth).text)
        check("lien vers le formulaire sur le dashboard",
              "/admin/nouveau" in client.get("/admin/dashboard", auth=auth).text)
        check("fichier vide refusé",
              client.post("/admin/nouveau", auth=auth, follow_redirects=False,
                          files={"photo": ("p.png", b"", "image/png"),
                                 "signature": ("s.png", signature_bytes, "image/png")}).status_code == 422)

        # Malformed input
        check("base64 invalide rejeté",
              client.post("/api/v1/ingest", json={"photo": "%%%", "signature": "%%%"},
                          headers=api_key).status_code in (422, 500))
        check("dossier inconnu → 404", client.get("/api/v1/submissions/deadbeef", headers=api_key).status_code == 404)

    server.shutdown()
    print(f"\néchecs : {failures or 'aucun'}")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    finally:
        shutil.rmtree(STORE, ignore_errors=True)
