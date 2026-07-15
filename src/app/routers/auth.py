"""OIDC Authorization Code Flow routes: login, callback, logout."""
from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import RedirectResponse, Response

from app.auth.csrf import verify_csrf
from app.auth.oidc import OIDCClient, OIDCError
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


@router.get("/login")
async def login(
    request: Request,
    settings: Annotated[Settings, Depends(get_settings)],
) -> RedirectResponse:
    """Redirect the browser to Keycloak with a PKCE challenge."""
    client = _oidc_client(settings)
    auth_url, state, code_verifier = client.build_auth_redirect()
    request.session["oidc_state"] = state
    request.session["oidc_verifier"] = code_verifier
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

    session_state: str | None = request.session.pop("oidc_state", None)
    code_verifier: str | None = request.session.pop("oidc_verifier", None)

    if not session_state or not code_verifier:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="missing OIDC session state — please start login again",
        )
    if state != session_state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="state mismatch — possible CSRF attempt",
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
