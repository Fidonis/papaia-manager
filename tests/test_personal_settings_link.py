"""The way to the personal settings area: an entry in the account menu and a dashboard tile.

The area is a service of its own; the manager only links to it. Its address is the optional
setting `MANAGER_PERSONAL_SETTINGS_URL`. Without it nothing is shown.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from collections.abc import Iterator
from typing import Any

import pytest

_SESSION_SECRET = "test-session-secret-value"

for _key, _value in {
    "OIDC_ISSUER_KC_AUTH": "https://kc.test/auth",
    "OIDC_ISSUER_KC_TOKEN": "https://kc.test/token",
    "OIDC_ISSUER_KC_CERTS": "https://kc.test/certs",
    "MANAGER_ADMIN_ROLE": "admin",
    "MANAGER_USER_ROLE": "user",
    "MANAGER_HOST": "http://localhost:8120",
    "MANAGER_OIDC_CLIENT_SECRET": "client-secret",
    "MANAGER_SESSION_SECRET": _SESSION_SECRET,
    "PAPAIA_CONFIG_DIR": tempfile.mkdtemp(prefix="papaia-me-link-config-"),
    "PAPAIA_WORKSPACE_DIR": tempfile.mkdtemp(prefix="papaia-me-link-workspace-"),
}.items():
    os.environ.setdefault(_key, _value)

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402
from app.core.personal_area import with_personal_tile  # noqa: E402
from app.core.tiles import Tile, TileGroup  # noqa: E402
from app.main import create_app  # noqa: E402

_AREA = "https://me.example.test/"


def _app(url: str | None) -> FastAPI:
    get_settings.cache_clear()
    settings = get_settings().model_copy(
        update={
            "papaia_config_dir": tempfile.mkdtemp(prefix="papaia-me-link-config-"),
            "manager_personal_settings_url": url,
        }
    )
    application = create_app()
    application.dependency_overrides[get_settings] = lambda: settings
    return application


@pytest.fixture(autouse=True)
def _settings_cache() -> Iterator[None]:
    yield
    get_settings.cache_clear()


def _as(app: FastAPI, role: str) -> TestClient:
    client = TestClient(app, follow_redirects=False)
    session: dict[str, Any] = {
        "user": {
            "sub": "u-1", "preferred_username": "tester", "roles": [role],
            "exp": int(time.time()) + 3600,
        },
        "_csrf_token": "csrf",
    }
    payload = base64.b64encode(json.dumps(session).encode())
    client.cookies.set(
        "papaia_manager_session", TimestampSigner(_SESSION_SECRET).sign(payload).decode()
    )
    return client


@pytest.mark.parametrize("role", ["user", "admin"])
def test_the_account_menu_leads_every_role_to_the_area(role: str) -> None:
    html = _as(_app(_AREA), role).get("/").text

    assert f'href="{_AREA}"' in html
    assert "My settings" in html


def test_the_entry_is_german_for_a_german_reader() -> None:
    html = _as(_app(_AREA), "user").get("/", headers={"Accept-Language": "de"}).text

    assert "Meine Einstellungen" in html


@pytest.mark.parametrize("role", ["user", "admin"])
def test_the_dashboard_has_a_tile_for_the_area(role: str) -> None:
    html = _as(_app(_AREA), role).get(
        "/partials/tiles", headers={"Accept-Language": "de"}
    ).text

    assert f'href="{_AREA}"' in html
    assert "Meine Einstellungen" in html


def test_without_the_setting_nothing_is_shown() -> None:
    app = _app(None)

    page = _as(app, "user").get("/").text
    tiles = _as(app, "user").get("/partials/tiles").text

    assert "My settings" not in page
    assert "My settings" not in tiles


@pytest.mark.parametrize("value", ["", "   "])
def test_an_empty_setting_counts_as_absent(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANAGER_PERSONAL_SETTINGS_URL", value)

    assert Settings().manager_personal_settings_url is None  # type: ignore[call-arg]


@pytest.mark.parametrize(
    "value",
    ["javascript:alert(1)", "me.example.test", "ftp://me.example.test/", "https://u:p@me.test/"],
)
def test_only_a_web_address_is_accepted(value: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MANAGER_PERSONAL_SETTINGS_URL", value)

    with pytest.raises(ValidationError):
        Settings()  # type: ignore[call-arg]


def test_the_tile_comes_first_and_is_not_shown_twice() -> None:
    groups = [TileGroup(name="Apps", tiles=[Tile(name="Chat", href="https://chat.test/")])]

    shown = with_personal_tile(
        groups, _AREA, name="My settings", description="d", group_name="Personal"
    )

    assert [g.name for g in shown] == ["Personal", "Apps"]
    (tile,) = shown[0].tiles
    assert isinstance(tile, Tile)
    assert (tile.name, tile.href, tile.visibility) == ("My settings", _AREA, "all")
    assert groups == [TileGroup(name="Apps", tiles=[Tile(name="Chat", href="https://chat.test/")])]

    own = [TileGroup(name="Apps", tiles=[Tile(name="Mine", href=_AREA.rstrip("/"))])]
    assert with_personal_tile(own, _AREA, name="x", description="", group_name="y") == own
