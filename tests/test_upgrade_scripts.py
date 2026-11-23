"""The upgrade pages of the manager on the target policy (T-UI-03).

oberfl 3.3, P4. The release upgrade (`upgrade.html` with the partials htmx swaps into it:
`partials/upgrade_status.html`, `partials/upgrade_runner.html`, `partials/upgrade_check.html`
and `partials/upgrade_images.html`) and the add-on upgrade (`addon_upgrade.html`) carry no
executable inline script, no `on*` attribute and no Alpine expression the CSP build refuses.
Their scripts are the page modules `static/js/pages/upgrade.js` and
`static/js/pages/addon-upgrade.js`; values from Jinja reach them through data islands and
`data-*` attributes only. Both pages are listed as converted and run on the CSP build of
Alpine.

The browser part serves the manager in the stand-in world (tests/design_serve.py): the policy
check of MGR-003 (tests/csp_browser.mjs) over both pages, and the paths of the group
(tests/upgrade_browser.mjs) in light and dark: check for upgrades, start and follow an
upgrade, dismiss it, remove outdated images, and review and apply an add-on upgrade. The
partials the stand-in world cannot fill (it has no git checkout and no Docker) are rendered
here from the real templates and handed to the browser. It needs the tools of
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

import csp_scan  # noqa: E402
import design_world  # noqa: E402

_WORLD = Path(tempfile.mkdtemp(prefix="papaia-upgrade-scripts-"))
_CONFIG, _WORKSPACE = _WORLD / "config", _WORLD / "workspace"
_CONFIG.mkdir()
_WORKSPACE.mkdir()
for _key, _value in design_world.environment(
    _CONFIG, _WORKSPACE, host="http://localhost:8120", secret="test-session-secret-value"
).items():
    os.environ.setdefault(_key, _value)

from fastapi.testclient import TestClient  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core import images, module_catalog, runner  # noqa: E402
from app.core.upgrade import (  # noqa: E402
    CheckoutState,
    Gate,
    Migration,
    Phase,
    UpgradeCheck,
    VersionState,
)
from app.main import create_app  # noqa: E402
from app.templating import templates  # noqa: E402

TEMPLATES = SRC / "app" / "templates"
JS = SRC / "app" / "static" / "js"
# The names core/legacy.js put on `window` during the conversion; it is gone (T-UI-03), and no
# page reaches them as globals any more.
TRANSITION_NAMES = ("csrfToken", "fmt", "fmtPlural", "showToast")
PAGES_JS = JS / "pages"
STRICT = '<meta name="papaia-csp" content="strict">'
NODE = shutil.which("node")
CSP_CHECK = REPO / "tests" / "csp_browser.mjs"
PATH_CHECK = REPO / "tests" / "upgrade_browser.mjs"
TOOLS = REPO / ".pk" / "node_modules" / "playwright-core" / "index.mjs"
ALPINE_CSP = SRC / "app" / "static" / "alpine-csp.min.js"
ADDON = design_world.SAMPLES["name"]

# The templates of this group: the two pages and the partials the release upgrade swaps in.
GROUP = (
    "upgrade.html",
    "addon_upgrade.html",
    "partials/upgrade_status.html",
    "partials/upgrade_runner.html",
    "partials/upgrade_check.html",
    "partials/upgrade_images.html",
)
# Page path, top-level template, page module.
PAGES = {
    "/upgrade": ("upgrade.html", "js/pages/upgrade.js"),
    f"/addons/{ADDON}/upgrade": ("addon_upgrade.html", "js/pages/addon-upgrade.js"),
}


# -- fixtures ------------------------------------------------------------------------------


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    config, workspace = design_world.make_world(tmp_path)
    get_settings.cache_clear()
    module_catalog.reset_readers()
    settings = get_settings().model_copy(
        update={"papaia_config_dir": str(config), "papaia_workspace_dir": str(workspace)}
    )

    async def idle(kind: object = None) -> None:
        return None

    monkeypatch.setattr(runner, "find_runner", idle)
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


# -- rendered partials ---------------------------------------------------------------------


def _check(**changes: Any) -> UpgradeCheck:
    values: dict[str, Any] = {
        "current": "1.1.0",
        "target": "1.2.0",
        "tag": "v1.2.0",
        "status": "ok",
        "available": ["1.3.0", "1.2.0"],
        "migrations": [Migration(id="1.2.0__move_settings", version="1.2.0", kind="py")],
        "gate": Gate(passed=True),
        "checked_at": "2026-11-23T10:00:00+00:00",
    }
    values.update(changes)
    return UpgradeCheck(**values)


def _status_partial(check: UpgradeCheck | None, *, backup: bool = True) -> str:
    return templates.env.get_template("partials/upgrade_status.html").render(
        version=VersionState(recorded="1.1.0", checkout="1.1.0"),
        checkout=CheckoutState(is_git=True, clean=True, head="abc1234", tag="v1.1.0"),
        backup_dir="/srv/backups" if backup else None,
        backup_dir_reachable=backup,
        check=check,
    )


def _runner_status(*, running: bool) -> runner.RunnerStatus:
    return runner.RunnerStatus(
        name="papaia-upgrade-1.2.0",
        target="1.2.0",
        status="running" if running else "exited",
        exit_code=None if running else 0,
        started_at="2026-11-23T10:00:00Z",
        finished_at="",
        prune_images=True,
    )


def _runner_partial(status: runner.RunnerStatus | None) -> str:
    phases = [
        Phase(key="backup", label="Write a restore point", state="done"),
        Phase(
            key="checkout",
            label="Move the checkout",
            state="running" if status and status.is_running else "done",
        ),
    ]
    return templates.env.get_template("partials/upgrade_runner.html").render(
        upgrade_runner=status,
        upgrade_log="[upgrade] moving the checkout" if status else "",
        upgrade_error="",
        phases=phases if status else [],
        recovery="",
        recovery_generated=False,
        provisioning=[],
        node_progress=[],
    )


def _images_partial() -> str:
    outdated = [
        images.OutdatedImage(
            id="sha256:" + "a" * 64,
            name="postgres:15",
            remove=("postgres:15",),
            size=650_000_000,
            created="2026-01-01T00:00:00Z",
            source="core",
        ),
        images.OutdatedImage(
            id="sha256:" + "b" * 64,
            name="redis:6",
            remove=("redis:6",),
            size=120_000_000,
            created="2026-02-01T00:00:00Z",
            source="core",
        ),
    ]
    return templates.env.get_template("partials/upgrade_images.html").render(
        report=images.ImageReport(outdated=outdated)
    )


# -- the templates -------------------------------------------------------------------------


@pytest.mark.parametrize("name", GROUP)
def test_the_templates_of_the_group_have_nothing_the_policy_refuses(name: str) -> None:
    # Style attributes included: a converted page has no row in the baseline at all.
    counts = csp_scan.count_markup((TEMPLATES / name).read_text(encoding="utf-8"))
    assert counts == dict.fromkeys(csp_scan.KINDS, 0)


def test_x_cloak_hides_what_alpine_shows_until_it_starts() -> None:
    css = (REPO / "docker" / "tailwind.app.css").read_text(encoding="utf-8")
    assert re.search(r"\[x-cloak\]\s*\{\s*display:\s*none\s*!important;?\s*\}", css)


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
    # After the shared modules, and before Alpine so its components are registered in time.
    assert sources.index("/static/js/core/main.js") < sources.index(module)
    assert sources.index(module) < sources.index("/static/alpine-csp.min.js")


def test_the_upgrade_page_hands_its_texts_over_in_an_island(client: TestClient) -> None:
    body = client.get("/upgrade").text
    texts = json.loads(_markup(body).islands["mgr-upgrade"])["texts"]
    assert texts["upgradeTitle"] == "Upgrade to {target}?"
    assert texts["upgradeIntro"].startswith('papAIa moves from <span class="font-mono">{current}')
    checkout = 'The checkout moves to <span class="font-mono">{target}</span>'
    assert texts["upgradeCheckout"] == checkout
    assert "docker logs papaia-upgrade-{target}" in texts["upgradeLogs"]
    for key in (
        "install",
        "dismissFirst",
        "checkFailed",
        "upToDate",
        "checkFailedBecause",
        "checkUnreachable",
        "refused",
        "started",
        "dismissFailed",
        "dismissedImagesFailed",
        "dismissedImagesOne",
        "dismissedImagesOther",
        "removeSelectedCount",
        "removeSelected",
        "removeTitleOne",
        "removeTitleOther",
        "removeFailed",
        "removedPartly",
        "removedOne",
        "removedOther",
    ):
        assert key in texts, key
    # The header cluster is one component; the hooks of the page stay where they were.
    assert 'x-data="upgradeActions"' in body
    assert '@papaia-upgrade-state.window="applyState($event.detail)"' in body
    assert '@papaia-upgrade-runner.window="applyRunner($event.detail)"' in body
    for hook in (
        'id="prune-images-modal"',
        'id="prune-images-title"',
        'id="prune-images-list"',
        'id="upgrade-runner-slot"',
        'id="upgrade-status-slot"',
        'id="upgrade-check-slot"',
        'id="upgrade-images-slot"',
        'id="upgrade-log-slot"',
        'x-model="pruneImages"',
        'x-ref="upgradeModal"',
        'data-upgrade-action="prune-cancel"',
        'data-upgrade-action="prune-confirm"',
    ):
        assert hook in body, hook
    # The sentences with markup are built as DOM nodes, never through x-html.
    for key in ("upgradeTitle", "upgradeIntro", "upgradeCheckout", "upgradeLogs"):
        assert f'data-sentence="{key}"' in body, key
    assert "x-html" not in body


def test_the_add_on_upgrade_hands_its_values_over_in_an_island(client: TestClient) -> None:
    body = client.get(f"/addons/{ADDON}/upgrade").text
    page = json.loads(_markup(body).islands["mgr-addon-upgrade"])
    assert page["name"] == ADDON
    assert isinstance(page["supported"], bool)
    assert page["texts"] == {"upgradeTo": "Upgrade to {version}"}
    assert 'x-data="addonUpgrade"' in body
    assert 'x-init="load()"' in body
    assert 'x-text="applyLabel"' in body


def test_the_status_partial_announces_its_state_in_an_island() -> None:
    html = _status_partial(_check(gate=Gate(passed=True)))
    markup = _markup(html)
    assert markup.inline_bodies == []
    state = json.loads(markup.islands["upgrade-state"])
    assert state == {
        "target": "1.2.0",
        "ready": True,
        "checkFailed": False,
        "forceable": False,
        "gatePassed": True,
        "hasBackupDir": True,
        "available": ["1.3.0", "1.2.0"],
        "current": "1.1.0",
    }
    nothing = json.loads(_markup(_status_partial(None, backup=False)).islands["upgrade-state"])
    assert nothing["target"] is None and nothing["ready"] is False
    assert nothing["hasBackupDir"] is False and nothing["available"] == []


def test_the_runner_partial_announces_its_state_in_data_attributes() -> None:
    for status, expected in (
        (None, ("false", "false", "false")),
        (_runner_status(running=True), ("true", "true", "false")),
        (_runner_status(running=False), ("true", "false", "true")),
    ):
        html = _runner_partial(status)
        markup = _markup(html)
        assert markup.inline_bodies == [] and markup.handlers == []
        active, running, finished = expected
        assert f'data-active="{active}"' in html
        assert f'data-running="{running}"' in html
        assert f'data-finished="{finished}"' in html
    finished = _runner_partial(_runner_status(running=False))
    assert 'data-upgrade-action="dismiss"' in finished
    assert 'data-upgrade-action="reload"' in finished


def test_the_check_and_images_partials_bind_through_data_attributes() -> None:
    check = templates.env.get_template("partials/upgrade_check.html").render(check=_check())
    assert _markup(check).handlers == []
    assert 'id="upgrade-version"' in check
    shown = _images_partial()
    assert _markup(shown).handlers == []
    for hook in (
        'data-upgrade-action="refresh-images"',
        'data-upgrade-action="prune-selected"',
        'data-upgrade-action="prune-all"',
        'id="prune-master-check"',
        'id="prune-selected-button"',
        'class="checkbox checkbox-sm shrink-0 prune-image-check"',
    ):
        assert hook in shown, hook


# -- the page modules ----------------------------------------------------------------------

_IMPORT = re.compile(r'^import\s+\{([^}]*)\}\s+from\s+"([^"]+)";', re.MULTILINE)
_MODULES = (
    "upgrade.js",
    "upgrade/component.js",
    "addon-upgrade.js",
    "addon-upgrade/component.js",
)


@pytest.mark.parametrize("module", _MODULES)
def test_the_page_modules_import_what_they_use(module: str) -> None:
    source = (PAGES_JS / module).read_text(encoding="utf-8")
    imports = _IMPORT.findall(source)
    for _, target in imports:
        assert target.startswith(("../core/", "../../core/", "./")), target
    # No former transitional global: the names are imports.
    names = TRANSITION_NAMES
    imported = {n.strip().split(" as ")[-1] for names_, _ in imports for n in names_.split(",")}
    body = _IMPORT.sub("", source)
    for name in names:
        if re.search(r"(?<![\w$.])" + re.escape(name) + r"(?![\w$])", body):
            assert name in imported, f"{module} uses the global {name}"


@pytest.mark.parametrize(
    ("entry", "component"), [("upgrade.js", "upgradeActions"), ("addon-upgrade.js", "addonUpgrade")]
)
def test_the_entries_register_their_component_on_alpine_init(entry: str, component: str) -> None:
    source = (PAGES_JS / entry).read_text(encoding="utf-8")
    assert 'addEventListener("alpine:init"' in source
    assert f'Alpine.data("{component}"' in source


def _expressions(template: str) -> list[str]:
    source = (TEMPLATES / template).read_text(encoding="utf-8")
    source = re.sub(r"\{#.*?#\}", "", source, flags=re.DOTALL)
    return re.findall(r'\s(?:x-[\w:.-]+|[@:][\w:.-]+)="([^"]*)"', source)


@pytest.mark.parametrize(
    ("template", "module", "names"),
    [
        (
            "upgrade.html",
            "upgrade/component.js",
            (
                "applyState",
                "applyRunner",
                "check",
                "open",
                "run",
                "fill",
                "installLabel",
                "blocked",
            ),
        ),
        (
            "addon_upgrade.html",
            "addon-upgrade/component.js",
            (
                "load",
                "apply",
                "show",
                "applyLabel",
                "canApply",
                "missing",
                "hasManifestChanges",
                "apiChanged",
                "hasManualSteps",
            ),
        ),
    ],
)
def test_every_method_an_expression_calls_is_a_member_of_the_component(
    template: str, module: str, names: tuple[str, ...]
) -> None:
    component = (PAGES_JS / module).read_text(encoding="utf-8")
    expressions = " ".join(_expressions(template))
    for name in names:
        assert re.search(r"(?<![\w$.])" + name + r"(?![\w$])", expressions), name
        assert re.search(r"(?:^|\s)(?:get\s+|async\s+)?" + name + r"\s*[(:]", component, re.M), name


# -- browser check -------------------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    """The manager of the stand-in world on 127.0.0.1, with Anna's storage state."""
    folder = tmp_path_factory.mktemp("upgrade-browser")
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
    args += ["--candidate", f"csp={ALPINE_CSP}"]
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


def _fixtures(folder: Path) -> Path:
    """The partials of an installation with a release on offer, as the templates render them."""
    rendered = {
        "status.html": _status_partial(_check()),
        "check.html": templates.env.get_template("partials/upgrade_check.html").render(
            check=_check()
        ),
        "runner-none.html": _runner_partial(None),
        "runner-running.html": _runner_partial(_runner_status(running=True)),
        "runner-done.html": _runner_partial(_runner_status(running=False)),
        "images.html": _images_partial(),
    }
    for name, html in rendered.items():
        (folder / name).write_text(html, encoding="utf-8")
    return folder


@_browser
def test_the_paths_of_the_group_work_in_a_browser(served: tuple[str, Path], tmp_path: Path) -> None:
    base, state = served
    fixtures = _fixtures(tmp_path)
    shots = os.environ.get("PAPAIA_UPGRADE_SHOTS", "")
    assert NODE is not None
    argv = [
        NODE,
        str(PATH_CHECK),
        "--base-url",
        base,
        "--storage-state",
        str(state),
        "--fixtures",
        str(fixtures),
        "--addon",
        ADDON,
    ]
    if shots:
        argv += ["--shots", shots]
    done = subprocess.run(  # noqa: S603 -- fixed argv
        argv,
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
    assert report["alpine"] == [], report["alpine"]
    assert done.returncode == 0
