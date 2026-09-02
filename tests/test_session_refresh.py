"""End-to-end: an expiring session is renewed through the real dependency stack.

The unit-level behaviour of the refresh helper lives in `test_roles.py`; this
module drives it through `SessionMiddleware`, the route dependency and the 401
exception handler, so a regression in the wiring shows up here.

Each test gets its own configuration directory through a dependency override,
following `test_api_tiles.py` -- the process environment is read once at import
time and shared with every other module that stands up the app.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from collections.abc import Iterator
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any

import httpx
import pytest

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-refresh-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-refresh-workspace-")

# `setdefault`, not `update`: another test module may have imported first and
# pointed the process at its own directories. The tests here work through the
# dependency override below, so whose values win does not matter.
for _key, _value in {
    "OIDC_ISSUER_KC_AUTH": "https://kc.test/auth",
    "OIDC_ISSUER_KC_TOKEN": "https://kc.test/token",
    "OIDC_ISSUER_KC_CERTS": "https://kc.test/certs",
    "MANAGER_ADMIN_ROLE": "admin",
    "MANAGER_USER_ROLE": "user",
    "MANAGER_HOST": "http://localhost:8120",
    "MANAGER_OIDC_CLIENT_SECRET": "client-secret",
    "MANAGER_SESSION_SECRET": "test-session-secret-value",
    "PAPAIA_CONFIG_DIR": _CONFIG_DIR,
    "PAPAIA_WORKSPACE_DIR": _WORKSPACE_DIR,
}.items():
    os.environ.setdefault(_key, _value)

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app.auth import deps  # noqa: E402
from app.auth.oidc import (  # noqa: E402
    OIDCClaims,
    OIDCClient,
    OIDCError,
    TokenSet,
    get_oidc_client,
)
from app.config import get_settings  # noqa: E402
from app.main import create_app  # noqa: E402

_CORE_ENV = "PAPAIA_HOST=https://papaia.test\n"


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    (directory / "manager").mkdir(parents=True)
    (directory / ".env").write_text(_CORE_ENV, encoding="utf-8")
    return directory


@pytest.fixture
def client(config_dir: Path) -> Iterator[TestClient]:
    get_settings.cache_clear()
    get_oidc_client.cache_clear()
    deps._refresh_locks.clear()
    deps._refresh_results.clear()

    settings = get_settings().model_copy(update={"papaia_config_dir": str(config_dir)})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    yield TestClient(app, follow_redirects=False)

    get_settings.cache_clear()
    get_oidc_client.cache_clear()
    deps._refresh_locks.clear()
    deps._refresh_results.clear()


def _secret() -> str:
    return get_settings().manager_session_secret


def _cookie(*, exp: int, refresh_token: str | None) -> str:
    session: dict[str, Any] = {
        "user": {
            "sub": "u-1",
            "preferred_username": "tester",
            "roles": ["admin"],
            "exp": exp,
        },
    }
    if refresh_token is not None:
        session["oidc"] = {"refresh_token": refresh_token}
    payload = base64.b64encode(json.dumps(session).encode())
    return TimestampSigner(_secret()).sign(payload).decode()


def _session_from_response(response: httpx.Response) -> dict[str, Any]:
    """Decode the session cookie this response set (the jar holds several)."""
    jar: SimpleCookie = SimpleCookie()
    for header in response.headers.get_list("set-cookie"):
        jar.load(header)
    raw = jar["papaia_manager_session"].value
    unsigned = TimestampSigner(_secret()).unsign(raw, max_age=28_800)
    decoded: dict[str, Any] = json.loads(base64.b64decode(unsigned))
    return decoded


def test_expiring_session_is_refreshed_in_place(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fresh = OIDCClaims(
        sub="u-1",
        preferred_username="tester",
        roles=["admin"],
        exp=int(time.time()) + 3600,
    )

    async def _fake_refresh(self: OIDCClient, *, refresh_token: str) -> TokenSet:
        assert refresh_token == "original"
        return TokenSet(claims=fresh, refresh_token="rotated")

    monkeypatch.setattr(OIDCClient, "refresh", _fake_refresh)

    client.cookies.set(
        "papaia_manager_session",
        _cookie(exp=int(time.time()) - 5, refresh_token="original"),
    )
    response = client.get("/")

    assert response.status_code == 200
    session = _session_from_response(response)
    assert session["user"]["exp"] == fresh.exp
    assert session["oidc"]["refresh_token"] == "rotated"


def test_dead_session_redirects_navigation_and_401s_xhr(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _reject(self: OIDCClient, *, refresh_token: str) -> TokenSet:
        raise OIDCError("invalid_grant")

    monkeypatch.setattr(OIDCClient, "refresh", _reject)
    dead = _cookie(exp=int(time.time()) - 5, refresh_token="stale")

    client.cookies.set("papaia_manager_session", dead)
    nav = client.get("/")
    assert nav.status_code == 307
    assert nav.headers["location"].startswith("/auth/login?next=")

    client.cookies.set("papaia_manager_session", dead)
    xhr = client.get("/", headers={"HX-Request": "true"})
    assert xhr.status_code == 401
