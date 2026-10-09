"""Authentication for the two exposed surfaces: the Make webhook and the review UI."""
from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from .config import settings

basic = HTTPBasic(realm="CERTIF ID review", auto_error=False)


def require_ingest_key(request: Request) -> None:
    """Shared-secret check for machine callers (Make.com, WordPress).

    Without a configured key the endpoint refuses every call: an identity-document
    intake must never be reachable anonymously because a variable was forgotten.
    """
    if not settings.ingest_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "INGEST_API_KEY n'est pas configuré sur le serveur.",
        )
    supplied = request.headers.get("x-api-key") or request.query_params.get("key") or ""
    if not secrets.compare_digest(supplied, settings.ingest_api_key):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Clé d'API invalide.")


def require_reviewer(request: Request, credentials: HTTPBasicCredentials | None = Depends(basic)) -> str:
    """HTTP Basic for the controller UI or API Key for WordPress REST calls.

    Returns the reviewer name for the audit trail.
    """
    # 1. Allow API Key authentication (for WordPress plugin server-to-server calls)
    api_key = request.headers.get("x-api-key") or request.query_params.get("key") or ""
    if api_key and settings.ingest_enabled and secrets.compare_digest(api_key, settings.ingest_api_key):
        return request.headers.get("x-reviewer-user") or request.query_params.get("reviewer") or "wordpress_admin"

    # 2. Check HTTP Basic
    if not settings.admin_enabled:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "ADMIN_USER / ADMIN_PASSWORD ne sont pas configurés sur le serveur.",
        )
    if credentials is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Authentification requise.",
            headers={"WWW-Authenticate": 'Basic realm="CERTIF ID review"'},
        )
    user_ok = secrets.compare_digest(credentials.username, settings.admin_user)
    password_ok = secrets.compare_digest(credentials.password, settings.admin_password)
    if not (user_ok and password_ok):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "Identifiants incorrects.",
            headers={"WWW-Authenticate": 'Basic realm="CERTIF ID review"'},
        )
    return credentials.username

