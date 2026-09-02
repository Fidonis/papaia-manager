"""Unit tests for the authorization policy and the role dependencies."""
from __future__ import annotations

import asyncio
import time
from typing import Any

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.auth import deps
from app.auth.deps import get_current_user, require_admin, require_manager_access
from app.auth.oidc import OIDCClaims, OIDCError, TokenSet
from app.auth.roles import has_manager_access, is_admin, is_user
from app.config import Settings


@pytest.fixture(autouse=True)
def _isolate_refresh_state() -> None:
    """The silent-refresh lock and result maps are module globals."""
    deps._refresh_locks.clear()
    deps._refresh_results.clear()


class _StubOIDCClient:
    """Stands in for the shared OIDCClient in refresh tests."""

    def __init__(
        self, *, result: TokenSet | None = None, error: Exception | None = None
    ) -> None:
        self._result = result
        self._error = error
        self.calls = 0

    async def refresh(self, *, refresh_token: str) -> TokenSet:
        self.calls += 1
        await asyncio.sleep(0)
        if self._error is not None:
            raise self._error
        assert self._result is not None
        return self._result


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


async def test_get_current_user_returns_session_claims() -> None:
    claims = _claims("user")
    resolved = await get_current_user(_request({"user": claims.to_dict()}))
    assert resolved.sub == claims.sub
    assert resolved.roles == ["user"]


async def test_get_current_user_without_session_raises_401() -> None:
    with pytest.raises(HTTPException) as exc:
        await get_current_user(_request())
    assert exc.value.status_code == 401


async def test_get_current_user_with_malformed_session_raises_401() -> None:
    with pytest.raises(HTTPException) as exc:
        await get_current_user(_request({"user": {"preferred_username": "no-sub"}}))
    assert exc.value.status_code == 401


async def test_get_current_user_with_expired_session_and_no_refresh_token_clears_it() -> None:
    session: dict[str, Any] = {"user": _claims("admin", expires_in=-60).to_dict()}
    request = _request(session)

    with pytest.raises(HTTPException) as exc:
        await get_current_user(request)

    assert exc.value.status_code == 401
    assert session == {}, "an unrecoverable expired session must be cleared"


async def test_get_current_user_refreshes_an_expiring_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _claims("admin", expires_in=3600)
    stub = _StubOIDCClient(result=TokenSet(claims=fresh, refresh_token="rotated"))
    monkeypatch.setattr(deps, "get_oidc_client", lambda: stub)

    session: dict[str, Any] = {
        "user": _claims("admin", expires_in=-10).to_dict(),
        "oidc": {"refresh_token": "original"},
    }
    resolved = await get_current_user(_request(session))

    assert stub.calls == 1
    assert resolved.exp == fresh.exp
    assert session["user"]["exp"] == fresh.exp
    assert session["oidc"]["refresh_token"] == "rotated"


async def test_get_current_user_raises_when_refresh_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stub = _StubOIDCClient(error=OIDCError("invalid_grant"))
    monkeypatch.setattr(deps, "get_oidc_client", lambda: stub)

    session: dict[str, Any] = {
        "user": _claims("admin", expires_in=-10).to_dict(),
        "oidc": {"refresh_token": "original"},
    }
    with pytest.raises(HTTPException) as exc:
        await get_current_user(_request(session))

    assert exc.value.status_code == 401
    assert session == {}


async def test_concurrent_pollers_trigger_one_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fresh = _claims("admin", expires_in=3600)
    stub = _StubOIDCClient(result=TokenSet(claims=fresh, refresh_token="rotated"))
    monkeypatch.setattr(deps, "get_oidc_client", lambda: stub)

    def _expired() -> dict[str, Any]:
        return {
            "user": _claims("admin", expires_in=-10).to_dict(),
            "oidc": {"refresh_token": "shared"},
        }

    resolved = await asyncio.gather(
        *(get_current_user(_request(_expired())) for _ in range(5))
    )

    assert stub.calls == 1
    assert all(r.exp == fresh.exp for r in resolved)
