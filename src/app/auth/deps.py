"""FastAPI dependencies for session-based authentication."""
from __future__ import annotations

from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from app.auth.oidc import OIDCClaims
from app.auth.roles import has_manager_access, is_admin
from app.config import Settings, get_settings


def get_current_user(request: Request) -> OIDCClaims:
    """Return the authenticated user from the session or raise 401."""
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
    if claims.is_expired:
        request.session.clear()
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="session expired",
            headers={"Location": "/auth/login"},
        )
    return claims


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
