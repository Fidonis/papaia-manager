"""Connections and notifications on the target policy (T-UI-03).

oberfl 3.3, P4. The Connections page (`connections.html` with the list htmx swaps in,
`partials/connection_list.html`) and the notifications page (`notifications.html`, at /mail)
carry no executable inline script, no `on*` attribute and no Alpine expression the CSP build
refuses. Their scripts are the page modules `static/js/pages/connections.js` and
`static/js/pages/notifications.js`; values from Jinja reach them through data islands and
`data-*` attributes only, and their dialogs are `Alpine.data` components the modules register
before Alpine starts. Both pages are listed as converted and run on the CSP build of Alpine.

The browser part serves the manager in the stand-in world (tests/design_serve.py) with a
writable connection store: the policy check of MGR-003 (tests/csp_browser.mjs) over both pages,
and the paths of the group (tests/connections_notifications_browser.mjs) in light and dark. It
needs the tools of scripts/design-tools.sh.
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

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
sys.path.insert(0, str(REPO / "tests"))

import csp_scan  # noqa: E402
import design_world  # noqa: E402

_WORLD = Path(tempfile.mkdtemp(prefix="papaia-connections-scripts-"))
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

TEMPLATES = SRC / "app" / "templates"
JS = SRC / "app" / "static" / "js"
PAGES_JS = JS / "pages"
# The names core/legacy.js put on `window` during the conversion; it is gone (T-UI-03), and no
# page reaches them as globals any more.
TRANSITION_NAMES = ("csrfToken", "fmt", "fmtPlural", "showToast")
STRICT = '<meta name="papaia-csp" content="strict">'
NODE = shutil.which("node")
CSP_CHECK = REPO / "tests" / "csp_browser.mjs"
PATH_CHECK = REPO / "tests" / "connections_notifications_browser.mjs"
TOOLS = REPO / ".pk" / "node_modules" / "playwright-core" / "index.mjs"
ALPINE_CSP = SRC / "app" / "static" / "alpine-csp.min.js"

# The templates of this group, and the shared macros of the row actions they import.
GROUP = (
    "connections.html",
    "partials/connection_list.html",
    "notifications.html",
    "partials/_actions.html",
)
# Page path, top-level template, page module.
PAGES = {
    "/connections": ("connections.html", "js/pages/connections.js"),
    "/mail": ("notifications.html", "js/pages/notifications.js"),
}
CSRF = {"X-CSRF-Token": design_world.CSRF}


# -- fixtures ------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    config, workspace = design_world.make_world(tmp_path, rag_catalog=True)
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
        design_world.session_cookie(get_settings().manager_session_secret),
    )
    yield test_client
    get_settings.cache_clear()
    module_catalog.reset_readers()


class _Markup(HTMLParser):
    """Scripts, data islands, inline event handlers and style attributes of a page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[dict[str, str | None]] = []
        self.handlers: list[str] = []
        self.styles: list[str] = []
        self.islands: dict[str, str] = {}
        self.inline_bodies: list[str] = []
        self._script: dict[str, str | None] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handlers += [name for name, _ in attrs if name.startswith("on")]
        self.styles += [str(value) for name, value in attrs if name == "style"]
        if tag == "script":
            self._script = dict(attrs)
            self.scripts.append(self._script)
        elif tag == "template" and dict(attrs).get("data-island") == "application/json":
            # The island of a partial htmx swaps in (partials/_csp.html, swapped_island).
            self._script = {"type": "application/json", "id": dict(attrs).get("id")}

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "template"):
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


def _path(src: str | None) -> str:
    return str(src).split("?", 1)[0]


def _create_connection(client: TestClient, name: str = "archive") -> None:
    response = client.post(
        "/api/v1/rag/connections",
        json={"name": name, "type": "qdrant", "fields": {"url": "http://127.0.0.1:9"}},
        headers=CSRF,
    )
    assert response.status_code == 201, response.text


def _create_recipient(client: TestClient) -> dict[str, object]:
    revision = client.get("/api/v1/notifications").json()["revision"]
    response = client.post(
        "/api/v1/notifications/recipients",
        json={
            "revision": revision,
            "kind": "webhook",
            "name": "Ops <team>",
            "target": "https://hooks.example.test/papaia",
            "enabled": True,
            "events": ["job_failed"],
        },
        headers=CSRF,
    )
    assert response.status_code == 201, response.text
    doc: dict[str, object] = client.get("/api/v1/notifications").json()
    return doc


# -- the templates -------------------------------------------------------------------------


@pytest.mark.parametrize("name", GROUP)
def test_the_templates_of_the_group_have_nothing_the_policy_refuses(name: str) -> None:
    counts = csp_scan.count_markup((TEMPLATES / name).read_text(encoding="utf-8"))
    assert counts == dict.fromkeys(csp_scan.KINDS, 0)


@pytest.mark.parametrize("name", ["connections.html", "notifications.html"])
def test_no_template_of_the_group_calls_a_transitional_global(name: str) -> None:
    source = (TEMPLATES / name).read_text(encoding="utf-8")
    if name == "connections.html":
        source += (TEMPLATES / "partials" / "connection_list.html").read_text(encoding="utf-8")
    for global_name in TRANSITION_NAMES:
        # `fmt(...)` in an expression is the method of the component, which the module gives
        # every component of the group; no other transitional name may appear.
        if global_name == "fmt":
            continue
        assert not re.search(r"(?<![\w$.-])" + re.escape(global_name) + r"\s*\(", source), (
            global_name
        )


@pytest.mark.parametrize("path", sorted(PAGES))
def test_the_page_runs_its_module_on_the_csp_build(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    body = response.text
    assert body.count(STRICT) == 1
    markup = _markup(body)
    assert markup.inline_bodies == []
    assert markup.handlers == []
    sources = [_path(s.get("src")) for s in markup.scripts if s.get("src")]
    assert [s for s in sources if "alpine" in s] == ["/static/alpine-csp.min.js"]
    module = "/static/" + PAGES[path][1]
    (tag,) = [s for s in markup.scripts if _path(s.get("src")) == module]
    assert tag.get("type") == "module"
    # After the shared modules and before Alpine, so its components exist when Alpine starts.
    assert sources.index("/static/js/core/main.js") < sources.index(module)
    assert sources.index(module) < sources.index("/static/alpine-csp.min.js")


def test_the_connection_list_carries_no_script_and_its_types_in_an_island(
    client: TestClient,
) -> None:
    _create_connection(client)
    body = client.get("/partials/connections").text
    markup = _markup(body)
    assert markup.inline_bodies == []
    assert markup.handlers == []
    assert markup.styles == []
    assert "<script" not in body.replace('<script type="application/json"', "")
    types = json.loads(markup.islands["mgr-connection-types"])
    listed = client.get("/api/v1/rag/connections").json()
    assert types == listed["types"]
    # The hooks the module and the rows use, unchanged.
    for hook in (
        'id="connection-modal"',
        'id="delete-connection-modal"',
        'data-connection="archive"',
    ):
        assert hook in body, hook
    # The dialogs are components the module registers, not functions of the page.
    for component in ("connectionDialog", "deleteConnection", "resetConnection", "connectionRow"):
        assert f'x-data="{component}"' in body, component


def test_the_connections_page_hands_its_texts_over_in_an_island(client: TestClient) -> None:
    body = client.get("/connections").text
    page = json.loads(_markup(body).islands["mgr-connections"])
    assert set(page["texts"]) == {
        "listFailed",
        "listUnavailable",
        "created",
        "saved",
        "deleted",
        "reset",
    }
    assert page["texts"]["created"] == "Connection <b>{name}</b> created"
    assert "connection-new" in body


def test_the_notifications_page_hands_its_values_over_in_an_island(client: TestClient) -> None:
    doc = _create_recipient(client)
    body = client.get("/mail").text
    page = json.loads(_markup(body).islands["mgr-notifications"])
    assert page["doc"] == doc
    assert set(page["texts"]) == {
        "saved",
        "testSent",
        "testFailed",
        "settingsSaved",
        "enterName",
        "enterAddress",
        "enterUrl",
        "added",
        "changed",
        "deleted",
    }
    # The hooks the module binds, with the ones they had.
    recipient = doc["recipients"][0]  # type: ignore[index]
    for hook in (
        'id="notify-settings"',
        'id="set-save"',
        f'data-recipient="{recipient["id"]}"',
        'data-event="job_failed"',
        "data-recipient-test",
        'x-data="recipientDialog"',
        'x-data="deleteRecipient"',
    ):
        assert hook in body, hook
    # A name never reaches the page as markup, neither in the rows nor in the island.
    assert "Ops <team>" not in body


# -- the page modules ----------------------------------------------------------------------

_IMPORT = re.compile(r'^import\s+\{([^}]*)\}\s+from\s+"([^"]+)";', re.MULTILINE)


@pytest.mark.parametrize("module", ["connections.js", "notifications.js"])
def test_the_page_modules_import_what_they_use(module: str) -> None:
    source = (PAGES_JS / module).read_text(encoding="utf-8")
    imports = _IMPORT.findall(source)
    assert imports, module
    for _, target in imports:
        assert target.startswith("../core/"), target
    imported = {n.strip().split(" as ")[-1] for names_, _ in imports for n in names_.split(",")}
    body = _IMPORT.sub("", source)
    for name in TRANSITION_NAMES:
        if re.search(r"(?<![\w$.])" + re.escape(name) + r"(?![\w$])", body):
            assert name in imported, f"{module} uses the global {name}"
    # Registered on alpine:init, before Alpine starts.
    assert 'addEventListener("alpine:init"' in source


# -- browser check -------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    """The manager of the stand-in world on 127.0.0.1, with Anna's storage state."""
    folder = tmp_path_factory.mktemp("connections-browser")
    state, log = folder / "storage-state.json", folder / "server.log"
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
            "--rag-catalog",
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
        yield base, state
    finally:
        server.terminate()
        try:
            server.wait(timeout=10)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait()
        log_file.close()


_browser = pytest.mark.skipif(
    NODE is None or not TOOLS.is_file() or not ALPINE_CSP.is_file(),
    reason="browser tools not installed (scripts/design-tools.sh)",
)


@_browser
def test_the_browser_sees_no_report_and_no_alpine_error(
    served: tuple[str, Path], tmp_path: Path
) -> None:
    base, state = served
    report = tmp_path / "report.json"
    assert NODE is not None
    args = [NODE, str(CSP_CHECK), "--base-url", base, "--storage-state", str(state)]
    for path in sorted(PAGES):
        args += ["--page", path]
    done = subprocess.run(  # noqa: S603 -- fixed argv
        [*args, "--report", str(report)],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert done.returncode == 0, done.stderr
    results = json.loads(report.read_text(encoding="utf-8"))["results"]
    # Every page in the light and the dark colour scheme.
    assert sorted((r["page"], r["scheme"]) for r in results) == sorted(
        (page, scheme) for page in PAGES for scheme in ("light", "dark")
    )
    for result in results:
        assert result["policy"].startswith("default-src 'self'"), result
    for result in results:
        assert result["status"] == 200, result
        assert result["converted"] is True, result
        assert result["violations"] == [], result
        assert result["alpine"] == [], result
        assert result["errors"] == [], result
        assert result["alpine_version"] is not None, result


@_browser
def test_the_paths_of_the_group_work_in_a_browser(served: tuple[str, Path]) -> None:
    base, state = served
    assert NODE is not None
    done = subprocess.run(  # noqa: S603 -- fixed argv
        [NODE, str(PATH_CHECK), "--base-url", base, "--storage-state", str(state)],
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
    assert report["errors"] == [], report["errors"]
    assert report["violations"] == [], report["violations"]
    assert done.returncode == 0
