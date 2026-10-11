"""The signed-in user's own access token: obtained from the session, held in memory only."""

from __future__ import annotations

import asyncio
import os
import tempfile
import time
from collections.abc import Iterator
from typing import Any, cast

import pytest

for _key, _value in {
    "OIDC_ISSUER_KC_AUTH": "https://kc.test/auth",
    "OIDC_ISSUER_KC_TOKEN": "https://kc.test/token",
    "OIDC_ISSUER_KC_CERTS": "https://kc.test/certs",
    "MANAGER_ADMIN_ROLE": "admin",
    "MANAGER_USER_ROLE": "user",
    "MANAGER_HOST": "http://localhost:8120",
    "MANAGER_OIDC_CLIENT_SECRET": "client-secret",
    "MANAGER_SESSION_SECRET": "test-session-secret-value",
    "PAPAIA_CONFIG_DIR": tempfile.mkdtemp(prefix="papaia-usertoken-config-"),
    "PAPAIA_WORKSPACE_DIR": tempfile.mkdtemp(prefix="papaia-usertoken-workspace-"),
}.items():
    os.environ.setdefault(_key, _value)

from app.auth import user_token  # noqa: E402
from app.auth.oidc import OIDCClaims, OIDCClient, OIDCError, TokenSet, get_oidc_client  # noqa: E402
from app.config import get_settings  # noqa: E402


class _Request:
    """The one thing `user_access_token` reads of a request: its session."""

    def __init__(self, session: dict[str, Any]) -> None:
        self.session = session


def _session(refresh_token: str | None = "refresh-1") -> dict[str, Any]:
    session: dict[str, Any] = {
        "user": {"sub": "u-1", "preferred_username": "admin", "roles": [], "exp": 1}
    }
    if refresh_token is not None:
        session["oidc"] = {"refresh_token": refresh_token}
    return session


def _claims(exp: int = 4_000_000_000) -> OIDCClaims:
    return OIDCClaims(sub="u-1", preferred_username="admin", roles=["papaia-admin"], exp=exp)


class _Keycloak:
    """Stands in for the token endpoint: counts refreshes and rotates the refresh token."""

    def __init__(self) -> None:
        self.refreshes: list[str] = []
        self.rotate = True
        self.access_token: str | None = "access-1"
        self.expires_at = 0.0
        self.error: OIDCError | None = None
        self.delay = 0.0

    async def refresh(self, *, refresh_token: str) -> TokenSet:
        self.refreshes.append(refresh_token)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return TokenSet(
            claims=_claims(),
            refresh_token=f"refresh-{len(self.refreshes) + 1}" if self.rotate else refresh_token,
            access_token=self.access_token,
            access_expires_at=self.expires_at,
        )


@pytest.fixture
def keycloak(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Keycloak]:
    get_settings.cache_clear()
    get_oidc_client.cache_clear()
    user_token.clear()
    fake = _Keycloak()
    monkeypatch.setattr(OIDCClient, "refresh", fake.refresh)
    yield fake
    user_token.clear()
    get_oidc_client.cache_clear()
    get_settings.cache_clear()


async def _token(session: dict[str, Any]) -> str:
    return await user_token.user_access_token(cast("Any", _Request(session)))


async def test_the_token_comes_from_the_refresh_token(keycloak: _Keycloak) -> None:
    keycloak.expires_at = time.time() + 300
    session = _session()
    assert await _token(session) == "access-1"
    assert keycloak.refreshes == ["refresh-1"]


async def test_the_session_keeps_the_rotated_refresh_token_and_never_the_access_token(
    keycloak: _Keycloak,
) -> None:
    keycloak.expires_at = time.time() + 300
    session = _session()
    await _token(session)
    assert session["oidc"] == {"refresh_token": "refresh-2"}
    assert session["user"]["roles"] == ["papaia-admin"]
    assert "access-1" not in repr(session), "the cookie must not carry the access token"


async def test_a_second_call_is_served_from_memory(keycloak: _Keycloak) -> None:
    keycloak.expires_at = time.time() + 300
    session = _session()
    await _token(session)
    assert await _token(session) == "access-1"
    assert len(keycloak.refreshes) == 1


async def test_a_second_tab_with_the_old_cookie_is_served_from_the_same_entry(
    keycloak: _Keycloak,
) -> None:
    keycloak.expires_at = time.time() + 300
    await _token(_session("refresh-1"))
    # The first request rotated the token; the next one carries the new cookie. So does a tab
    # that still has the old one.
    assert await _token(_session("refresh-2")) == "access-1"
    assert await _token(_session("refresh-1")) == "access-1"
    assert len(keycloak.refreshes) == 1


async def test_a_token_about_to_expire_is_replaced(keycloak: _Keycloak) -> None:
    keycloak.expires_at = time.time() + 5  # inside the skew
    session = _session()
    await _token(session)
    keycloak.access_token = "access-2"
    keycloak.expires_at = time.time() + 300
    assert await _token(session) == "access-2"
    assert len(keycloak.refreshes) == 2


async def test_a_response_without_expiry_is_trusted_for_a_minute_only(keycloak: _Keycloak) -> None:
    keycloak.expires_at = 0.0
    await _token(_session())
    (expires_at, _), *_rest = user_token._cache.values()
    assert time.time() + 30 < expires_at <= time.time() + 61


async def test_concurrent_calls_ask_keycloak_once(keycloak: _Keycloak) -> None:
    keycloak.expires_at = time.time() + 300
    keycloak.delay = 0.05
    session = _session()
    tokens = await asyncio.gather(*(_token(session) for _ in range(8)))
    assert set(tokens) == {"access-1"}
    assert len(keycloak.refreshes) == 1


async def test_a_session_without_a_refresh_token_has_no_token(keycloak: _Keycloak) -> None:
    with pytest.raises(user_token.UserTokenUnavailable):
        await _token(_session(refresh_token=None))
    assert keycloak.refreshes == []


async def test_a_refresh_token_keycloak_refuses_ends_the_session(keycloak: _Keycloak) -> None:
    keycloak.error = OIDCError("token request failed with HTTP 400")
    with pytest.raises(user_token.UserTokenUnavailable):
        await _token(_session())


async def test_an_answer_without_an_access_token_is_not_a_token(keycloak: _Keycloak) -> None:
    keycloak.access_token = None
    with pytest.raises(user_token.UserTokenUnavailable):
        await _token(_session())


async def test_the_token_is_not_part_of_the_token_sets_repr() -> None:
    token_set = TokenSet(claims=_claims(), refresh_token="r", access_token="secret-access")
    assert "secret-access" not in repr(token_set)
