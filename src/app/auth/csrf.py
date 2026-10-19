"""Session-bound CSRF Double-Submit token."""
from __future__ import annotations

import secrets

from fastapi import HTTPException, Request, status

_SESSION_KEY = "_csrf_token"
_HEADER_NAME = "x-csrf-token"


def get_csrf_token(request: Request) -> str:
    """Return the session CSRF token, generating one if absent."""
    token: str | None = request.session.get(_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        request.session[_SESSION_KEY] = token
    return token


def verify_csrf(request: Request, form_token: str | None = None) -> None:
    """Verify the CSRF token from the request header or from a form field.

    HTMX and `fetch` calls send the header. A plain HTML form cannot set one, so a route
    that is the target of such a form reads its `csrf_token` field and passes it as
    `form_token`. Either one is enough if it matches the session token.

    Raises 403 if the token is missing or does not match the session token.
    """
    session_token: str | None = request.session.get(_SESSION_KEY)
    if not session_token:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="csrf token missing")

    for submitted in (request.headers.get(_HEADER_NAME), form_token):
        # Compared as bytes: `compare_digest` refuses a str with non-ASCII characters, and a
        # form field is arbitrary text.
        if submitted and secrets.compare_digest(submitted.encode(), session_token.encode()):
            return

    raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="csrf token invalid")
