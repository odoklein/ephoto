"""Authentication for the exposed surfaces: Make intake, server-to-server review, review UI.

Keys travel in the `X-API-Key` header only.  A key in the query string ends up in
access logs, proxy logs, browser history and Referer headers — and once it was being
written into `<img src>` attributes that every shop manager's browser could read.
"""
from __future__ import annotations

import re
import secrets
import threading
import time
from collections import defaultdict, deque

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .config import settings

basic = HTTPBasic(realm="CERTIF ID review", auto_error=False)
CHALLENGE = {"WWW-Authenticate": 'Basic realm="CERTIF ID review"'}
REVIEWER_UNSAFE = re.compile(r"[^\w .@+'-]", re.UNICODE)
REVIEWER_MAX = 64


def _same(supplied: str, expected: str) -> bool:
    """Constant-time comparison that cannot raise.

    compare_digest() rejects non-ASCII *str* with a TypeError, which turned a header
    holding an accented character into a 500; comparing bytes avoids that.
    """
    if not supplied or not expected:
        return False
    return secrets.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))


class FailureLimiter:
    """Refuse a client for a while after repeated authentication failures.

    In memory and per process, which matches the single-worker deployment: the point is
    to make guessing the reviewer password from one address impractical, not to be a WAF.
    """

    def __init__(self, limit: int = 10, window: float = 300.0) -> None:
        self.limit, self.window = limit, window
        self._failures: dict[str, deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def _recent(self, client: str, now: float) -> deque[float]:
        attempts = self._failures[client]
        while attempts and now - attempts[0] > self.window:
            attempts.popleft()
        return attempts

    def blocked(self, client: str) -> bool:
        with self._lock:
            return len(self._recent(client, time.monotonic())) >= self.limit

    def fail(self, client: str) -> None:
        with self._lock:
            now = time.monotonic()
            self._recent(client, now).append(now)
            if len(self._failures) > 10_000:  # bound memory under a spray of addresses
                self._failures.clear()

    def reset(self, client: str) -> None:
        with self._lock:
            self._failures.pop(client, None)


limiter = FailureLimiter()


def _client(request: Request) -> str:
    return request.client.host if request.client else "unknown"


def _refuse(request: Request, detail: str, challenge: bool = False) -> HTTPException:
    limiter.fail(_client(request))
    return HTTPException(status.HTTP_401_UNAUTHORIZED, detail, headers=CHALLENGE if challenge else None)


def _guard(request: Request) -> None:
    if limiter.blocked(_client(request)):
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Trop d'échecs d'authentification ; réessayez plus tard.")


def _supplied_key(request: Request) -> str:
    return request.headers.get("x-api-key", "").strip()


def reviewer_name(raw: str) -> str:
    """A delegated reviewer name fit for the audit trail and the panel."""
    cleaned = REVIEWER_UNSAFE.sub("", raw or "").strip()[:REVIEWER_MAX]
    return cleaned or "api"


def require_ingest_key(request: Request) -> str:
    """Shared-secret check for the intake (Make.com, WordPress auto-ingest).

    Without a configured key the endpoint refuses every call: an identity-document
    intake must never be reachable anonymously because a variable was forgotten.
    """
    if not settings.ingest_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "INGEST_API_KEY n'est pas configuré sur le serveur.")
    _guard(request)
    if not _same(_supplied_key(request), settings.ingest_api_key):
        raise _refuse(request, "Clé d'API invalide.")
    limiter.reset(_client(request))
    return "ingest"


def require_reader(request: Request) -> str:
    """Read access to a dossier's status: the intake key or the review key."""
    if not (settings.ingest_enabled or settings.review_key_enabled):
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "Aucune clé d'API n'est configurée sur le serveur.")
    _guard(request)
    supplied = _supplied_key(request)
    for name, expected in (("ingest", settings.ingest_api_key), ("review", settings.review_api_key)):
        if _same(supplied, expected):
            limiter.reset(_client(request))
            return name
    raise _refuse(request, "Clé d'API invalide.")


def require_reviewer(request: Request, credentials: HTTPBasicCredentials | None = Depends(basic)) -> str:
    """HTTP Basic for the controller UI, or REVIEW_API_KEY for server-to-server calls.

    Returns the reviewer name for the audit trail.  With the API key the calling server
    vouches for its own user through `X-Reviewer-User`; the name is sanitised and marked
    as delegated so the panel never presents it as a local login.
    """
    if not (settings.admin_enabled or settings.review_key_enabled):
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ADMIN_USER / ADMIN_PASSWORD (ou REVIEW_API_KEY) ne sont pas configurés sur le serveur.",
        )
    _guard(request)

    supplied = _supplied_key(request)
    if supplied:
        # A key that is present but wrong is refused outright rather than falling through
        # to Basic: the caller clearly meant to use a key, and the ingest key — however
        # valid for /ingest — grants no review rights here.
        if settings.review_key_enabled and _same(supplied, settings.review_api_key):
            # Every WordPress reviewer shares the shop server's address: a good key
            # clears the count so one mistyped setting cannot lock them all out.
            limiter.reset(_client(request))
            request.state.auth_channel = "api"
            return f"{reviewer_name(request.headers.get('x-reviewer-user', ''))} (API)"
        raise _refuse(request, "Clé de contrôle invalide.")

    if not settings.admin_enabled:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, "ADMIN_USER / ADMIN_PASSWORD ne sont pas configurés.")
    if credentials is None:
        # The browser's first, credential-less request is part of the Basic handshake;
        # it is not counted as a failure.
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Authentification requise.", headers=CHALLENGE)
    user_ok = _same(credentials.username, settings.admin_user)
    password_ok = _same(credentials.password, settings.admin_password)
    if not (user_ok and password_ok):
        raise _refuse(request, "Identifiants incorrects.", challenge=True)
    limiter.reset(_client(request))
    request.state.auth_channel = "basic"
    return credentials.username
