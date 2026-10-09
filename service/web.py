"""Cross-cutting HTTP behaviour: hardening headers, CSRF origin check, body cap, access log.

Written as plain ASGI middleware rather than BaseHTTPMiddleware so that streamed file
responses and background work are untouched by it.
"""
from __future__ import annotations

import logging
import re
import secrets
import sys
import time
from urllib.parse import urlsplit

from fastapi import HTTPException, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.responses import JSONResponse
from fastapi.templating import Jinja2Templates
from starlette.datastructures import Headers, MutableHeaders
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.types import ASGIApp, Message, Receive, Scope, Send

log = logging.getLogger("ephoto.http")

UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
# Identity data lives behind these prefixes: never cached, never framed, and protected
# against cross-site form posts.
PROTECTED_PREFIXES = ("/admin", "/api/v1")
STATIC_PREFIX = "/admin/static/"
ADMIN_CSP = (
    "default-src 'self'; img-src 'self' data: blob:; style-src 'self'; script-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'"
)
REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


def configure_logging(level: str) -> None:
    """One line per event on stdout, which is what Dokploy collects."""
    root = logging.getLogger("ephoto")
    if root.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(getattr(logging, level, logging.INFO))
    root.propagate = False


class HardeningMiddleware:
    def __init__(self, app: ASGIApp, public_base_url: str = "", max_body_bytes: int = 0) -> None:
        self.app = app
        self.public_host = urlsplit(public_base_url).netloc.lower() if public_base_url else ""
        self.hsts = public_base_url.lower().startswith("https://")
        self.max_body_bytes = max_body_bytes

    def _same_origin(self, headers: Headers) -> bool:
        """Browser-sent Origin (or Referer) must name this site.

        HTTP Basic credentials are replayed by the browser on any request to the site,
        including a form another page posts to it, so SameSite cookies cannot help here.
        Server-to-server callers (Make, WordPress) send neither header and are let
        through; they authenticate with a key no other site can make a browser send.
        """
        claimed = headers.get("origin") or headers.get("referer")
        if not claimed:
            return True
        netloc = urlsplit(claimed).netloc.lower()
        if not netloc:  # "null" origin: sandboxed frame, privacy redirect
            return False
        return netloc in {headers.get("host", "").lower(), self.public_host} - {""}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        path, method = scope["path"], scope["method"]
        supplied_id = headers.get("x-request-id", "")
        request_id = supplied_id if REQUEST_ID.match(supplied_id) else secrets.token_hex(6)
        protected = path.startswith(PROTECTED_PREFIXES) and not path.startswith(STATIC_PREFIX)
        started = time.perf_counter()
        outcome = {"status": 500}

        async def send_with_headers(message: Message) -> None:
            if message["type"] == "http.response.start":
                outcome["status"] = message["status"]
                response_headers = MutableHeaders(scope=message)
                response_headers["X-Request-Id"] = request_id
                response_headers.setdefault("X-Content-Type-Options", "nosniff")
                response_headers.setdefault("Referrer-Policy", "same-origin")
                if protected:
                    response_headers["Cache-Control"] = "no-store, private"
                    response_headers["Pragma"] = "no-cache"
                    response_headers.setdefault("X-Frame-Options", "DENY")
                    if path.startswith("/admin"):
                        response_headers.setdefault("Content-Security-Policy", ADMIN_CSP)
                if self.hsts:
                    response_headers.setdefault("Strict-Transport-Security", "max-age=31536000")
            await send(message)

        try:
            if method in UNSAFE_METHODS and protected and not self._same_origin(headers):
                response = JSONResponse({"detail": "Requête d'une autre origine refusée."}, status_code=403)
                await response(scope, receive, send_with_headers)
                return
            declared = headers.get("content-length", "")
            if self.max_body_bytes and declared.isdigit() and int(declared) > self.max_body_bytes:
                response = JSONResponse({"detail": "Requête trop volumineuse."}, status_code=413)
                await response(scope, receive, send_with_headers)
                return
            await self.app(scope, self._capped(receive), send_with_headers)
        finally:
            elapsed = (time.perf_counter() - started) * 1000
            # The path only: query strings carry search terms, i.e. customer names.
            level = logging.DEBUG if path == "/api/health" else logging.INFO
            log.log(level, "%s %s %s %.0fms rid=%s", method, path, outcome["status"], elapsed, request_id)

    def _capped(self, receive: Receive) -> Receive:
        """Count streamed bytes for bodies that declared no length (chunked uploads)."""
        if not self.max_body_bytes:
            return receive
        total = 0

        async def capped() -> Message:
            nonlocal total
            message = await receive()
            if message["type"] == "http.request":
                total += len(message.get("body", b""))
                if total > self.max_body_bytes:
                    raise HTTPException(413, "Requête trop volumineuse.")
            return message

        return capped


def install_error_pages(app, templates: Jinja2Templates, context: dict) -> None:
    """Errors under /admin render as a page when a browser asked for one.

    The reviewer used to land on raw JSON after a failed transmission.  401 keeps its
    default response, whose WWW-Authenticate header drives the Basic login prompt.
    """
    from .workflow import WorkflowError  # noqa: PLC0415 - avoids an import cycle

    async def render(request: Request, status_code: int, message: str):
        wants_page = request.url.path.startswith("/admin") and "text/html" in request.headers.get("accept", "")
        if wants_page and status_code != 401:
            return templates.TemplateResponse(
                request, "error.html", {"status_code": status_code, "message": message, **context},
                status_code=status_code,
            )
        return None

    async def on_http_error(request: Request, error: StarletteHTTPException):
        page = await render(request, error.status_code, str(error.detail))
        return page or await http_exception_handler(request, error)

    async def on_workflow_error(request: Request, error: WorkflowError):
        page = await render(request, error.status_code, error.message)
        return page or JSONResponse({"detail": error.message}, status_code=error.status_code)

    app.add_exception_handler(StarletteHTTPException, on_http_error)
    app.add_exception_handler(WorkflowError, on_workflow_error)
