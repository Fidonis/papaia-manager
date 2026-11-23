"""The scripts of the dashboard and the tile editor (T-UI-03, oberfl 3.3, P4).

dashboard.html, partials/tile_editor.html and partials/tile_gallery.html carry no executable
inline script, no `on*` attribute and no Alpine expression the CSP build rejects. The editor's
code is the ES module static/js/pages/dashboard.js, loaded for administrators after the core
modules and before Alpine; its texts reach it as the data island `dashboard-texts`, the draft
as the island `tiles-editor` of the editor partial, never as script text.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

_WORLD = Path(tempfile.mkdtemp(prefix="papaia-dashboard-scripts-"))
REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))

import csp_scan  # noqa: E402
import design_world  # noqa: E402

_CONFIG, _WORKSPACE = _WORLD / "config", _WORLD / "workspace"
_CONFIG.mkdir()
_WORKSPACE.mkdir()
for _key, _value in design_world.environment(
    _CONFIG, _WORKSPACE, host="http://localhost:8120", secret="test-session-secret-value"
).items():
    os.environ.setdefault(_key, _value)

from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core import module_catalog  # noqa: E402
from app.main import create_app  # noqa: E402

TEMPLATES = REPO / "src" / "app" / "templates"
MODULE = REPO / "src" / "app" / "static" / "js" / "pages" / "dashboard.js"
GROUP = ("dashboard.html", "partials/tile_editor.html", "partials/tile_gallery.html")
SCRIPT_KINDS = ("inline_scripts", "on_attributes", "alpine_unsupported")
# The names of the shared page script the templates of this group called as globals.
FORMER_GLOBALS = ("fmt", "fmtHtml", "fmtPlural", "showToast", "csrfToken", "_authGuard")
TEXT_KEYS = {
    "whereGroup", "whereTile", "applicationOne", "applicationOther", "widgetOne",
    "widgetOther", "newGroup", "nameCopy", "nameMissing", "widgetGone", "titleTooLong",
    "rawReadFailed", "rawSaved", "saveConflict", "saveFailed", "saved", "discardConfirm",
    "leaveConfirm", "deleteGroupOne", "deleteGroupOther", "addLinkTo", "unsavedOne",
    "unsavedOther", "undoRemoved", "refreshesEvery", "noWidget",
}  # fmt: skip
HOSTILE = "</script><script>alert(1)</script>"


@pytest.fixture
def world(tmp_path: Path) -> Path:
    config, _ = design_world.make_world(tmp_path)
    return config


@pytest.fixture
def client(world: Path, tmp_path: Path) -> Iterator[TestClient]:
    get_settings.cache_clear()
    module_catalog.reset_readers()
    workspace = tmp_path / "workspace"
    settings = get_settings().model_copy(
        update={"papaia_config_dir": str(world), "papaia_workspace_dir": str(workspace)}
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    design_world.stand_ins(app)
    yield TestClient(app, follow_redirects=False)
    get_settings.cache_clear()


def _as(client: TestClient, roles: tuple[str, ...] = design_world.ROLES) -> TestClient:
    client.cookies.set(
        design_world.SESSION_COOKIE,
        design_world.session_cookie(get_settings().manager_session_secret, roles=roles),
    )
    return client


class _Markup(HTMLParser):
    """Scripts, islands and attributes of a rendered page or partial."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[dict[str, str | None]] = []
        self.islands: dict[str, str] = {}
        self.attributes: list[tuple[str, str]] = []
        self._island: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        if tag == "script":
            self.scripts.append(a)
            if a.get("type") == "application/json" and a.get("id"):
                self._island = str(a["id"])
                self.islands[self._island] = ""
        self.attributes.extend((name, value or "") for name, value in attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._island = None

    def handle_data(self, data: str) -> None:
        if self._island is not None:
            self.islands[self._island] += data


def _parse(body: str) -> _Markup:
    markup = _Markup()
    markup.feed(body)
    return markup


def _executable_inline(markup: _Markup) -> list[dict[str, str | None]]:
    return [
        s
        for s in markup.scripts
        if "src" not in s and (s.get("type") or "") not in {"application/json"}
    ]


def _src_index(body: str, name: str) -> int:
    match = re.search(r'<script[^>]+src="/static/' + re.escape(name) + r'[?"]', body)
    assert match is not None, name
    return match.start()


# -- the templates -----------------------------------------------------------------------


@pytest.mark.parametrize("name", GROUP)
def test_the_templates_of_the_group_carry_no_script_finding(name: str) -> None:
    counts = csp_scan.count_markup((TEMPLATES / name).read_text(encoding="utf-8"))
    assert {kind: counts[kind] for kind in SCRIPT_KINDS} == dict.fromkeys(SCRIPT_KINDS, 0)


def test_the_dashboard_runs_on_the_csp_build() -> None:
    doc = json.loads((REPO / "tests" / "csp_baseline.json").read_text(encoding="utf-8"))
    assert "dashboard.html" in doc["converted"]
    rows = doc["templates"]
    assert "dashboard.html" not in rows
    assert "partials/tile_gallery.html" not in rows
    # Inline style attributes are left to the policy ticket; nothing else is.
    editor = rows.get("partials/tile_editor.html", {})
    assert {kind: editor.get(kind, 0) for kind in SCRIPT_KINDS} == dict.fromkeys(SCRIPT_KINDS, 0)


@pytest.mark.parametrize("name", GROUP)
def test_the_templates_call_no_former_global(name: str) -> None:
    source = (TEMPLATES / name).read_text(encoding="utf-8")
    for former in FORMER_GLOBALS:
        assert not re.search(r"(?<![\w$.])" + re.escape(former) + r"\s*\(", source), former
    assert "tilesEnterEdit" not in source
    assert "tilesInitSortable" not in source


def test_the_module_reaches_no_former_global_through_window() -> None:
    source = MODULE.read_text(encoding="utf-8")
    for former in FORMER_GLOBALS:
        assert f"window.{former}" not in source, former
        assert f"win.{former}" not in source, former
    imports = re.findall(r'^import \{([^}]*)\} from "([^"]+)";', source, re.MULTILINE)
    imported = {name.strip(): path for names, path in imports for name in names.split(",")}
    assert imported["fmt"] == imported["fmtPlural"] == "../core/format.js"
    assert imported["csrfToken"] == "../core/csrf.js"
    assert imported["readIsland"] == "../core/islands.js"
    assert imported["pageCore"] == "../core/page.js"


# -- the rendered dashboard ----------------------------------------------------------------


def test_the_dashboard_loads_its_module_before_alpine(client: TestClient) -> None:
    body = _as(client).get("/").text
    markup = _parse(body)
    assert _executable_inline(markup) == []
    modules = [s for s in markup.scripts if (s.get("src") or "").startswith("/static/js/")]
    assert [str(s["src"]).split("?")[0] for s in modules] == [
        "/static/js/core/main.js",
        "/static/js/pages/dashboard.js",
    ]
    assert all(s.get("type") == "module" for s in modules)
    main = _src_index(body, "js/core/main.js")
    sortable = _src_index(body, "sortable.min.js")
    page = _src_index(body, "js/pages/dashboard.js")
    alpine = _src_index(body, "alpine-csp.min.js")
    assert main < sortable < page < alpine
    assert _src_index(body, "htmx.min.js") < alpine
    assert "/static/alpine.min.js" not in body
    assert '<meta name="papaia-csp" content="strict">' in body


def test_the_texts_island_carries_every_text_of_the_module(client: TestClient) -> None:
    texts = json.loads(_parse(_as(client).get("/").text).islands["dashboard-texts"])
    assert set(texts) == TEXT_KEYS
    assert texts["saved"] == "Overview saved."
    assert texts["unsavedOther"] == '<span class="font-semibold">{count}</span> unsaved changes'
    module = MODULE.read_text(encoding="utf-8")
    used = set(re.findall(r"\btexts\.(\w+)", module))
    assert used == TEXT_KEYS


def test_the_texts_island_speaks_the_page_language(client: TestClient) -> None:
    body = _as(client).get("/", headers={"Accept-Language": "de"}).text
    texts = json.loads(_parse(body).islands["dashboard-texts"])
    one = '<span class="font-semibold">{count}</span> ungespeicherte Änderung'
    assert texts["unsavedOne"] == one
    assert texts["saved"] != "Overview saved."


def test_a_person_without_administration_gets_no_editor_script(client: TestClient) -> None:
    body = _as(client, roles=("user",)).get("/").text
    markup = _parse(body)
    assert "js/pages/dashboard.js" not in body
    assert "sortable.min.js" not in body
    assert "dashboard-texts" not in markup.islands
    assert _executable_inline(markup) == []


# -- the partials --------------------------------------------------------------------------


def test_the_gallery_opens_the_editor_through_the_store(client: TestClient) -> None:
    body = _as(client).get("/partials/tiles").text
    markup = _parse(body)
    assert not [name for name, _ in markup.attributes if re.fullmatch(r"on[a-z]+", name)]
    assert ("@click", "$store.tiles.enterEdit()") in markup.attributes
    user = _as(client, roles=("user",)).get("/partials/tiles").text
    assert "$store.tiles" not in user


def test_the_editor_hands_its_draft_over_as_an_island(client: TestClient) -> None:
    body = _as(client).get("/partials/tiles/edit").text
    markup = _parse(body)
    assert _executable_inline(markup) == []
    island = json.loads(markup.islands["tiles-editor"])
    assert set(island) == {
        "draft", "revision", "linkKeys", "widgetTypes", "widgetIcons", "defaultIcons",
    }  # fmt: skip
    api = _as(client).get("/api/v1/tiles").json()
    assert island["revision"] == api["revision"]
    names = [t["name"] for g in island["draft"]["groups"] for t in g["tiles"]]
    assert "LibreChat" in names
    assert ("x-data", "tileEditor") in markup.attributes
    assert ("x-effect", "layout()") in markup.attributes
    assert not [name for name, _ in markup.attributes if name in {"x-init", "x-html"}]
    expressions = " ".join(v for n, v in markup.attributes if csp_scan.is_alpine_attribute(n))
    for former in FORMER_GLOBALS:
        assert not re.search(r"(?<![\w$.])" + re.escape(former) + r"\s*\(", expressions)
    assert "confirm(" not in expressions


def test_a_hostile_group_name_stays_inside_the_island(client: TestClient, world: Path) -> None:
    (world / "manager").mkdir(exist_ok=True)
    tiles: dict[str, Any] = {
        "version": 1,
        "groups": [{"name": HOSTILE, "tiles": [{"name": "A", "href": "https://a.example.test"}]}],
    }
    (world / "manager" / "tiles.yaml").write_text(json.dumps(tiles), encoding="utf-8")
    body = _as(client).get("/partials/tiles/edit").text
    assert HOSTILE not in body
    island = json.loads(_parse(body).islands["tiles-editor"])
    assert island["draft"]["groups"][0]["name"] == HOSTILE
    assert _executable_inline(_parse(body)) == []
