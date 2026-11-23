"""Add-ons, catalogs, nodes, host, services and modules on the target policy (T-UI-03).

oberfl 3.3, P4. The pages of this group (add-ons with an add-on's page, catalogs, nodes with a
node's page, host, services and modules) and the partials htmx swaps into them carry no
executable inline script, no `on*` attribute, no Alpine expression the CSP build refuses and
no inline style. Their scripts are the page modules `static/js/pages/addons.js`,
`catalogs.js`, `nodes.js`, `services.js` and `modules.js`; values from Jinja reach them through
data islands and `data-*` attributes only, and the swapped-in partials run nothing of their
own (an `x-init` method of the owning component at most). Every page of the group is listed as
converted and runs on the CSP build of Alpine.

The browser part serves the manager in the stand-in world (tests/design_serve.py): the policy
check of MGR-003 (tests/csp_browser.mjs) over the pages, and the paths of the group
(tests/addons_services_browser.mjs) in light and dark. It needs the tools of
scripts/design-tools.sh.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
sys.path.insert(0, str(REPO / "tests"))

import csp_page  # noqa: E402
import csp_scan  # noqa: E402
import design_world  # noqa: E402

_WORLD = Path(tempfile.mkdtemp(prefix="papaia-addons-services-scripts-"))
_CONFIG, _WORKSPACE = _WORLD / "config", _WORLD / "workspace"
_CONFIG.mkdir()
_WORKSPACE.mkdir()
for _key, _value in design_world.environment(
    _CONFIG, _WORKSPACE, host="http://localhost:8120", secret="test-session-secret-value"
).items():
    os.environ.setdefault(_key, _value)

from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core import module_catalog, nodes  # noqa: E402
from app.main import create_app  # noqa: E402
from app.templating import templates  # noqa: E402

TEMPLATES = SRC / "app" / "templates"
JS = SRC / "app" / "static" / "js"
# The names core/legacy.js put on `window` during the conversion; it is gone (T-UI-03), and no
# page reaches them as globals any more.
TRANSITION_NAMES = ("csrfToken", "fmt", "fmtPlural", "showToast")
STRICT = '<meta name="papaia-csp" content="strict">'
NODE = shutil.which("node")
CSP_CHECK = REPO / "tests" / "csp_browser.mjs"
PATH_CHECK = REPO / "tests" / "addons_services_browser.mjs"
TOOLS = REPO / ".pk" / "node_modules" / "playwright-core" / "index.mjs"
ALPINE_CSP = SRC / "app" / "static" / "alpine-csp.min.js"

# The templates of this group: the pages, the partials htmx swaps into them and the macros
# they share.
GROUP = (
    "addons.html",
    "addon_detail.html",
    "partials/addon_detail_content.html",
    "partials/addon_gallery.html",
    "partials/_addon_controls.html",
    "partials/_env_fields.html",
    "catalogs.html",
    "partials/catalog_list.html",
    "nodes.html",
    "node_detail.html",
    "host.html",
    "partials/host_list.html",
    "services.html",
    "partials/service_list.html",
    "partials/service_status_chip.html",
    "partials/_group_controls.html",
    "partials/_actions.html",
    "partials/stack_runner.html",
    "modules.html",
)
CONVERTED = (
    "addon_detail.html",
    "addons.html",
    "catalogs.html",
    "host.html",
    "modules.html",
    "node_detail.html",
    "nodes.html",
    "services.html",
)
# The Alpine components each page module registers (`Alpine.data`).
COMPONENTS = {
    "addons.js": {"catalogRefresh", "addonControls"},
    "catalogs.js": {"catalogRow"},
    "services.js": {"stackControls", "serviceGroups", "serviceAddon", "stackRunner"},
    "modules.js": {"delegationOverview"},
    "nodes.js": set(),
}
NODE_NAME = design_world.SAMPLES["name"]


# -- fixtures ------------------------------------------------------------------------------


def _client(tmp_path: Path, **session: Any) -> TestClient:
    config, workspace = design_world.make_world(tmp_path)
    get_settings.cache_clear()
    module_catalog.reset_readers()
    settings = get_settings().model_copy(
        update={"papaia_config_dir": str(config), "papaia_workspace_dir": str(workspace)}
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    design_world.stand_ins(app)
    test_client = TestClient(app, follow_redirects=False)
    test_client.cookies.set(
        design_world.SESSION_COOKIE,
        design_world.session_cookie(get_settings().manager_session_secret, **session),
    )
    return test_client


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    yield _client(tmp_path)
    get_settings.cache_clear()
    module_catalog.reset_readers()


@pytest.fixture
def stepped_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    """A fresh sign-in, and a node that waits for its host key and its bundle."""
    pending = nodes.NodeState(
        name=NODE_NAME,
        address="127.0.0.2",
        executor="ssh",
        controller=False,
        services=(),
        host_key_pinned=False,
        reachable=False,
        no_bundle=True,
    )

    async def listed(**kwargs: Any) -> list[nodes.NodeState]:
        return [pending]

    async def keys(name: str, **kwargs: Any) -> list[nodes.HostKey]:
        return [nodes.HostKey("ssh-ed25519", "SHA256:" + "b" * 43)]

    monkeypatch.setattr(nodes, "overview", listed)
    monkeypatch.setattr(nodes, "host_keys", keys)
    yield _client(tmp_path, step_up_at=int(time.time()))
    get_settings.cache_clear()
    module_catalog.reset_readers()


class _Markup(HTMLParser):
    """Scripts, data islands, inline event handlers and Alpine attributes of a page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[dict[str, str | None]] = []
        self.handlers: list[str] = []
        self.islands: dict[str, str] = {}
        self.inline_bodies: list[str] = []
        self.alpine: list[tuple[str, str | None]] = []
        self.styles: list[str | None] = []
        self._script: dict[str, str | None] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handlers += [n for n, _ in attrs if n.startswith("on") and n[2:].isalpha()]
        self.alpine += [(n, v) for n, v in attrs if csp_scan.is_alpine_attribute(n)]
        self.styles += [v for n, v in attrs if n == "style"]
        if tag == "script":
            self._script = dict(attrs)
            self.scripts.append(self._script)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._script = None

    def handle_data(self, data: str) -> None:
        script = self._script
        if script is None or not data.strip():
            return
        if script.get("type") == "application/json":
            self.islands[str(script.get("id"))] = data
        elif "src" not in script:
            self.inline_bodies.append(data.strip())


def _markup(body: str) -> _Markup:
    parser = _Markup()
    parser.feed(body)
    parser.close()
    return parser


def _assert_clean_fragment(body: str) -> _Markup:
    """A swapped-in fragment: nothing executable, no handler, no inline style, no refused
    Alpine expression, and every component it declares is one a page module registers."""
    markup = _markup(body)
    assert "<script" not in body
    assert markup.handlers == []
    assert markup.styles == []
    refused = [(n, v) for n, v in markup.alpine if csp_scan.alpine_rejected(n, v)]
    assert refused == []
    registered = set().union(*COMPONENTS.values())
    for name, value in markup.alpine:
        if name == "x-data" and value and not value.lstrip().startswith("{"):
            assert value.removesuffix("()") in registered, value
    return markup


# -- the templates -------------------------------------------------------------------------


@pytest.mark.parametrize("name", GROUP)
def test_the_templates_of_the_group_have_nothing_the_policy_refuses(name: str) -> None:
    counts = csp_scan.count_markup((TEMPLATES / name).read_text(encoding="utf-8"))
    assert counts == dict.fromkeys(csp_scan.KINDS, 0)


# -- the pages -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "module"),
    [
        ("/addons", "addons.js"),
        (f"/addons/{NODE_NAME}", "addons.js"),
        ("/catalogs", "catalogs.js"),
        ("/services", "services.js"),
    ],
)
def test_the_page_runs_its_module_on_the_csp_build(
    client: TestClient, path: str, module: str
) -> None:
    response = client.get(path)
    assert response.status_code == 200
    csp_page.assert_converted_page(response.text, module)


def test_the_host_page_runs_on_the_csp_build_without_a_module(client: TestClient) -> None:
    body = client.get("/host").text
    assert body.count(STRICT) == 1
    markup = _markup(body)
    assert markup.inline_bodies == []
    assert markup.handlers == []
    assert "/static/js/pages/" not in body
    # The local time of [data-checked-at] is written by core/format.js after every swap.
    assert "localizeCheckedAt" in (JS / "core" / "main.js").read_text(encoding="utf-8")


def test_the_catalogs_page_binds_its_dialogs_through_the_module(client: TestClient) -> None:
    body = client.get("/catalogs").text
    texts = csp_page.island(body, "catalogs-texts")
    assert texts == {"addFailed": "Add catalog failed:"}
    # The elements the module binds, with the ids and field names they had.
    for hook in (
        'id="add-catalog-form"',
        'id="edit-catalog-form"',
        'id="catalog-type-select" name="type"',
        'id="add-name" name="name"',
        'id="add-url" name="url"',
        'id="add-ref" name="ref"',
        'id="add-path" name="path"',
        'data-dialog-open="add-catalog-modal"',
        'data-dialog-close="add-catalog-modal"',
        'data-dialog-close="edit-catalog-modal"',
    ):
        assert hook in body, hook


def test_the_services_page_hands_its_sentences_over_in_an_island(client: TestClient) -> None:
    body = client.get("/services").text
    texts = csp_page.island(body, "services-texts")
    assert texts["groupsSelected"] == ["{count} group selected", "{count} groups selected"]
    assert texts["modules"] == ["{count} module", "{count} modules"]
    assert texts["restartGroups"] == [
        "Restart {count} service group?",
        "Restart {count} service groups?",
    ]
    assert texts["stackStop"] == "Stop the whole stack?"
    assert set(texts) == {
        "groupsSelected",
        "modules",
        "startGroups",
        "stopGroups",
        "restartGroups",
        "stackStart",
        "stackStop",
        "stackRestart",
    }
    assert 'x-data="stackControls"' in body
    assert 'id="service-controls" x-data="serviceGroups"' in body


def test_the_nodes_page_loads_its_module_only_after_a_fresh_sign_in(
    client: TestClient, stepped_up: TestClient
) -> None:
    plain = client.get("/nodes").text
    assert plain.count(STRICT) == 1
    assert "/static/js/pages/nodes.js" not in plain
    assert 'id="nodes-init"' not in plain
    body = stepped_up.get("/nodes").text
    csp_page.assert_converted_page(body, "nodes.js")
    init = csp_page.island(body, "nodes-init")
    assert init == {"name": "", "texts": {"addFailed": "Adding the node failed:"}}
    assert '<form id="add-node"' in body
    for field in ("name", "address", "ssh_user", "ssh_port"):
        assert f'name="{field}"' in body, field


def test_the_node_page_marks_its_buttons_for_the_module(stepped_up: TestClient) -> None:
    body = stepped_up.get(f"/nodes/{NODE_NAME}").text
    csp_page.assert_converted_page(body, "nodes.js")
    init = csp_page.island(body, "nodes-init")
    assert init["name"] == NODE_NAME
    assert set(init["texts"]) == {"failed", "removeQuestion"}
    fingerprint = "SHA256:" + "b" * 43
    assert f'data-node-action="pinKey" data-fingerprint="{fingerprint}"' in body
    assert 'data-node-action="sendBundle"' in body
    assert 'data-node-action="removeNode"' in body


def test_the_modules_page_loads_its_module_with_the_overview(client: TestClient) -> None:
    body = client.get("/modules").text
    csp_page.assert_converted_page(body, "modules.js")
    assert 'x-data="delegationOverview()" x-init="load()"' in body
    texts = csp_page.island(body, "delegation-texts")
    assert texts == {
        "confirm": "End every delegation of {person} at {module}?",
        "revoked": "The delegations of {person} at {module} are revoked.",
        "failed": "The overview could not be loaded: {detail}",
    }


def test_the_modules_page_without_the_overview_loads_no_module(tmp_path: Path) -> None:
    test_client = _client(tmp_path, roles=("admin", "user"))
    try:
        body = test_client.get("/modules").text
    finally:
        get_settings.cache_clear()
        module_catalog.reset_readers()
    assert body.count(STRICT) == 1
    assert "delegationOverview" not in body
    assert "/static/js/pages/modules.js" not in body
    assert 'id="delegation-texts"' not in body


# -- the partials htmx swaps in ------------------------------------------------------------


def _addon(**values: Any) -> dict[str, Any]:
    field = {
        "key": "DEMO_URL",
        "label": "URL",
        "required": True,
        "prompt_on_install": True,
        "is_secret": False,
        "marker": None,
        "example_value": "https://demo.example.test",
        "prefill": "https://demo.example.test",
        "html_input_type": "url",
        "placeholder": "",
        "current_value": "https://demo.example.test",
        "current_set": True,
        "default": "",
    }
    secret = {
        **field,
        "key": "DEMO_TOKEN",
        "label": "Token",
        "required": False,
        "is_secret": True,
        "prompt_on_install": False,
        "current_value": "",
        "current_set": False,
    }
    return {
        "name": "demo",
        "catalog": "main",
        "catalog_version": "1.1.0",
        "installed_version": None,
        "status": "available",
        "env_fields": [field, secret],
        **values,
    }


def test_the_gallery_runs_on_the_add_on_component() -> None:
    html = templates.env.get_template("partials/addon_gallery.html").render(
        addons=[
            _addon(),
            _addon(name="wiki", status="running", installed_version="1.0.0", update_available=True),
            _addon(name="chat", status="installed", installed_version="1.0.0"),
        ],
        request=None,
    )
    markup = _assert_clean_fragment(html)
    assert html.count('x-data="addonControls"') == 3
    assert "run('gallery', 'wiki', 'stop')" in html
    assert "run('gallery', 'chat', 'start')" in html
    assert "confirmRun('removeModal', 'gallery', 'wiki', 'remove')" in html
    assert "confirmRun('uninstallModal', 'gallery', 'wiki', 'uninstall')" in html
    assert "install($el, 'gallery', 'demo', 'main')" in html
    # The install form keeps its field hooks.
    assert 'data-env-key="DEMO_URL"' in html and 'name="start"' in html
    # Hidden until Alpine starts, without an inline style.
    assert ("x-cloak", "") in markup.alpine or ("x-cloak", None) in markup.alpine


def test_the_detail_panel_runs_on_the_add_on_component() -> None:
    html = templates.env.get_template("partials/addon_detail_content.html").render(
        addon=_addon(
            status="running",
            installed_version="1.0.0",
            previous={
                "version": "0.9.0",
                "available": True,
                "restore_point": "rp-1",
                "restore_point_available": True,
            },
            upgrades_supported=True,
            actions=[{"name": "reset", "description": "Reset", "destructive": True}],
        ),
        request=None,
    )
    _assert_clean_fragment(html)
    # The header cluster, the maintenance actions, the versions and the editor: one component
    # each, so `busy` stays per area as before.
    assert html.count('x-data="addonControls"') == 4
    assert "run('detail', 'demo', 'stop')" in html
    assert "maintain('demo', &#34;reset&#34;, true, " in html
    assert 'x-show="activeVerb === &#34;reset&#34;"' in html
    assert '@click="openRollback()"' in html and "rollback('demo')" in html
    assert 'x-data="addonControls" @input="noteInput($event)"' in html
    assert '@click="cancelEdit()"' in html
    assert "saveConfig('demo', false)" in html and "saveConfig('demo', true)" in html


def test_the_catalog_list_runs_on_the_row_component() -> None:
    html = templates.env.get_template("partials/catalog_list.html").render(
        catalogs=[
            {
                "name": "main",
                "type": "git",
                "url": "https://git.example.test/addons.git",
                "ref": "main",
                "enabled": True,
                "auth": False,
            },
            {
                "name": "local",
                "type": "local",
                "path": "/srv/addons",
                "ref": None,
                "enabled": False,
            },
        ],
        addon_counts={},
        shadowed_counts={},
        request=None,
    )
    _assert_clean_fragment(html)
    assert html.count('x-data="catalogRow"') == 2
    assert "refresh('main')" in html
    assert "remove('main', " in html
    assert (
        'data-name="main" data-type="git"\n'
        '                   data-url="https://git.example.test/addons.git" data-ref="main"\n'
        '                   data-path="" data-enabled="true"'
    ) in html
    assert 'data-name="local" data-type="local"' in html
    assert (
        'data-url="" data-ref=""' in html and 'data-path="/srv/addons" data-enabled="false"' in html
    )


def test_the_service_list_hands_the_group_map_to_the_selection(client: TestClient) -> None:
    html = templates.env.get_template("partials/service_list.html").render(
        core_modules=[],
        addon_modules=[],
        unreachable_nodes=[],
        group_map={"rag": ["qdrant", "ingest"], "nginx": ["nginx"]},
        request=None,
    )
    markup = _assert_clean_fragment(html)
    (value,) = [v for n, v in markup.alpine if n == "x-init"]
    assert value == "setGroups($el)"
    found = re.search(r'data-group-map="([^"]*)"', html)
    assert found is not None
    decoded = found.group(1).replace("&#34;", '"').replace("&amp;", "&")
    assert json.loads(decoded) == {"rag": ["qdrant", "ingest"], "nginx": ["nginx"]}
    # Without a map the selection is pruned against an empty one.
    empty = templates.env.get_template("partials/service_list.html").render(
        core_modules=[], addon_modules=[], unreachable_nodes=[], request=None
    )
    assert 'data-group-map="{}"' in empty
    # The real partial, from the route.
    _assert_clean_fragment(client.get("/partials/services").text)


def test_the_stack_runner_strip_dismisses_through_its_component() -> None:
    html = templates.env.get_template("partials/stack_runner.html").render(
        stack={
            "active": True,
            "running": False,
            "succeeded": True,
            "action": "restart",
            "finished_at": "2026-11-23T10:00:00Z",
            "log": "",
        },
        request=None,
    )
    _assert_clean_fragment(html)
    assert 'x-data="stackRunner"' in html and '@click="dismiss()"' in html


def test_the_host_list_carries_no_script() -> None:
    # The local time of [data-checked-at] comes from core/format.js (localizeCheckedAt), which
    # main.js runs after every swap; the partial has no script of its own in any branch.
    source = (TEMPLATES / "partials" / "host_list.html").read_text(encoding="utf-8")
    assert "<script" not in source
    html = templates.env.get_template("partials/host_list.html").render(
        host={"available": True, "checks": [], "notes": ["Nothing measured yet."]},
        request=None,
    )
    _assert_clean_fragment(html)


def test_a_floating_menu_is_fixed_by_the_stylesheet() -> None:
    html = templates.env.from_string(
        '{% from "partials/_actions.html" import overflow_menu %}'
        "{% call overflow_menu(size='xs', float=True) %}<li>x</li>{% endcall %}"
    ).render()
    assert "dropdown-float" in html and "style=" not in html
    css = (REPO / "docker" / "tailwind.app.css").read_text(encoding="utf-8")
    assert re.search(r"\.dropdown-float \.dropdown-content \{\s*position: fixed;", css)


# -- the page modules ----------------------------------------------------------------------

_IMPORT = re.compile(r'^import\s+\{([^}]*)\}\s+from\s+"([^"]+)";', re.MULTILINE)


@pytest.mark.parametrize("module", sorted(COMPONENTS))
def test_the_page_modules_import_what_they_use(module: str) -> None:
    source = (JS / "pages" / module).read_text(encoding="utf-8")
    imports = _IMPORT.findall(source)
    assert imports, module
    for _, target in imports:
        assert target.startswith("../core/"), target
    names = TRANSITION_NAMES
    imported = {n.strip().split(" as ")[-1] for names_, _ in imports for n in names_.split(",")}
    body = _IMPORT.sub("", source)
    for name in names:
        if re.search(r"(?<![\w$.])" + re.escape(name) + r"(?![\w$])", body):
            assert name in imported, f"{module} uses the global {name}"
    registered = set(re.findall(r'Alpine\.data\("(\w+)"', source))
    assert registered == COMPONENTS[module]


def test_the_shared_actions_of_this_group_are_no_globals_any_more() -> None:
    # The transition module that put names on `window` is gone; no module assigns one.
    assert not (JS / "core" / "legacy.js").exists()
    sources = "\n".join(path.read_text(encoding="utf-8") for path in JS.rglob("*.js"))
    for name in (
        "_addonPost",
        "_stackBody",
        "addonAction",
        "addonMaintenanceAction",
        "catalogAction",
        "catalogDelete",
        "catalogUpdate",
        "galleryAction",
        "refreshStackRunner",
        "serviceAddonAction",
        "stackAction",
        "stackGroupAction",
    ):
        assert not re.search(r"\bwindow\." + re.escape(name) + r"\s*=", sources), name


# -- browser check -------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path, Path]]:
    """The manager of the stand-in world on 127.0.0.1, with Anna's storage state, and the
    partials of this group rendered from the templates with sample add-ons and catalogs."""
    folder = tmp_path_factory.mktemp("addons-services-browser")
    state, log = folder / "storage-state.json", folder / "server.log"
    fragments = folder / "fragments"
    fragments.mkdir()
    _write_fragments(fragments)
    port = _free_port()
    log_file = log.open("wb")
    server = subprocess.Popen(  # noqa: S603 -- fixed argv, the interpreter of this run
        [
            sys.executable,
            str(REPO / "tests" / "design_serve.py"),
            "--port",
            str(port),
            "--storage-state",
            str(state),
        ],
        cwd=SRC,
        stdout=log_file,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + 60
        while True:
            if server.poll() is not None:
                raise RuntimeError(f"the manager did not start: {log.read_text()[-2000:]}")
            try:
                with urllib.request.urlopen(f"{base}/health", timeout=2) as answer:  # noqa: S310
                    if answer.status == 200 and state.is_file() and state.stat().st_size:
                        break
            except (urllib.error.URLError, ConnectionError, TimeoutError):
                pass
            if time.monotonic() > deadline:
                raise RuntimeError("the manager did not answer within 60 s")
            time.sleep(0.3)
        yield base, state, fragments
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        log_file.close()


def _write_fragments(folder: Path) -> None:
    """The partials the stand-in world has no data for, from the manager's own templates."""
    gallery = templates.env.get_template("partials/addon_gallery.html").render(
        addons=[
            _addon(),
            _addon(name="chat", status="installed", installed_version="1.0.0"),
            _addon(name="wiki", status="running", installed_version="1.0.0"),
        ],
        request=None,
    )
    detail = templates.env.get_template("partials/addon_detail_content.html").render(
        addon=_addon(name="wiki", status="running", installed_version="1.0.0"),
        request=None,
    )
    catalogs = templates.env.get_template("partials/catalog_list.html").render(
        catalogs=[
            {
                "name": "main",
                "type": "git",
                "url": "https://git.example.test/addons.git",
                "ref": "main",
                "enabled": True,
                "auth": False,
            }
        ],
        addon_counts={"main": 2},
        shadowed_counts={},
        request=None,
    )
    module = {
        "name": "rag",
        "health": "healthy",
        "addon": None,
        "profiles": ["rag"],
        "summary": "2 / 2 running",
        "containers": [],
    }
    services = templates.env.get_template("partials/service_list.html").render(
        core_modules=[module, {**module, "name": "nginx", "profiles": ["nginx"]}],
        addon_modules=[{**module, "name": "wiki", "addon": "wiki", "profiles": ["wiki"]}],
        unreachable_nodes=[],
        group_count=2,
        total=3,
        cnt_running=3,
        cnt_degraded=0,
        cnt_stopped=0,
        cnt_missing=0,
        group_map={"rag": ["rag"], "nginx": ["nginx"]},
        self_profile="manager",
        link_profile="",
        request=None,
    )
    runner = templates.env.get_template("partials/stack_runner.html").render(
        stack={
            "active": True,
            "running": False,
            "succeeded": True,
            "action": "restart",
            "finished_at": "2026-11-23T10:00:00Z",
            "log": "",
        },
        request=None,
    )
    for name, html in {
        "addons.html": gallery,
        "addon-wiki.html": detail,
        "catalogs.html": catalogs,
        "services.html": services,
        "stack-runner.html": runner,
    }.items():
        (folder / name).write_text(html, encoding="utf-8")


_browser = pytest.mark.skipif(
    NODE is None or not TOOLS.is_file() or not ALPINE_CSP.is_file(),
    reason="browser tools not installed (scripts/design-tools.sh)",
)
_BROWSER_PAGES = (
    "/addons",
    f"/addons/{NODE_NAME}",
    "/catalogs",
    "/host",
    "/modules",
    "/nodes",
    f"/nodes/{NODE_NAME}",
    "/services",
)


@_browser
def test_the_browser_sees_no_report_and_no_alpine_error(
    served: tuple[str, Path, Path], tmp_path: Path
) -> None:
    base, state, _ = served
    report = tmp_path / "report.json"
    assert NODE is not None
    args = [NODE, str(CSP_CHECK), "--base-url", base, "--storage-state", str(state)]
    for path in _BROWSER_PAGES:
        args += ["--page", path]
    done = subprocess.run(  # noqa: S603 -- fixed argv
        [*args, "--report", str(report)],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=240,
    )
    assert done.returncode == 0, done.stderr
    results = json.loads(report.read_text(encoding="utf-8"))["results"]
    # Every page in the light and the dark colour scheme.
    assert sorted((r["page"], r["scheme"]) for r in results) == sorted(
        (page, scheme) for page in _BROWSER_PAGES for scheme in ("light", "dark")
    )
    for result in results:
        assert result["policy"].startswith("default-src 'self'"), result
    for result in results:
        assert result["status"] == 200, result
        assert result["converted"] is True, result
        assert result["violations"] == [], result
        assert result["alpine"] == [], result
        assert result["errors"] == [], result


@_browser
def test_the_paths_of_the_group_work_in_a_browser(served: tuple[str, Path, Path]) -> None:
    base, state, fragments = served
    assert NODE is not None
    done = subprocess.run(  # noqa: S603 -- fixed argv
        [
            NODE,
            str(PATH_CHECK),
            "--base-url",
            base,
            "--storage-state",
            str(state),
            "--fragments",
            str(fragments),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert done.returncode in (0, 1), done.stdout + done.stderr
    report = json.loads(done.stdout)
    failed = [c for c in report["checks"] if not c["ok"]]
    assert failed == [], json.dumps(failed, indent=2)
    names = {c["name"] for c in report["checks"]}
    for theme in ("light", "dark"):
        assert f"{theme}: no report of the content security policy" in names
        assert f"addons ({theme}): install sends the fields and the start choice" in names
        assert f"catalogs ({theme}): the edit dialog saves through PUT" in names
        assert f"services ({theme}): a group restart sends the selection" in names
    assert done.returncode == 0


def test_the_path_check_refuses_a_page_off_the_loopback() -> None:
    if NODE is None:
        pytest.skip("node is not available")
    done = subprocess.run(  # noqa: S603 -- fixed argv
        [
            NODE,
            str(PATH_CHECK),
            "--base-url",
            "http://example.test",
            "--storage-state",
            "x",
            "--fragments",
            "y",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert done.returncode == 2
    assert "loopback only" in done.stderr
