"""The settings pages of the manager and of the modules on the target policy (T-UI-03).

oberfl 3.3, P4. The Manager tab (`settings.html` with the branding and host partials) and the
Modules tab (`settings_modules.html` with the job strip `partials/config_job_status.html`,
which htmx swaps in) carry no executable inline script, no `on*` attribute and no Alpine
expression the CSP build refuses. Their scripts are the page modules
`static/js/pages/settings.js` and `static/js/pages/settings-modules.js`; values from Jinja
reach them through data islands and `data-*` attributes only. Both pages are listed as
converted and run on the CSP build of Alpine.

The browser part serves the manager in the stand-in world (tests/design_serve.py): the
policy check of MGR-003 (tests/csp_browser.mjs) over both pages, and the paths of the group
(tests/settings_browser.mjs) in light and dark. It needs the tools of scripts/design-tools.sh.
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

_WORLD = Path(tempfile.mkdtemp(prefix="papaia-settings-scripts-"))
_CONFIG, _WORKSPACE = _WORLD / "config", _WORLD / "workspace"
_CONFIG.mkdir()
_WORKSPACE.mkdir()
for _key, _value in design_world.environment(
    _CONFIG, _WORKSPACE, host="http://localhost:8120", secret="test-session-secret-value"
).items():
    os.environ.setdefault(_key, _value)

from fastapi.testclient import TestClient  # noqa: E402

from app import csp  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core import module_catalog  # noqa: E402
from app.main import create_app  # noqa: E402

TEMPLATES = SRC / "app" / "templates"
PAGES_JS = SRC / "app" / "static" / "js" / "pages"
BASELINE = REPO / "tests" / "csp_baseline.json"
STRICT = '<meta name="papaia-csp" content="strict">'
NODE = shutil.which("node")
CSP_CHECK = REPO / "tests" / "csp_browser.mjs"
PATH_CHECK = REPO / "tests" / "settings_browser.mjs"
TOOLS = REPO / ".pk" / "node_modules" / "playwright-core" / "index.mjs"
ALPINE_CSP = SRC / "app" / "static" / "alpine-csp.min.js"

# The templates of this group and the partial the Modules tab swaps in.
GROUP = (
    "settings.html",
    "settings_modules.html",
    "partials/_settings_host.html",
    "partials/_settings_branding.html",
    "partials/config_job_status.html",
)
# Page path, top-level template, page module.
PAGES = {
    "/settings": ("settings.html", "js/pages/settings.js"),
    "/settings/modules": ("settings_modules.html", "js/pages/settings-modules.js"),
}


# -- fixtures ------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
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
        design_world.session_cookie(get_settings().manager_session_secret),
    )
    yield test_client
    get_settings.cache_clear()
    module_catalog.reset_readers()


class _Markup(HTMLParser):
    """Scripts, data islands and inline event handlers of a page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[dict[str, str | None]] = []
        self.handlers: list[str] = []
        self.islands: dict[str, str] = {}
        self.inline_bodies: list[str] = []
        self._script: dict[str, str | None] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handlers += [name for name, _ in attrs if name.startswith("on")]
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


def _path(src: str | None) -> str:
    return str(src).split("?", 1)[0]


# -- the templates -------------------------------------------------------------------------


@pytest.mark.parametrize("name", GROUP)
def test_the_templates_of_the_group_have_nothing_the_policy_refuses(name: str) -> None:
    counts = csp_scan.count_markup((TEMPLATES / name).read_text(encoding="utf-8"))
    refused = {k: counts[k] for k in ("inline_scripts", "on_attributes", "alpine_unsupported")}
    assert refused == dict.fromkeys(refused, 0)


def test_the_group_has_no_row_in_the_baseline_and_its_pages_are_converted() -> None:
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    for name in GROUP:
        assert name not in baseline["templates"], name
    for template, _ in PAGES.values():
        assert template in baseline["converted"]
        assert template in csp.converted_templates()


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
    # After the shared modules, so the shared page script is in place.
    assert sources.index(module) > sources.index("/static/js/core/main.js")


def test_the_manager_tab_hands_its_values_over_in_islands(client: TestClient) -> None:
    body = client.get("/settings").text
    islands = _markup(body).islands
    page = json.loads(islands["mgr-settings"])
    revision = client.get("/api/v1/settings").json()["revision"]
    assert page["revision"] == revision
    assert set(page["texts"]) == {"requestFailed"}
    host = json.loads(islands["mgr-settings-host"])
    assert host["min"] == 10 and host["default"] == 60 and host["max"] % 60 == 0
    assert set(host["texts"]) == {"wholeNumber", "range", "saved"}
    branding = json.loads(islands["mgr-settings-branding"])
    assert set(branding["texts"]) == {"saved", "reset", "logoUpdated", "logoRemoved"}
    # The buttons the module binds, with the hooks they had.
    for hook in (
        'id="branding-form"',
        'id="host-form"',
        'id="reset-branding-confirm"',
        'id="logo-file"',
        'id="reset-branding-modal"',
    ):
        assert hook in body, hook


def test_the_modules_tab_hands_its_values_over_in_an_island(client: TestClient) -> None:
    body = client.get("/settings/modules").text
    page = json.loads(_markup(body).islands["mgr-settings-modules"])
    listed = client.get("/api/v1/config/modules").json()
    assert page["revision"] == listed["revision"]
    assert [r["name"] for r in page["rows"]] == [m["name"] for m in listed["modules"]]
    assert page["defaultPorts"] == {"starttls": 587, "tls": 465, "none": 25}
    assert set(page["offNotes"]) == {"rag", "mail", "knowledge-map"}
    for key in ("offTitle", "confirmDeletion", "switchingOn", "switchingOff", "done", "failed"):
        assert key in page["texts"], key
    # Every switchable toggle is bound by the module; the locked ones stay disabled.
    toggles = re.findall(r'<input type="checkbox"[^>]*data-module="([^"]+)"[^>]*>', body)
    assert "manager" in toggles


def test_the_job_strip_announces_its_state_without_a_script(client: TestClient) -> None:
    body = client.get("/partials/config/job-status").text
    assert "<script" not in body
    assert 'data-announce="papaia-config-job"' in body
    assert 'data-active="false"' in body


# -- the page modules ----------------------------------------------------------------------

_IMPORT = re.compile(r'^import\s+\{([^}]*)\}\s+from\s+"([^"]+)";', re.MULTILINE)


@pytest.mark.parametrize("module", ["settings.js", "settings-modules.js"])
def test_the_page_modules_import_what_they_use(module: str) -> None:
    source = (PAGES_JS / module).read_text(encoding="utf-8")
    imports = _IMPORT.findall(source)
    assert imports, module
    for _, target in imports:
        assert target.startswith("../core/"), target
    # No transitional global of core/legacy.js: the names are imports.
    legacy = (SRC / "app" / "static" / "js" / "core" / "legacy.js").read_text(encoding="utf-8")
    block = legacy[legacy.index("LEGACY_NAMES") :]
    names = re.findall(r'"([^"]+)",', block[: block.index("]);")])
    imported = {n.strip().split(" as ")[-1] for names_, _ in imports for n in names_.split(",")}
    body = _IMPORT.sub("", source)
    for name in names:
        if re.search(r"(?<![\w$.])" + re.escape(name) + r"(?![\w$])", body):
            assert name in imported, f"{module} uses the global {name}"


# -- browser check -------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    """The manager of the stand-in world on 127.0.0.1, with Anna's storage state."""
    folder = tmp_path_factory.mktemp("settings-browser")
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
    assert sorted(r["page"] for r in results) == sorted(PAGES)
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
