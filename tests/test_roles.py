"""Unit tests for the authorization policy and the role dependencies."""
from __future__ import annotations

import time
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.auth.deps import get_current_user, require_admin, require_manager_access
from app.auth.oidc import OIDCClaims
from app.auth.roles import has_manager_access, is_admin, is_user
from app.config import Settings


def _settings(**overrides: str) -> Settings:
    """Build a Settings instance without touching the ambient environment."""
    values: dict[str, str] = {
        "oidc_issuer_kc_auth": "https://kc.example/auth",
        "oidc_issuer_kc_token": "https://kc.example/token",
        "oidc_issuer_kc_certs": "https://kc.example/certs",
        "manager_host": "https://manager.example",
        "manager_oidc_client_secret": "client-secret",
        "manager_session_secret": "session-secret",
        "papaia_config_dir": "/srv/papaia/config",
        "papaia_workspace_dir": "/srv/papaia/workspace",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _claims(*roles: str, expires_in: int = 3600) -> OIDCClaims:
    return OIDCClaims(
        sub="u-1",
        preferred_username="tester",
        roles=list(roles),
        exp=int(time.time()) + expires_in,
    )


def _request(session: dict[str, Any] | None = None) -> Request:
    request = Request({"type": "http", "method": "GET", "path": "/", "headers": []})
    request.scope["session"] = {} if session is None else session
    return request


# ---------------------------------------------------------------------------
# Policy predicates
# ---------------------------------------------------------------------------


def test_is_admin_recognises_the_configured_role() -> None:
    settings = _settings()
    assert is_admin(_claims("admin"), settings) is True
    assert is_admin(_claims("user"), settings) is False
    assert is_admin(_claims(), settings) is False


def test_is_user_recognises_the_configured_role() -> None:
    settings = _settings()
    assert is_user(_claims("user"), settings) is True
    assert is_user(_claims("admin"), settings) is False


@pytest.mark.parametrize(
    ("roles", "expected"),
    [
        (("admin",), True),
        (("user",), True),
        (("admin", "user"), True),
        ((), False),
        (("offline_access",), False),
    ],
)
def test_has_manager_access(roles: tuple[str, ...], expected: bool) -> None:
    assert has_manager_access(_claims(*roles), _settings()) is expected


def test_role_names_are_configurable() -> None:
    """Both role names come from settings, not from hardcoded literals."""
    settings = _settings(manager_admin_role="papaia-ops", manager_user_role="papaia-staff")

    assert is_admin(_claims("papaia-ops"), settings) is True
    assert is_user(_claims("papaia-staff"), settings) is True
    # The stock names carry no meaning once they have been overridden.
    assert has_manager_access(_claims("admin"), settings) is False
    assert has_manager_access(_claims("user"), settings) is False


# ---------------------------------------------------------------------------
# Route dependencies
# ---------------------------------------------------------------------------


def test_require_admin_accepts_admin() -> None:
    claims = _claims("admin")
    assert require_admin(claims, _settings()) is claims


@pytest.mark.parametrize("roles", [("user",), (), ("something-else",)])
def test_require_admin_rejects_non_admin(roles: tuple[str, ...]) -> None:
    with pytest.raises(HTTPException) as exc:
        require_admin(_claims(*roles), _settings())
    assert exc.value.status_code == 403


@pytest.mark.parametrize("roles", [("admin",), ("user",), ("admin", "user")])
def test_require_manager_access_accepts_either_role(roles: tuple[str, ...]) -> None:
    claims = _claims(*roles)
    assert require_manager_access(claims, _settings()) is claims


@pytest.mark.parametrize("roles", [(), ("offline_access",)])
def test_require_manager_access_rejects_unknown_roles(roles: tuple[str, ...]) -> None:
    with pytest.raises(HTTPException) as exc:
        require_manager_access(_claims(*roles), _settings())
    assert exc.value.status_code == 403


# ---------------------------------------------------------------------------
# Session extraction
# ---------------------------------------------------------------------------


def test_get_current_user_returns_session_claims() -> None:
    claims = _claims("user")
    resolved = get_current_user(_request({"user": claims.to_dict()}))
    assert resolved.sub == claims.sub
    assert resolved.roles == ["user"]


def test_get_current_user_without_session_raises_401() -> None:
    with pytest.raises(HTTPException) as exc:
        get_current_user(_request())
    assert exc.value.status_code == 401


def test_get_current_user_with_malformed_session_raises_401() -> None:
    with pytest.raises(HTTPException) as exc:
        get_current_user(_request({"user": {"preferred_username": "no-sub"}}))
    assert exc.value.status_code == 401


def test_get_current_user_with_expired_session_raises_401_and_clears_it() -> None:
    session = {"user": _claims("admin", expires_in=-60).to_dict()}
    request = _request(session)

    with pytest.raises(HTTPException) as exc:
        get_current_user(request)

    assert exc.value.status_code == 401
    assert session == {}, "an expired session must be cleared, not left in place"
