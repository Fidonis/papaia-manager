"""OIDC Authorization Code Flow routes: login, callback, logout."""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Annotated
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import RedirectResponse, Response

from app.auth.csrf import verify_csrf
from app.auth.oidc import OIDCError, _pkce_pair, get_oidc_client
from app.auth.roles import has_manager_access
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth")


def _safe_next(raw: str) -> str | None:
    """Return `raw` if it is a safe same-origin path to redirect back to.

    Only absolute paths on this origin: no scheme, no host, no
    protocol-relative "//host" form, no backslash or control-character tricks.
    """
    if not raw or not raw.startswith("/") or raw.startswith("//"):
        return None
    if any(ch in raw for ch in ("\\", "\x00", "\n", "\r", "\t")):
        return None
    parts = urlsplit(raw)
    if parts.scheme or parts.netloc:
        return None
    return raw


def _make_state(secret: str, verifier: str) -> str:
    """Embed the PKCE verifier in an HMAC-signed state token.

    Format: <sha256_hex_64chars>.<verifier_43chars>
    The verifier uses only [A-Za-z0-9-_] so the single dot is an unambiguous
    separator. Signing prevents state forgery without relying on the session cookie.
    """
    mac = hmac.new(secret.encode(), verifier.encode(), hashlib.sha256).hexdigest()
    return f"{mac}.{verifier}"


def _parse_state(secret: str, state: str) -> str | None:
    """Verify state signature and return the embedded verifier, or None on failure."""
    if "." not in state:
        return None
    mac_part, verifier = state.split(".", 1)
    expected = hmac.new(secret.encode(), verifier.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(mac_part, expected):
        return None
    return verifier


@router.get("/login")
async def login(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    next_: Annotated[str, Query(alias="next")] = "",
) -> RedirectResponse:
    """Redirect the browser to Keycloak with a PKCE challenge.

    A safe same-origin `next` path is remembered in the session so the
    callback can return the browser to where it was sent away from.
    """
    dest = _safe_next(next_)
    if dest is not None:
        request.session["post_login_redirect"] = dest

    client = get_oidc_client()
    verifier, challenge = _pkce_pair()
    state = _make_state(settings.manager_session_secret, verifier)
    auth_url = client.build_auth_url(state, challenge)
    logger.info("auth/login: redirecting, state=%s...", state[:8])
    return RedirectResponse(auth_url, status_code=status.HTTP_302_FOUND)


@router.get("/callback")
async def callback(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
    code: str = "",
    state: str = "",
    error: str = "",
) -> Response:
    """Handle the Keycloak redirect; validate state, exchange code, set session."""
    if error:
        logger.warning("OIDC error from provider: %s", error)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"authentication error: {error}",
        )

    code_verifier = _parse_state(settings.manager_session_secret, state)
    if not code_verifier:
        logger.warning("auth/callback: invalid or missing state parameter")
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing OIDC session state — please start login again",
        )
    if not code:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing authorization code",
        )

    client = get_oidc_client()
    try:
        token_set = await client.exchange_code(code=code, code_verifier=code_verifier)
    except OIDCError as exc:
        logger.warning("OIDC token exchange failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication failed",
        ) from exc

    claims = token_set.claims
    # Gate the session on holding *any* known role. Per-surface authorization
    # happens in the route dependencies; rejecting non-admins here would lock
    # dashboard users out of the application entirely.
    if not has_manager_access(claims, settings):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="your account does not have a role granting access to this application",
        )

    request.session["user"] = claims.to_dict()
    if token_set.refresh_token:
        # Lets the session be renewed on its own until the Keycloak SSO session
        # itself ends -- see app.auth.deps._refresh_session.
        request.session["oidc"] = {"refresh_token": token_set.refresh_token}
    logger.info("user %s authenticated successfully", claims.preferred_username)

    dest = request.session.pop("post_login_redirect", "/")
    if not isinstance(dest, str) or not dest.startswith("/"):
        dest = "/"
    return RedirectResponse(dest, status_code=status.HTTP_302_FOUND)


@router.post("/logout")
async def logout(request: Request) -> Response:
    """Clear the session and redirect to the login page."""
    verify_csrf(request)
    request.session.clear()
    return RedirectResponse("/auth/login", status_code=status.HTTP_302_FOUND)


@router.get("/logout")
async def logout_get(request: Request) -> Response:
    """GET /auth/logout for convenience (no CSRF required — only clears session)."""
    request.session.clear()
    return RedirectResponse("/auth/login", status_code=status.HTTP_302_FOUND)
