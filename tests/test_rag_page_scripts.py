"""The scripts of Embedding, Models & Usage and Collections (T-UI-03, oberfl 3.3, P4).

embedding.html, collections.html and models.html and the partials htmx swaps into them carry
no executable inline script, no `on*` attribute and no Alpine expression the CSP build
rejects, and the three pages run on the CSP build of Alpine. Their code is the ES modules
static/js/pages/embedding.js, collections.js and models.js, loaded after the core modules and
before Alpine; server values reach them as data islands or `data-*` attributes, never as
script text.
"""

from __future__ import annotations

import base64
import json
import os
import re
import sys
import tempfile
import time
from collections.abc import Iterator
from datetime import date
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-rag-scripts-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-rag-scripts-workspace-")
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

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))

import csp_scan  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core import model_usage, usage_view  # noqa: E402
from app.core.ingest import runs  # noqa: E402
from app.core.rag_collections import meta_point_id  # noqa: E402
from app.core.vectordb.ingest_file import CONNECTIONS_RELPATH  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers.models_deps import get_litellm_transport  # noqa: E402
from app.routers.rag_deps import get_http_transport  # noqa: E402
from tests.fake_ingest import TOKEN, FakeIngest  # noqa: E402
from tests.fake_litellm import MASTER_KEY, FakeLitellm  # noqa: E402
from tests.fake_qdrant import API_KEY, FakeQdrant, Fleet  # noqa: E402

TEMPLATES = REPO / "src" / "app" / "templates"
PAGES = REPO / "src" / "app" / "static" / "js" / "pages"
CONVERTED = ("collections.html", "embedding.html", "models.html")
GROUP = (
    "embedding.html",
    "partials/embedding_body.html",
    "partials/embedding_tree.html",
    "partials/embedding_status.html",
    "partials/embedding_uploads.html",
    "models.html",
    "partials/models_body.html",
    "collections.html",
    "partials/collection_list.html",
)
SCRIPT_KINDS = ("inline_scripts", "on_attributes", "alpine_unsupported")
# What the templates of this group called as globals: the names of the transition (the
# removed core/legacy.js) and the page functions of their former inline scripts.
FORMER_GLOBALS = (
    "_authGuard", "_ragDetail", "_ragError", "_ragEscape", "_ragRequest", "csrfToken", "fmt",
    "fmtHtml", "fmtPlural", "showToast", "reloadEmbedding", "_embedConn", "_embedUrl",
    "reloadCollections", "_collConn", "_collUrl", "_collRoles", "createOperatorGrant",
)  # fmt: skip
EMBED_TEXT_KEYS = {
    "loadFailed", "unavailable", "reloadPage", "replaceTitle", "points", "discardWhat",
    "uploading", "interrupted", "discarded", "startedOne", "startedOther", "abortFailed",
    "abortRequested",
}  # fmt: skip
COLLECTION_TEXT_KEYS = {
    "loadFailed", "unavailable", "reloadPage", "created", "rolesSaved", "deleted",
    "grantFailed", "rolesTitle", "deleteTitle", "points",
}  # fmt: skip
EMBED_STATE_KEYS = {
    "collection", "points", "model", "supportsAdd", "ready", "otherJobs", "activeRun",
}  # fmt: skip
MODEL = "nomic-embed-text"
TODAY = date(2026, 10, 24)
_CSRF = "test-csrf-token-value"


# -- the templates -----------------------------------------------------------------------


@pytest.mark.parametrize("name", GROUP)
def test_the_templates_of_the_group_carry_no_script_the_policy_refuses(name: str) -> None:
    counts = csp_scan.count_markup((TEMPLATES / name).read_text(encoding="utf-8"))
    assert {kind: counts[kind] for kind in SCRIPT_KINDS} == dict.fromkeys(SCRIPT_KINDS, 0)


def test_the_chart_geometry_is_no_style_attribute() -> None:
    source = (TEMPLATES / "partials" / "models_body.html").read_text(encoding="utf-8")
    assert csp_scan.count_markup(source)["style_attributes"] == 0
    for attribute in ("data-pct-bottom", "data-pct-height", "data-pct-width", "data-chart-stroke"):
        assert attribute in source, attribute


@pytest.mark.parametrize("name", GROUP)
def test_no_former_global_is_called_from_the_templates(name: str) -> None:
    source = csp_scan.strip_jinja((TEMPLATES / name).read_text(encoding="utf-8"))
    for former in FORMER_GLOBALS:
        assert re.search(r"(?<![\w$.])" + re.escape(former) + r"\s*\(", source) is None, former


@pytest.mark.parametrize("module", ["embedding.js", "collections.js", "models.js"])
def test_the_page_modules_exist_and_import_what_they_call(module: str) -> None:
    source = (PAGES / module).read_text(encoding="utf-8")
    assert "Alpine.data(" in source
    reached = r"\b(?:window|win|globalThis)\.(?:_rag|fmt|showToast|csrfToken)"
    assert re.search(reached, source) is None


# -- the rendered pages ------------------------------------------------------------------


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
        elif tag == "template" and a.get("data-island") == "application/json" and a.get("id"):
            # The island of a partial htmx swaps in (partials/_csp.html, swapped_island).
            self._island = str(a["id"])
            self.islands[self._island] = ""
        self.attributes.extend((name, value or "") for name, value in attrs)

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "template"):
            self._island = None

    def handle_data(self, data: str) -> None:
        if self._island is not None:
            self.islands[self._island] += data


def _parse(body: str) -> _Markup:
    markup = _Markup()
    markup.feed(body)
    return markup


def _executable_inline(markup: _Markup) -> list[dict[str, str | None]]:
    return [s for s in markup.scripts if "src" not in s and s.get("type") != "application/json"]


def _src_index(body: str, name: str) -> int:
    match = re.search(r'<script[^>]+src="/static/' + re.escape(name) + r'[?"]', body)
    assert match is not None, name
    return match.start()


class World:
    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self.qdrant = FakeQdrant(API_KEY)
        self.qdrant.add("kb", size=4)
        self.qdrant.add("_collection_meta", size=1)
        self.qdrant.put(
            "_collection_meta",
            meta_point_id("kb"),
            {"collection": "kb", "embedding_model": MODEL, "vector_dimension": 4},
        )
        self.ingest = FakeIngest(config_dir)
        self.fleet = Fleet()
        self.fleet.add("qdrant.test", self.qdrant)
        self.fleet.add("qdrant-ingest", self.ingest)
        self.litellm = FakeLitellm()
        self.litellm.use(TODAY, "gpt-4o-mini", prompt=1000, completion=2000, spend=0.002, ok=3)


@pytest.fixture
def world(tmp_path: Path) -> World:
    config_dir = tmp_path / "config"
    (config_dir / "manager").mkdir(parents=True)
    (config_dir / "ai" / "rag" / "documents").mkdir(parents=True)
    (config_dir / "ai" / "litellm").mkdir(parents=True)
    (config_dir / CONNECTIONS_RELPATH.parent).mkdir(parents=True, exist_ok=True)
    (config_dir / ".env").write_text("COMPOSE_PROFILES=keycloak,rag\n", encoding="utf-8")
    (config_dir / "ai" / "rag" / ".env").write_text(
        f"QDRANT_JWT_SECRET={API_KEY}\nQI_CONNECTIONS_SECRET=s\nQI_API_TOKEN={TOKEN}\n",
        encoding="utf-8",
    )
    (config_dir / "ai" / "litellm" / ".env").write_text(
        f"LITELLM_MASTER_KEY={MASTER_KEY}\n", encoding="utf-8"
    )
    return World(config_dir)


@pytest.fixture
def client(world: World, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    async def idle(_kind: object) -> None:
        return None

    monkeypatch.setattr(runs.runner, "find_runner", idle)
    model_usage.reset_cache()
    monkeypatch.setattr(model_usage, "utc_today", lambda: TODAY)
    monkeypatch.setattr(usage_view, "utc_today", lambda: TODAY)
    get_settings.cache_clear()
    settings = get_settings().model_copy(
        update={
            "papaia_config_dir": str(world.config_dir),
            "qdrant_url": "http://qdrant.test:6333",
            "qdrant_ingest_url": "http://qdrant-ingest:8300",
        }
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_http_transport] = lambda: world.fleet.transport()
    app.dependency_overrides[get_litellm_transport] = lambda: world.litellm.transport
    session: dict[str, Any] = {
        "user": {
            "sub": "u-1",
            "preferred_username": "Tester",
            "roles": ["admin"],
            "exp": int(time.time()) + 3600,
        },
        "_csrf_token": _CSRF,
    }
    payload = base64.b64encode(json.dumps(session).encode())
    signed = TimestampSigner(settings.manager_session_secret).sign(payload).decode()
    browser = TestClient(app, follow_redirects=False)
    browser.cookies.set("papaia_manager_session", signed)
    yield browser
    model_usage.reset_cache()
    get_settings.cache_clear()


@pytest.mark.parametrize(
    ("path", "module"),
    [
        ("/embedding?collection=kb", "embedding.js"),
        ("/collections", "collections.js"),
        ("/models", "models.js"),
    ],
)
def test_a_page_loads_its_module_after_the_core_and_before_the_csp_build(
    client: TestClient, path: str, module: str
) -> None:
    body = client.get(path).text
    markup = _parse(body)

    assert _executable_inline(markup) == []
    assert not [name for name, _ in markup.attributes if re.fullmatch(r"on[a-z]+", name)]
    assert '<meta name="papaia-csp" content="strict">' in body
    assert "alpine.min.js" not in body
    main = _src_index(body, "js/core/main.js")
    page = _src_index(body, "js/pages/" + module)
    alpine = _src_index(body, "alpine-csp.min.js")
    assert main < page < alpine
    assert re.search(r'<script type="module" src="/static/js/pages/' + re.escape(module), body)


def test_the_embedding_page_hands_its_texts_as_an_island(client: TestClient) -> None:
    markup = _parse(client.get("/embedding?collection=kb").text)

    texts = json.loads(markup.islands["embed-texts"])
    assert set(texts) == EMBED_TEXT_KEYS
    assert texts["replaceTitle"] == 'Replace <span class="font-mono">{collection}</span>?'


def test_the_collections_page_hands_its_texts_as_an_island(client: TestClient) -> None:
    markup = _parse(client.get("/collections").text)

    texts = json.loads(markup.islands["collections-texts"])
    assert set(texts) == COLLECTION_TEXT_KEYS
    assert texts["deleteTitle"] == 'Delete <span class="font-mono">{name}</span>?'


def test_the_embedding_body_hands_its_state_as_an_island(client: TestClient) -> None:
    markup = _parse(client.get("/partials/embedding?collection=kb").text)

    assert _executable_inline(markup) == []
    state = json.loads(markup.islands["embed-state"])
    assert set(state) == EMBED_STATE_KEYS
    assert state["collection"] == "kb" and state["model"] == MODEL
    assert ("x-init", "loaded()") in markup.attributes


def test_a_hostile_model_name_stays_inside_the_island(client: TestClient, world: World) -> None:
    hostile = "</script><script>alert(1)</script>"
    world.qdrant.put(
        "_collection_meta",
        meta_point_id("kb"),
        {"collection": "kb", "embedding_model": hostile, "vector_dimension": 4},
    )

    body = client.get("/partials/embedding?collection=kb").text
    markup = _parse(body)

    assert "<script>alert(1)" not in body
    assert _executable_inline(markup) == []
    assert json.loads(markup.islands["embed-state"])["model"] == hostile


def test_the_status_strip_says_its_state_in_attributes(client: TestClient, world: World) -> None:
    body = client.get("/partials/embedding/status?collection=kb").text
    markup = _parse(body)

    assert _executable_inline(markup) == []
    assert ("data-announce", "papaia-embed-run") in markup.attributes
    assert ("data-active", "false") in markup.attributes


def test_the_collection_list_carries_no_script(client: TestClient) -> None:
    body = client.get("/partials/collections").text
    markup = _parse(body)

    assert _executable_inline(markup) == []
    assert ("x-data", "newCollection()") in markup.attributes
    assert ("x-data", "editRoles()") in markup.attributes
    assert ("x-data", "deleteCollection()") in markup.attributes
    assert not [name for name, _ in markup.attributes if name == "x-html"]


def test_the_chart_hands_its_tips_as_an_attribute(client: TestClient) -> None:
    body = client.get("/partials/models").text
    markup = _parse(body)

    assert ("x-data", "usageChart") in markup.attributes
    tips = json.loads(dict(markup.attributes)["data-tips"])
    assert len(tips) == 7
    assert tips[-1]["title"] == "Sat, Oct 24"
    assert _executable_inline(markup) == []
