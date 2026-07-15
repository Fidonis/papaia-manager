"""Session-bound CSRF Double-Submit token."""
from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, status

_SESSION_KEY = "_csrf_token"
_HEADER_NAME = "x-csrf-token"
_FORM_FIELD = "csrf_token"


def get_csrf_token(request: Request) -> str:
    """Return the session CSRF token, generating one if absent."""
    token: str | None = request.session.get(_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[_SESSION_KEY] = token
    return token


def verify_csrf(request: Request) -> None:
    """Verify the CSRF token from the request header or form field.

    Raises 403 if the token is missing or does not match the session token.
    """
    session_token: str | None = request.session.get(_SESSION_KEY)
    if not session_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="csrf token missing")

    submitted = request.headers.get(_HEADER_NAME)
    if submitted and secrets.compare_digest(submitted, session_token):
        return

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="csrf token invalid")
