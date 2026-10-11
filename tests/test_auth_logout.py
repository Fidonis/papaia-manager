"""Signing out: the form post, the end of the Keycloak session and the manager's own.

Fixtures follow test_api_audit.py: a `client` with `get_settings` overridden, and a signed
session cookie set by hand. The provider is a stand-in that records what it was asked.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import tempfile
import time
from collections.abc import Iterator
from typing import Any

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
    "PAPAIA_CONFIG_DIR": tempfile.mkdtemp(prefix="papaia-logout-config-"),
    "PAPAIA_WORKSPACE_DIR": tempfile.mkdtemp(prefix="papaia-logout-workspace-"),
}.items():
    os.environ.setdefault(_key, _value)

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app.auth import user_token  # noqa: E402
from app.auth.oidc import OIDCError  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.main import create_app  # noqa: E402

_CSRF = "test-csrf-token-value"
_REFRESH = "refresh-token-secret-1"
_PROTECTED = "/api/v1/audit"


class _Provider:
    """Stands in for the OIDC client: records `end_session` and can be told to fail."""

    def __init__(self) -> None:
        self.ended: list[str] = []
        self.error: OIDCError | None = None
        self.has_endpoint = True

    async def end_session(self, *, refresh_token: str) -> bool:
        self.ended.append(refresh_token)
        if self.error is not None:
            raise self.error
        return self.has_endpoint


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> _Provider:
    stand_in = _Provider()
    monkeypatch.setattr("app.routers.auth.get_oidc_client", lambda: stand_in)
    return stand_in


@pytest.fixture
def client() -> Iterator[TestClient]:
    get_settings.cache_clear()
    user_token.clear()
    yield TestClient(create_app(), follow_redirects=False)
    user_token.clear()
    get_settings.cache_clear()


def _session(**overrides: Any) -> dict[str, Any]:
    session: dict[str, Any] = {
        "user": {
            "sub": "u-1",
            "preferred_username": "tester",
            "roles": ["admin"],
            "exp": int(time.time()) + 3600,
        },
        "oidc": {"refresh_token": _REFRESH},
        "_csrf_token": _CSRF,
    }
    session.update(overrides)
    return session


def _sign_in(client: TestClient, session: dict[str, Any] | None = None) -> TestClient:
    payload = base64.b64encode(json.dumps(session or _session()).encode())
    signed = TimestampSigner(get_settings().manager_session_secret).sign(payload).decode()
    client.cookies.clear()
    # The domain the app's answers are filed under (`cookiejar` appends ".local" to a host
    # without a dot), so that the cookie it expires on sign-out is this one.
    client.cookies.set("papaia_manager_session", signed, domain="testserver.local", path="/")
    return client


def _signed_in(client: TestClient) -> bool:
    return client.get(_PROTECTED).status_code == 200


# -- the form the sidebar posts ------------------------------------------------------


def test_the_sign_out_form_ends_both_sessions_and_shows_the_login_page(
    client: TestClient, provider: _Provider
) -> None:
    _sign_in(client)
    assert _signed_in(client)

    response = client.post("/auth/logout", data={"csrf_token": _CSRF})

    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"
    assert provider.ended == [_REFRESH]
    assert client.get(_PROTECTED).status_code == 401


def test_a_script_may_send_the_token_as_a_header_instead(
    client: TestClient, provider: _Provider
) -> None:
    _sign_in(client)

    response = client.post("/auth/logout", headers={"X-CSRF-Token": _CSRF})

    assert response.status_code == 303
    assert provider.ended == [_REFRESH]
    assert client.get(_PROTECTED).status_code == 401


@pytest.mark.parametrize(
    ("data", "headers"),
    [
        ({}, {}),
        ({"csrf_token": "wrong"}, {}),
        ({"csrf_token": ""}, {}),
        ({"csrf_token": "tökén-with-umlauts-é"}, {}),
        ({}, {"X-CSRF-Token": "wrong"}),
    ],
)
def test_a_post_without_the_right_token_changes_nothing(
    client: TestClient, provider: _Provider, data: dict[str, str], headers: dict[str, str]
) -> None:
    _sign_in(client)

    response = client.post("/auth/logout", data=data, headers=headers)

    assert response.status_code == 403
    assert provider.ended == []
    assert _signed_in(client)


def test_there_is_no_get_variant_that_ends_the_session(
    client: TestClient, provider: _Provider
) -> None:
    _sign_in(client)

    response = client.get("/auth/logout")

    assert response.status_code == 405
    assert provider.ended == []
    assert _signed_in(client)


# -- what the sign-out leaves behind -------------------------------------------------


def test_the_cached_access_token_of_the_session_is_dropped(
    client: TestClient, provider: _Provider
) -> None:
    _sign_in(client)
    expires_at = time.time() + 300
    user_token._cache[user_token._key(_REFRESH)] = (expires_at, "access-1")

    client.post("/auth/logout", data={"csrf_token": _CSRF})

    assert user_token._cache == {}


def test_a_keycloak_that_refuses_does_not_keep_the_user_signed_in(
    client: TestClient, provider: _Provider, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    provider.error = OIDCError("logout request failed with HTTP 500")
    _sign_in(client)

    response = client.post("/auth/logout", data={"csrf_token": _CSRF})

    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"
    assert client.get(_PROTECTED).status_code == 401
    assert "could not end the Keycloak session" in caplog.text
    assert _REFRESH not in caplog.text
    assert _CSRF not in caplog.text


def test_a_provider_without_a_logout_endpoint_still_ends_the_local_session(
    client: TestClient, provider: _Provider
) -> None:
    provider.has_endpoint = False
    _sign_in(client)

    response = client.post("/auth/logout", data={"csrf_token": _CSRF})

    assert response.status_code == 303
    assert client.get(_PROTECTED).status_code == 401


def test_a_session_without_a_refresh_token_is_ended_locally(
    client: TestClient, provider: _Provider
) -> None:
    session = _session()
    del session["oidc"]
    _sign_in(client, session)

    response = client.post("/auth/logout", data={"csrf_token": _CSRF})

    assert response.status_code == 303
    assert provider.ended == []
    assert client.get(_PROTECTED).status_code == 401


# -- a stale tab ---------------------------------------------------------------------


def test_a_stale_tab_without_a_session_goes_to_the_login_page(
    client: TestClient, provider: _Provider
) -> None:
    response = client.post("/auth/logout", data={"csrf_token": _CSRF})

    assert response.status_code == 303
    assert response.headers["location"] == "/auth/login"
    assert provider.ended == []
