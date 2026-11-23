"""The knowledge map in a browser, on the CSP build of Alpine (T-UI-03, oberfl 3.3).

Serves the manager on 127.0.0.1 with the map of tests/test_knowledge_map.py (the map server
is the fake of that file) and runs tests/knowledge_map_browser.mjs in Chromium, in the light
and the dark theme: no report of the report-only policy, no refused Alpine expression and no
script error; the map renders, the view-as filter and the entry search work, a node opens its
detail by click and by keyboard, and every tab whose expressions were rewritten renders. Then
tests/csp_browser.mjs measures every Alpine expression of the page against the shipped CSP
build. Needs the browser tools of scripts/design-tools.sh.
"""

from __future__ import annotations

import base64
import json
import shutil
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml
from itsdangerous import TimestampSigner

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))

from test_knowledge_map import (  # noqa: E402
    _CSRF,
    _TOKEN,
    CALENDAR,
    WIKI,
    FakeMapServer,
)

from app import main as app_main  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core import knowledge_map as km  # noqa: E402
from app.core.jobs import JobQueue  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers.knowledge_map_deps import get_knowledge_map_transport  # noqa: E402

NODE = shutil.which("node")
TOOLS = REPO / ".pk" / "node_modules" / "playwright-core" / "index.mjs"
ALPINE_CSP = REPO / "src" / "app" / "static" / "alpine-csp.min.js"
SCRIPT = REPO / "tests" / "knowledge_map_browser.mjs"
CHECK = REPO / "tests" / "csp_browser.mjs"

pytestmark = pytest.mark.skipif(
    NODE is None or not TOOLS.is_file() or not ALPINE_CSP.is_file(),
    reason="browser tools not installed (scripts/design-tools.sh)",
)

# A place without a tool next to the wiki and the undescribed calendar of the page tests, so
# every lane but the agents' has a box and the wiki's RAG path is hidden from plain users.
BINDER = {
    "id": "place.binder",
    "kind": "reference",
    "system": "Quality binder",
    "coverage": "Signed audit reports",
    "location": "Office 2, shelf 3",
    "topics": [{"id": "org.processes", "label": "Processes"}],
    "provenance": {"declared_by": "operator"},
}


def _world(root: Path) -> Path:
    config = root / "config"
    (config / "knowledge-map" / "manifests").mkdir(parents=True)
    (config / "ai" / "knowledge-map").mkdir(parents=True)
    (config / ".env").write_text("COMPOSE_PROFILES=librechat,knowledge-map\n", encoding="utf-8")
    (config / km.MODULE_ENV).write_text(f"KM_ADMIN_TOKEN={_TOKEN}\n", encoding="utf-8")
    for entry in (WIKI, CALENDAR, BINDER):
        (config / "knowledge-map" / "manifests" / f"{entry['id']}.yaml").write_text(
            yaml.safe_dump(entry), encoding="utf-8"
        )
    (config / "knowledge-map" / "lints.yaml").write_text(
        yaml.safe_dump(
            {
                "generated_at": "2026-11-14T10:00:00+00:00",
                "findings": [
                    {
                        "severity": "high",
                        "code": "undeclared_server",
                        "target": "mcp.calendar",
                        "message": "Calendar describes nothing",
                    }
                ],
                "warnings": [],
            }
        ),
        encoding="utf-8",
    )
    return config


def _cookie(secret: str) -> str:
    session = {
        "user": {
            "sub": "u-1",
            "preferred_username": "admin",
            "roles": ["admin", "user"],
            "exp": int(time.time()) + 3600,
        },
        "_csrf_token": _CSRF,
    }
    payload = base64.b64encode(json.dumps(session).encode())
    return TimestampSigner(secret).sign(payload).decode()


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    import uvicorn

    root = tmp_path_factory.mktemp("km-browser")
    config = _world(root)
    get_settings.cache_clear()
    settings = get_settings().model_copy(update={"papaia_config_dir": str(config)})
    previous_queue = app_main._job_queue  # noqa: SLF001
    app_main._job_queue = JobQueue(config_dir=str(config))  # noqa: SLF001
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    fake = FakeMapServer()
    app.dependency_overrides[get_knowledge_map_transport] = lambda: httpx.MockTransport(
        fake.handler
    )
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_level="warning"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    for _ in range(200):
        if server.started:
            break
        time.sleep(0.05)
    assert server.started, "the manager did not start"
    state = root / "storage-state.json"
    state.write_text(
        json.dumps(
            {
                "cookies": [
                    {
                        "name": "papaia_manager_session",
                        "value": _cookie(settings.manager_session_secret),
                        "domain": "127.0.0.1",
                        "path": "/",
                        "expires": -1,
                        "httpOnly": True,
                        "secure": False,
                        "sameSite": "Lax",
                    }
                ],
                "origins": [],
            }
        ),
        encoding="utf-8",
    )
    try:
        yield f"http://127.0.0.1:{port}", state
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
        app_main._job_queue = previous_queue  # noqa: SLF001
        get_settings.cache_clear()


def test_the_page_works_in_both_themes_without_a_report(
    served: tuple[str, Path], tmp_path: Path
) -> None:
    assert NODE is not None
    base_url, state = served
    report = tmp_path / "report.json"
    done = subprocess.run(
        [
            NODE,
            str(SCRIPT),
            "--base-url",
            base_url,
            "--storage-state",
            str(state),
            "--report",
            str(report),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    results = json.loads(report.read_text(encoding="utf-8"))["results"]
    assert [r["theme"] for r in results] == ["light", "dark"]
    for result in results:
        assert result["converted"] is True
        assert (
            result["alpine_version"]
            == json.loads((REPO / "tests" / "csp_baseline.json").read_text(encoding="utf-8"))[
                "alpine"
            ]["version"]
        )
        assert result["violations"] == []
        assert result["alpine"] == []
        assert result["errors"] == []
        assert result["failed"] == [], result["failed"]
        # Topics, sources, access paths: the wiki, the binder and both of the wiki's paths.
        assert result["nodes"] >= 6  # noqa: PLR2004
        assert result["passed"] == result["steps"]


def test_every_expression_of_the_page_evaluates_on_the_csp_build(
    served: tuple[str, Path], tmp_path: Path
) -> None:
    assert NODE is not None
    base_url, state = served
    report = tmp_path / "report.json"
    done = subprocess.run(
        [
            NODE,
            str(CHECK),
            "--base-url",
            base_url,
            "--page",
            "/knowledge-map",
            "--storage-state",
            str(state),
            "--candidate",
            f"csp={ALPINE_CSP}",
            "--report",
            str(report),
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    (result,) = json.loads(report.read_text(encoding="utf-8"))["results"]
    assert result["converted"] is True
    assert result["expressions"], "no Alpine expression found on the page"
    assert result["candidates"]["csp"] == []
