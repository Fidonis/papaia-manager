"""The widget routes and the tiles API with widgets in it, end to end through the real app.

What this module adds to the unit tests is the boundary: who is allowed to fetch which card,
what a failed card says to whom, that nothing a widget (or a tile's title) supplies reaches
the page unescaped, and that a dashboard with widgets in it can be written and read back.

Each test gets a configuration directory of its own, injected by overriding the settings
dependency, and the widget types it needs are registered for that test only.
"""

from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-widgets-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-widgets-workspace-")

# `setdefault`, not `update`: another test module may have imported first. Every test here
# works through the dependency override below, so whose values win does not matter.
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

from app.config import Settings, get_settings  # noqa: E402
from app.core.tiles import Tile, TileGroup, TilesConfig, WidgetTile, save_tiles  # noqa: E402
from app.core.widgets import registry, runtime  # noqa: E402
from app.core.widgets.registry import (  # noqa: E402
    WidgetContext,
    WidgetType,
    WidgetUnavailableError,
)
from app.main import create_app  # noqa: E402

_CSRF = "test-csrf-token-value"


class Probe:
    """The provider behind the widget types these tests place."""

    def __init__(self) -> None:
        self.calls = 0
        self.label = "Last backup"
        self.fails: str | None = None

    async def __call__(self, context: WidgetContext) -> dict[str, Any]:
        self.calls += 1
        if self.fails is not None:
            raise WidgetUnavailableError(self.fails)
        return {
            "widget_api": 1,
            "view": "stat",
            "state": "warn",
            "data": {"value": "42", "unit": "days", "label": self.label},
            "link": {"label": "Open backups", "href": "/backup"},
        }


@pytest.fixture
def probe(monkeypatch: pytest.MonkeyPatch) -> Probe:
    provider = Probe()
    types = dict(registry._TYPES)  # noqa: SLF001
    types["probe"] = WidgetType(
        id="probe",
        name="Probe widget",
        description="Only for these tests.",
        icon="tag",
        view="stat",
        provider=provider,
    )
    types["needs-more"] = WidgetType(
        id="needs-more",
        name="Needs more",
        description="Cannot be used here.",
        icon="alert",
        view="stat",
        provider=provider,
        requires=lambda settings: "This installation lacks the thing it needs.",
    )
    monkeypatch.setattr(registry, "_TYPES", types)
    return provider


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    runtime.reset_cache()
    yield
    runtime.reset_cache()


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    (directory / "manager").mkdir(parents=True)
    (directory / ".env").write_text("PAPAIA_HOST=https://papaia.test\n", encoding="utf-8")
    return directory


@pytest.fixture
def client(config_dir: Path) -> Iterator[TestClient]:
    get_settings.cache_clear()
    settings: Settings = get_settings().model_copy(update={"papaia_config_dir": str(config_dir)})

    # No `with` block: skipping the lifespan keeps the job-queue worker thread and the
    # workspace handshake out of these tests.
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    yield TestClient(app, follow_redirects=False)
    get_settings.cache_clear()


def _as(client: TestClient, *roles: str) -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": "u-1",
            "preferred_username": "tester",
            "roles": list(roles),
            "exp": int(time.time()) + 3600,
        },
        "_csrf_token": _CSRF,
    }
    payload = base64.b64encode(json.dumps(session).encode())
    signed = TimestampSigner(get_settings().manager_session_secret).sign(payload).decode()
    client.cookies.clear()
    client.cookies.set("papaia_manager_session", signed)
    return client


def _admin(client: TestClient) -> TestClient:
    return _as(client, "admin")


def _user(client: TestClient) -> TestClient:
    return _as(client, "user")


def _headers() -> dict[str, str]:
    return {"X-CSRF-Token": _CSRF}


def _place(config_dir: Path, *tiles: Tile | WidgetTile) -> None:
    save_tiles(str(config_dir), TilesConfig(groups=[TileGroup(name="Status", tiles=list(tiles))]))


# ---------------------------------------------------------------------------
# Who may fetch which card
# ---------------------------------------------------------------------------


def test_an_administrator_gets_the_card_of_a_widget_for_administrators(
    client: TestClient, config_dir: Path, probe: Probe
) -> None:
    _place(config_dir, WidgetTile(widget="probe", id="p1", title="Backups", visibility="admin"))

    response = _admin(client).get("/partials/widgets/p1")

    assert response.status_code == 200
    assert "Backups" in response.text and "42" in response.text and "days" in response.text
    assert response.headers["cache-control"] == "no-store"


def test_a_user_does_not_get_a_widget_for_administrators(
    client: TestClient, config_dir: Path, probe: Probe
) -> None:
    _place(config_dir, WidgetTile(widget="probe", id="p1", visibility="admin"))

    response = _user(client).get("/partials/widgets/p1")

    assert response.status_code == 404
    assert probe.calls == 0


def test_a_widget_that_is_not_visible_answers_like_one_that_does_not_exist(
    client: TestClient, config_dir: Path, probe: Probe
) -> None:
    """The route must not tell a plain user which ids exist."""
    _place(config_dir, WidgetTile(widget="probe", id="p1", visibility="admin"))

    hidden = _user(client).get("/partials/widgets/p1")
    missing = _user(client).get("/partials/widgets/nope")

    assert (hidden.status_code, hidden.json()) == (missing.status_code, missing.json())


def test_a_user_gets_a_widget_set_for_everyone(
    client: TestClient, config_dir: Path, probe: Probe
) -> None:
    _place(config_dir, WidgetTile(widget="probe", id="p2", title="Shared", visibility="all"))

    response = _user(client).get("/partials/widgets/p2")

    assert response.status_code == 200 and "Shared" in response.text


def test_a_request_without_a_session_gets_a_401_to_act_on(
    client: TestClient, config_dir: Path, probe: Probe
) -> None:
    _place(config_dir, WidgetTile(widget="probe", id="p2", visibility="all"))

    response = client.get("/partials/widgets/p2", headers={"HX-Request": "true"})

    assert response.status_code == 401


def test_an_id_that_is_not_path_safe_is_refused(
    client: TestClient, config_dir: Path, probe: Probe
) -> None:
    assert _admin(client).get("/partials/widgets/a%20b").status_code == 422
    assert _admin(client).get("/partials/widgets/" + "x" * 41).status_code == 422


def test_a_broken_tiles_file_is_a_404_not_a_500(
    client: TestClient, config_dir: Path, probe: Probe
) -> None:
    (config_dir / "manager" / "tiles.yaml").write_text("groups: [unterminated", encoding="utf-8")

    assert _admin(client).get("/partials/widgets/p1").status_code == 404


# ---------------------------------------------------------------------------
# A card that cannot show its data
# ---------------------------------------------------------------------------


def test_an_administrator_is_told_why_a_card_has_nothing_to_show(
    client: TestClient, config_dir: Path, probe: Probe
) -> None:
    probe.fails = "The backup directory is not mounted."
    _place(config_dir, WidgetTile(widget="probe", id="p1", visibility="all"))

    response = _admin(client).get("/partials/widgets/p1")

    assert response.status_code == 200
    assert "Not available" in response.text
    assert "The backup directory is not mounted." in response.text


def test_everyone_else_is_not_told_why(client: TestClient, config_dir: Path, probe: Probe) -> None:
    probe.fails = "The backup directory /srv/papaia/backup is not mounted."
    _place(config_dir, WidgetTile(widget="probe", id="p1", visibility="all"))

    response = _user(client).get("/partials/widgets/p1")

    assert response.status_code == 200
    assert "Not available" in response.text
    assert "/srv/papaia" not in response.text and "not mounted" not in response.text
    assert "cannot show its data right now" in response.text


def test_a_widget_that_was_removed_from_the_manager_is_a_card_that_says_so(
    client: TestClient, config_dir: Path, probe: Probe
) -> None:
    _place(config_dir, WidgetTile(widget="no-such", id="g1", visibility="all"))

    admin = _admin(client).get("/partials/widgets/g1")
    user = _user(client).get("/partials/widgets/g1")

    assert admin.status_code == user.status_code == 200
    assert "No widget called" in admin.text
    assert "No widget called" not in user.text and "cannot show its data right now" in user.text


def test_a_widget_whose_requirement_is_not_met_is_not_read(
    client: TestClient, config_dir: Path, probe: Probe
) -> None:
    _place(config_dir, WidgetTile(widget="needs-more", id="n1", visibility="all"))

    response = _admin(client).get("/partials/widgets/n1")

    assert "lacks the thing it needs" in response.text
    assert probe.calls == 0


def test_a_stale_card_says_when_its_figures_are_from(
    client: TestClient, config_dir: Path, probe: Probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr(runtime, "_clock", lambda: clock["now"])
    _place(config_dir, WidgetTile(widget="probe", id="p1", visibility="all"))
    assert "42" in _admin(client).get("/partials/widgets/p1").text

    probe.fails = "down"
    clock["now"] += 61
    response = _admin(client).get("/partials/widgets/p1")

    assert "As of" in response.text and "42" in response.text and "down" in response.text


def test_a_user_sees_a_stale_card_without_the_reason(
    client: TestClient, config_dir: Path, probe: Probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    clock = {"now": 1000.0}
    monkeypatch.setattr(runtime, "_clock", lambda: clock["now"])
    _place(config_dir, WidgetTile(widget="probe", id="p1", visibility="all"))
    _user(client).get("/partials/widgets/p1")

    probe.fails = "disk /srv/papaia is gone"
    clock["now"] += 61
    response = _user(client).get("/partials/widgets/p1")

    assert "As of" in response.text and "/srv/papaia" not in response.text


# ---------------------------------------------------------------------------
# Nothing a widget or a tile supplies reaches the page unescaped
# ---------------------------------------------------------------------------


def test_text_with_special_characters_is_escaped(
    client: TestClient, config_dir: Path, probe: Probe
) -> None:
    probe.label = 'Tom & "Jerry" > Spike'
    _place(config_dir, WidgetTile(widget="probe", id="p1", visibility="all"))

    text = _admin(client).get("/partials/widgets/p1").text

    assert "Tom &amp; &#34;Jerry&#34; &gt; Spike" in text


def test_markup_in_a_payload_is_not_drawn(
    client: TestClient, config_dir: Path, probe: Probe
) -> None:
    probe.label = "<script>alert(1)</script>"
    _place(config_dir, WidgetTile(widget="probe", id="p1", visibility="all"))

    admin = _admin(client).get("/partials/widgets/p1").text
    user = _user(client).get("/partials/widgets/p1").text

    for text in (admin, user):
        assert "<script" not in text and "alert(1)" not in text
        assert "Not available" in text


def test_a_title_with_markup_is_escaped_on_the_card_and_in_the_placeholder(
    client: TestClient, config_dir: Path, probe: Probe
) -> None:
    title = "<img src=x onerror=alert(1)>"
    _place(config_dir, WidgetTile(widget="probe", id="p1", title=title, visibility="all"))

    card = _admin(client).get("/partials/widgets/p1").text
    gallery = _admin(client).get("/partials/tiles").text

    for text in (card, gallery):
        assert "<img src=x" not in text
        assert "&lt;img src=x onerror=alert(1)&gt;" in text


def test_a_link_to_a_page_of_the_manager_is_shown_to_administrators_only(
    client: TestClient, config_dir: Path, probe: Probe
) -> None:
    _place(config_dir, WidgetTile(widget="probe", id="p1", visibility="all"))

    assert 'href="/backup"' in _admin(client).get("/partials/widgets/p1").text
    assert 'href="/backup"' not in _user(client).get("/partials/widgets/p1").text


def test_an_address_in_a_payload_is_a_link_for_everyone_and_opens_apart(
    client: TestClient, config_dir: Path, probe: Probe, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def provider(context: WidgetContext) -> dict[str, Any]:
        return {
            "widget_api": 1,
            "view": "stat",
            "state": "ok",
            "data": {"value": "1"},
            "link": {"label": "Open Grafana", "href": "https://grafana.test/d/1"},
        }

    types = dict(registry._TYPES)  # noqa: SLF001
    types["outside"] = WidgetType(
        id="outside", name="Outside", description="", icon="tag", view="stat", provider=provider
    )
    monkeypatch.setattr(registry, "_TYPES", types)
    _place(config_dir, WidgetTile(widget="outside", id="o1", visibility="all"))

    text = _user(client).get("/partials/widgets/o1").text

    assert 'href="https://grafana.test/d/1"' in text
    assert 'rel="noopener noreferrer"' in text


# ---------------------------------------------------------------------------
# The editor's preview
# ---------------------------------------------------------------------------


def test_the_preview_is_a_card_of_a_widget_that_is_not_placed_yet(
    client: TestClient, probe: Probe
) -> None:
    response = _admin(client).get("/partials/widgets/preview/probe?title=My%20title")

    assert response.status_code == 200
    assert "My title" in response.text and "42" in response.text


def test_the_preview_is_called_after_the_widget_when_no_title_is_given(
    client: TestClient, probe: Probe
) -> None:
    assert "Probe widget" in _admin(client).get("/partials/widgets/preview/probe").text


def test_the_preview_escapes_the_title(client: TestClient, probe: Probe) -> None:
    response = _admin(client).get("/partials/widgets/preview/probe?title=%3Cb%3Ex%3C/b%3E")

    assert "<b>x</b>" not in response.text and "&lt;b&gt;x&lt;/b&gt;" in response.text


def test_a_user_cannot_use_the_preview(client: TestClient, probe: Probe) -> None:
    assert _user(client).get("/partials/widgets/preview/probe").status_code == 403


def test_the_preview_of_a_type_that_does_not_exist_is_a_404(
    client: TestClient, probe: Probe
) -> None:
    assert _admin(client).get("/partials/widgets/preview/no-such").status_code == 404


def test_the_preview_of_a_type_the_installation_cannot_use_says_why(
    client: TestClient, probe: Probe
) -> None:
    response = _admin(client).get("/partials/widgets/preview/needs-more")

    assert "lacks the thing it needs" in response.text and probe.calls == 0


def test_the_title_of_a_preview_has_a_limit(client: TestClient, probe: Probe) -> None:
    response = _admin(client).get("/partials/widgets/preview/probe?title=" + "x" * 81)

    assert response.status_code == 422


# ---------------------------------------------------------------------------
# The dashboard and the editor
# ---------------------------------------------------------------------------


def _dashboard(config_dir: Path) -> None:
    _place(
        config_dir,
        WidgetTile(widget="probe", id="adm1", visibility="admin"),
        WidgetTile(widget="probe", id="all1", visibility="all", size="m"),
        WidgetTile(widget="probe", id="wide1", visibility="all", size="l"),
        Tile(name="LibreChat", href="https://chat.test"),
    )


def test_the_dashboard_hands_each_widget_to_the_browser_to_fetch(
    client: TestClient, config_dir: Path, probe: Probe
) -> None:
    _dashboard(config_dir)

    text = _admin(client).get("/partials/tiles").text

    for widget_id in ("adm1", "all1", "wide1"):
        assert f'hx-get="/partials/widgets/{widget_id}"' in text
    assert "every 60s" in text
    # Nothing was read to render the dashboard itself: each card is a request of its own.
    assert probe.calls == 0


def test_polling_pauses_while_the_tab_is_hidden(
    client: TestClient, config_dir: Path, probe: Probe
) -> None:
    _dashboard(config_dir)

    assert "document.visibilityState === 'visible'" in _admin(client).get("/partials/tiles").text


def test_a_user_is_not_handed_a_widget_for_administrators(
    client: TestClient, config_dir: Path, probe: Probe
) -> None:
    _dashboard(config_dir)

    text = _user(client).get("/partials/tiles").text

    assert "/partials/widgets/adm1" not in text
    assert "/partials/widgets/all1" in text and "/partials/widgets/wide1" in text


def test_a_widget_takes_the_width_of_its_size(
    client: TestClient, config_dir: Path, probe: Probe
) -> None:
    _dashboard(config_dir)

    text = _admin(client).get("/partials/tiles").text

    assert 'id="widget-all1"\n         class="sm:col-span-2"' in text
    assert "sm:col-span-2 xl:col-span-3 2xl:col-span-4" in text


def test_a_group_counts_its_applications_and_its_widgets_apart(
    client: TestClient, config_dir: Path, probe: Probe
) -> None:
    _dashboard(config_dir)

    text = " ".join(_admin(client).get("/partials/tiles").text.split())

    assert "1 application &nbsp;·&nbsp; 3 widgets" in text


def test_a_group_of_links_is_counted_as_before(
    client: TestClient, config_dir: Path, probe: Probe
) -> None:
    _place(config_dir, Tile(name="A", href="/a"), Tile(name="B", href="/b"))

    text = " ".join(_admin(client).get("/partials/tiles").text.split())

    assert "2 applications" in text and "widget" not in text


def test_the_editor_is_handed_the_widget_types_and_the_icons(
    client: TestClient, config_dir: Path, probe: Probe
) -> None:
    _dashboard(config_dir)

    text = _admin(client).get("/partials/tiles/edit").text

    for expected in ("Probe widget", "Needs more", "This installation lacks the thing it needs."):
        assert expected in text
    assert "backup-age" in text and "archive" in text


def test_the_editor_is_for_administrators_only(
    client: TestClient, config_dir: Path, probe: Probe
) -> None:
    assert _user(client).get("/partials/tiles/edit").status_code == 403


# ---------------------------------------------------------------------------
# Writing a dashboard with widgets in it
# ---------------------------------------------------------------------------


def _link(name: str, href: str) -> dict[str, Any]:
    return {"name": name, "href": href, "description": "", "icon": None, "visibility": "all"}


def _widget(widget: str = "backup-age", **fields: Any) -> dict[str, Any]:
    return {"type": "widget", "id": "", "widget": widget, "title": "", "size": "s", **fields}


def _put(client: TestClient, revision: str, *tiles: dict[str, Any]) -> Any:
    group = {"name": "Status", "tiles": list(tiles)}
    body = {"revision": revision, "version": 1, "groups": [group]}
    return _admin(client).put("/api/v1/tiles", json=body, headers=_headers())


def _revision(client: TestClient) -> str:
    return str(_admin(client).get("/api/v1/tiles").json()["revision"])


def test_a_widget_is_saved_and_given_an_id(client: TestClient, config_dir: Path) -> None:
    response = _put(
        client,
        _revision(client),
        _widget("backup-age", visibility="admin"),
        _link("LibreChat", "https://chat.test"),
    )

    assert response.status_code == 200
    saved = response.json()["groups"][0]["tiles"]
    assert saved[0]["type"] == "widget" and saved[0]["id"]
    assert "type" not in saved[1]
    written = yaml.safe_load((config_dir / "manager" / "tiles.yaml").read_text(encoding="utf-8"))
    assert written["groups"][0]["tiles"][0]["type"] == "widget"
    assert written["groups"][0]["tiles"][0]["id"] == saved[0]["id"]


def test_the_id_stays_when_the_dashboard_is_saved_again(client: TestClient) -> None:
    first = _put(client, _revision(client), _widget("backup-age")).json()
    widget_id = first["groups"][0]["tiles"][0]["id"]

    second = _put(client, first["revision"], _widget("backup-age", id=widget_id)).json()

    assert second["groups"][0]["tiles"][0]["id"] == widget_id


def test_two_new_widgets_of_one_type_get_two_ids(client: TestClient) -> None:
    saved = _put(client, _revision(client), _widget("disk-space"), _widget("disk-space")).json()[
        "groups"
    ][0]["tiles"]

    assert saved[0]["id"] != saved[1]["id"]


def test_a_widget_the_manager_does_not_have_is_refused_and_named(client: TestClient) -> None:
    response = _put(client, _revision(client), _link("A", "/a"), _widget("no-such"))

    assert response.status_code == 422
    problem = response.json()["detail"][0]
    assert (problem["group"], problem["tile"], problem["field"]) == (0, 1, "widget")
    assert "no-such" in problem["message"]


def test_a_title_that_is_too_long_is_refused(client: TestClient) -> None:
    response = _put(client, _revision(client), _widget("backup-age", title="t" * 81))

    assert response.status_code == 422 and response.json()["detail"][0]["field"] == "title"


def test_a_refused_dashboard_leaves_the_file_alone(client: TestClient, config_dir: Path) -> None:
    revision = _revision(client)
    before = (config_dir / "manager" / "tiles.yaml").read_text(encoding="utf-8")

    _put(client, revision, _widget("no-such"))

    assert (config_dir / "manager" / "tiles.yaml").read_text(encoding="utf-8") == before


def test_a_widget_without_a_visibility_is_saved_for_administrators(client: TestClient) -> None:
    body = _widget("backup-age")
    saved = _put(client, _revision(client), body).json()["groups"][0]["tiles"][0]

    assert saved["visibility"] == "admin"


def test_a_widget_comes_back_the_way_it_went(client: TestClient) -> None:
    _put(
        client,
        _revision(client),
        _widget("disk-space", id="d1", title="Disks", size="l", visibility="all"),
    )

    tile = _admin(client).get("/api/v1/tiles").json()["groups"][0]["tiles"][0]

    assert tile == {
        "type": "widget",
        "id": "d1",
        "widget": "disk-space",
        "title": "Disks",
        "size": "l",
        "visibility": "all",
    }


def test_a_dashboard_of_links_only_is_still_written_as_before(client: TestClient) -> None:
    response = _put(client, _revision(client), _link("A", "https://a.test"))

    assert response.status_code == 200
    assert response.json()["groups"][0]["tiles"] == [
        {
            "name": "A",
            "href": "https://a.test",
            "description": "",
            "icon": None,
            "visibility": "all",
        }
    ]


def test_the_raw_editor_takes_a_widget(client: TestClient, config_dir: Path) -> None:
    revision = str(_admin(client).get("/api/v1/tiles/raw").json()["revision"])
    document = yaml.safe_dump(
        {
            "version": 1,
            "groups": [
                {
                    "name": "Status",
                    "tiles": [{"type": "widget", "widget": "backup-age", "visibility": "admin"}],
                }
            ],
        }
    )

    response = _admin(client).put(
        "/api/v1/tiles/raw", json={"revision": revision, "yaml": document}, headers=_headers()
    )

    assert response.status_code == 200
    assert response.json()["groups"][0]["tiles"][0]["id"]


def test_the_raw_editor_refuses_a_widget_the_manager_does_not_have(client: TestClient) -> None:
    revision = str(_admin(client).get("/api/v1/tiles/raw").json()["revision"])
    document = yaml.safe_dump(
        {"version": 1, "groups": [{"name": "S", "tiles": [{"type": "widget", "widget": "nope"}]}]}
    )

    response = _admin(client).put(
        "/api/v1/tiles/raw", json={"revision": revision, "yaml": document}, headers=_headers()
    )

    assert response.status_code == 422
