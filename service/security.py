"""Authentication for the two exposed surfaces: the Make webhook and the review UI."""
from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .config import settings

basic = HTTPBasic(realm="CERTIF ID review")


def require_ingest_key(request: Request) -> None:
    """Shared-secret check for machine callers (Make.com).

    Without a configured key the endpoint refuses every call: an identity-document
    intake must never be reachable anonymously because a variable was forgotten.
    """
    if not settings.ingest_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "INGEST_API_KEY n'est pas configuré sur le serveur.",
        )
    supplied = request.headers.get("x-api-key") or ""
    if not secrets.compare_digest(supplied, settings.ingest_api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Clé d'API invalide.")


def require_reviewer(credentials: HTTPBasicCredentials = Depends(basic)) -> str:
    """HTTP Basic for the controller UI; returns the reviewer name for the audit trail."""
    if not settings.admin_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ADMIN_USER / ADMIN_PASSWORD ne sont pas configurés sur le serveur.",
        )
    # Both comparisons always run, so the response time does not reveal which half failed.
    user_ok = secrets.compare_digest(credentials.username, settings.admin_user)
    password_ok = secrets.compare_digest(credentials.password, settings.admin_password)
    if not (user_ok and password_ok):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Identifiants incorrects.",
            headers={"WWW-Authenticate": 'Basic realm="CERTIF ID review"'},
        )
    return credentials.username
