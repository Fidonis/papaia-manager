"""OIDC Authorization Code Flow routes: login, callback, logout."""
from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response

from app.auth.csrf import verify_csrf
from app.auth.oidc import OIDCClient, OIDCError, _pkce_pair
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth")


def _oidc_client(settings: Settings) -> OIDCClient:
    return OIDCClient(
        auth_endpoint=settings.oidc_issuer_kc_auth,
        token_endpoint=settings.oidc_issuer_kc_token,
        jwks_endpoint=settings.oidc_issuer_kc_certs,
        client_id=settings.manager_oidc_client_id,
        client_secret=settings.manager_oidc_client_secret,
        redirect_uri=settings.oidc_redirect_uri,
        role_claim=settings.oidc_role_claim,
        ssl_cert_file=settings.ssl_cert_file,
    )


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
) -> RedirectResponse:
    """Redirect the browser to Keycloak with a PKCE challenge."""
    client = _oidc_client(settings)
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

    client = _oidc_client(settings)
    try:
        claims = await client.exchange_code(code=code, code_verifier=code_verifier)
    except OIDCError as exc:
        logger.warning("OIDC token exchange failed: %s", exc)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="authentication failed",
        ) from exc

    if settings.manager_admin_role not in claims.roles:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="your account does not have the required admin role",
        )

    request.session["user"] = claims.to_dict()
    logger.info("user %s authenticated successfully", claims.preferred_username)
    return RedirectResponse("/", status_code=status.HTTP_302_FOUND)


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
