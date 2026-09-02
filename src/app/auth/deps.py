"""FastAPI dependencies for session-based authentication."""
from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status

from app.auth.oidc import OIDCClaims, OIDCError, get_oidc_client
from app.auth.roles import has_manager_access, is_admin
from app.config import Settings, get_settings

# Renew the session this many seconds before the access token actually expires,
# so a request landing right on the boundary still goes through.
_REFRESH_SKEW_SECONDS = 60

# One in-flight refresh per refresh token (keyed by its SHA-256): a burst of
# concurrent pollers arriving with the same expired cookie makes a single
# Keycloak round-trip instead of racing token rotation.
_refresh_locks: dict[str, asyncio.Lock] = {}

# The outcome of a completed refresh, kept for a short window so the pollers
# that lost the race copy it instead of presenting the now-rotated old token.
_refresh_results: dict[str, tuple[float, dict[str, Any]]] = {}
_REFRESH_RESULT_TTL_SECONDS = 60.0


async def get_current_user(request: Request) -> OIDCClaims:
    """Return the authenticated user from the session or raise 401.

    An access token within ``_REFRESH_SKEW_SECONDS`` of expiry (or already past
    it) is refreshed transparently against Keycloak while its SSO session is
    still alive. Only once the refresh token is gone or rejected does this
    raise, which the 401 handler turns into a login redirect.
    """
    raw = request.session.get("user")
    if raw is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="not authenticated",
            headers={"Location": "/auth/login"},
        )
    try:
        claims = OIDCClaims.from_dict(raw)
    except (KeyError, ValueError, TypeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="invalid session",
        ) from exc

    if claims.exp - time.time() >= _REFRESH_SKEW_SECONDS:
        return claims

    refreshed = await _refresh_session(request)
    if refreshed is not None:
        return refreshed

    if claims.is_expired:
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="session expired",
            headers={"Location": "/auth/login"},
        )
    # Still valid for a little longer: a transient Keycloak hiccup must not log
    # an active user out on the spot.
    return claims


async def _refresh_session(request: Request) -> OIDCClaims | None:
    """Renew ``request.session`` from the stored refresh token, or return None."""
    oidc_state = request.session.get("oidc")
    if not isinstance(oidc_state, dict):
        return None
    refresh_token = oidc_state.get("refresh_token")
    if not isinstance(refresh_token, str) or not refresh_token:
        return None

    key = hashlib.sha256(refresh_token.encode()).hexdigest()
    lock = _refresh_locks.setdefault(key, asyncio.Lock())
    async with lock:
        now = time.time()
        _prune_refresh_state(now)

        cached = _refresh_results.get(key)
        if cached is not None:
            _apply_session(request, cached[1])
            return OIDCClaims.from_dict(cached[1]["user"])

        try:
            token_set = await get_oidc_client().refresh(refresh_token=refresh_token)
        except OIDCError:
            return None

        payload = {
            "user": token_set.claims.to_dict(),
            "oidc": {"refresh_token": token_set.refresh_token or refresh_token},
        }
        _refresh_results[key] = (now, payload)
        _apply_session(request, payload)
        return token_set.claims


def _apply_session(request: Request, payload: dict[str, Any]) -> None:
    request.session["user"] = payload["user"]
    request.session["oidc"] = payload["oidc"]


def _prune_refresh_state(now: float) -> None:
    for key in [
        k
        for k, (ts, _) in _refresh_results.items()
        if now - ts > _REFRESH_RESULT_TTL_SECONDS
    ]:
        _refresh_results.pop(key, None)
    # Locks are cheap; only sweep them if the map has genuinely run away, and
    # only those with no recent result behind them.
    if len(_refresh_locks) > 512:
        live = set(_refresh_results)
        for key in [k for k in _refresh_locks if k not in live]:
            _refresh_locks.pop(key, None)


def require_admin(
    claims: Annotated[OIDCClaims, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> OIDCClaims:
    """Raise 403 if the authenticated user does not hold the admin role."""
    if not is_admin(claims, settings):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin role required",
        )
    return claims


def require_manager_access(
    claims: Annotated[OIDCClaims, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> OIDCClaims:
    """Raise 403 unless the user holds either the admin or the user role."""
    if not has_manager_access(claims, settings):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="admin or user role required",
        )
    return claims


# Route annotations. The name states the required tier, so a route's access
# level is readable at its signature instead of hidden in a shared alias.
AdminUser = Annotated[OIDCClaims, Depends(require_admin)]
AnyUser = Annotated[OIDCClaims, Depends(require_manager_access)]
