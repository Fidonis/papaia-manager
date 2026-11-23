"""The kit's specimen page `/_kit/specimen` as the first page on the target policy.

oberfl 3.5, T-UI-05, T-UI-07, PLT-83 (A5: meta `papaia-kit` of the vendored version). The page
is signed-in only, rendered from the vendored kit inside the shell, and it is converted from
the start (tests/csp_baseline.json): it carries the strict marker, runs the CSP build of
Alpine and nothing on it needs `unsafe-inline` or `unsafe-eval`. The browser part serves the
manager in the stand-in world (tests/design_serve.py) and opens the page with the browser
check of the policy (tests/csp_browser.mjs); it needs the tools of scripts/design-tools.sh,
which the CI job `csp` installs.
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
import tomllib
import urllib.error
import urllib.request
from collections.abc import Iterator
from html.parser import HTMLParser
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
sys.path.insert(0, str(REPO / "tests"))

import design_world  # noqa: E402

_WORLD = Path(tempfile.mkdtemp(prefix="papaia-kit-specimen-"))
_CONFIG, _WORKSPACE = _WORLD / "config", _WORLD / "workspace"
_CONFIG.mkdir()
_WORKSPACE.mkdir()
for _key, _value in design_world.environment(
    _CONFIG, _WORKSPACE, host="http://localhost:8120", secret="test-session-secret-value"
).items():
    os.environ.setdefault(_key, _value)

from fastapi.testclient import TestClient  # noqa: E402
from papaia_kit.ui.specimen import check_specimen_markup, load_specimen  # noqa: E402

from app import csp  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core import module_catalog  # noqa: E402
from app.main import create_app  # noqa: E402

PATH = "/_kit/specimen"
TEMPLATE = "kit_specimen.html"
STRICT = '<meta name="papaia-csp" content="strict">'
NODE = shutil.which("node")
CHECK = REPO / "tests" / "csp_browser.mjs"
TOOLS = REPO / ".pk" / "node_modules" / "playwright-core" / "index.mjs"
ALPINE_CSP = SRC / "app" / "static" / "alpine-csp.min.js"


def _locked_version() -> str:
    lock = tomllib.loads((SRC / "kit.lock").read_text(encoding="utf-8"))
    version: str = lock["version"]
    return version


# -- fixtures ------------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path: Path) -> tuple[Path, Path]:
    return design_world.make_world(tmp_path)


def _app(world: tuple[Path, Path]) -> TestClient:
    config, workspace = world
    get_settings.cache_clear()
    module_catalog.reset_readers()
    settings = get_settings().model_copy(
        update={"papaia_config_dir": str(config), "papaia_workspace_dir": str(workspace)}
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    design_world.stand_ins(app)
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def anonymous(world: tuple[Path, Path]) -> Iterator[TestClient]:
    yield _app(world)
    get_settings.cache_clear()
    module_catalog.reset_readers()


@pytest.fixture
def client(world: tuple[Path, Path]) -> Iterator[TestClient]:
    test_client = _app(world)
    test_client.cookies.set(
        design_world.SESSION_COOKIE,
        design_world.session_cookie(get_settings().manager_session_secret),
    )
    yield test_client
    get_settings.cache_clear()
    module_catalog.reset_readers()


class _Markup(HTMLParser):
    """Scripts, inline event handlers and style attributes of a page."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.scripts: list[dict[str, str | None]] = []
        self.handlers: list[str] = []
        self.styles = 0
        self._inline = False
        self.inline_bodies: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = dict(attrs)
        self.handlers += [name for name, _ in attrs if name.startswith("on")]
        self.styles += sum(1 for name, _ in attrs if name == "style")
        if tag == "script":
            self.scripts.append(names)
            self._inline = "src" not in names and names.get("type") != "application/json"

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._inline = False

    def handle_data(self, data: str) -> None:
        if self._inline and data.strip():
            self.inline_bodies.append(data.strip())


def _path(src: str | None) -> str:
    """The path of a script URL without the cache-busting query of `asset_url`."""
    return str(src).split("?", 1)[0]


def _markup(body: str) -> _Markup:
    parser = _Markup()
    parser.feed(body)
    parser.close()
    return parser


# -- access --------------------------------------------------------------------------------


def test_the_specimen_answers_a_signed_in_person(client: TestClient) -> None:
    response = client.get(PATH)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert check_specimen_markup(response.text, load_specimen()) == []


@pytest.mark.parametrize("headers", [{}, {"Accept": "text/html"}])
def test_the_specimen_sends_a_stranger_to_the_sign_in(
    anonymous: TestClient, headers: dict[str, str]
) -> None:
    response = anonymous.get(PATH, headers=headers)
    assert response.status_code == 307
    assert response.headers["location"] == "/auth/login?next=/_kit/specimen"
    assert "data-pk-specimen" not in response.text


def test_a_partial_request_without_a_session_gets_no_specimen(anonymous: TestClient) -> None:
    response = anonymous.get(PATH, headers={"HX-Request": "true"})
    assert response.status_code == 401
    assert "data-pk-specimen" not in response.text


# -- the vendored kit ----------------------------------------------------------------------


def test_the_kit_meta_element_names_the_version_of_kit_lock(client: TestClient) -> None:
    body = client.get(PATH).text
    found = re.findall(r'<meta name="papaia-kit" content="([^"]*)">', body)
    assert found == [_locked_version()]


def test_the_specimen_is_rendered_inside_the_shell(client: TestClient) -> None:
    body = client.get(PATH).text
    assert body.count('<main id="pk-main"') == 1
    zones = re.findall(r'data-pk-zone="([a-z-]+)"', body)
    assert zones == ["brand", "switcher", "nav", "footer", "page-header"]
    main = body[body.index('<main id="pk-main"') : body.index("</main>")]
    assert "data-pk-specimen" in main


# -- converted from the start (tests/csp_baseline.json, app/csp.py) ------------------------


def test_the_specimen_is_listed_as_converted() -> None:
    baseline = json.loads((REPO / "tests" / "csp_baseline.json").read_text(encoding="utf-8"))
    assert TEMPLATE in baseline["converted"]
    assert TEMPLATE in csp.converted_templates()


def test_the_specimen_carries_the_strict_marker_once(client: TestClient) -> None:
    body = client.get(PATH).text
    assert body.count(STRICT) == 1


def test_the_specimen_runs_the_csp_build_of_alpine_only(client: TestClient) -> None:
    scripts = _markup(client.get(PATH).text).scripts
    alpine = [_path(s["src"]) for s in scripts if "alpine" in str(s.get("src") or "")]
    assert alpine == ["/static/alpine-csp.min.js"]


def test_the_specimen_has_nothing_the_target_policy_refuses(client: TestClient) -> None:
    response = client.get(PATH)
    assert response.headers["content-security-policy-report-only"] == csp.POLICY
    markup = _markup(response.text)
    assert markup.inline_bodies == []
    assert markup.handlers == []
    assert markup.styles == 0
    assert "<style" not in response.text
    for script in markup.scripts:
        src = script.get("src")
        assert src is None or src.startswith("/"), src


def test_the_specimen_loads_no_other_manager_script_or_stylesheet(client: TestClient) -> None:
    """Only the kit styles the reference content the kit's conformance run measures."""
    body = client.get(PATH).text
    assert "app.css" not in body and "htmx" not in body
    sources = [s.get("src") for s in _markup(body).scripts]
    own = [_path(s) for s in sources if s and s.startswith("/static/")]
    assert own == ["/static/alpine-csp.min.js"]


# -- browser check -------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    """The manager of the stand-in world on 127.0.0.1, with Anna's storage state."""
    folder = tmp_path_factory.mktemp("specimen-browser")
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


@pytest.mark.skipif(
    NODE is None or not TOOLS.is_file() or not ALPINE_CSP.is_file(),
    reason="browser tools not installed (scripts/design-tools.sh)",
)
def test_the_browser_sees_no_report_and_no_alpine_error(
    served: tuple[str, Path], tmp_path: Path
) -> None:
    base, state = served
    report = tmp_path / "report.json"
    assert NODE is not None
    done = subprocess.run(  # noqa: S603 -- fixed argv
        [
            NODE,
            str(CHECK),
            "--base-url",
            base,
            "--storage-state",
            str(state),
            "--report",
            str(report),
            "--page",
            PATH,
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
    )
    assert done.returncode == 0, done.stderr
    (result,) = json.loads(report.read_text(encoding="utf-8"))["results"]
    assert result["status"] == 200
    assert result["converted"] is True
    assert result["violations"] == []
    assert result["alpine"] == []
    assert result["errors"] == []
    # Alpine started under the policy: the CSP build needs no `eval` to boot.
    assert isinstance(result["alpine_version"], str) and result["alpine_version"]
