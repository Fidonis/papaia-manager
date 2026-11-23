"""Hostile values under the enforced policy, in a browser (tests/csp_hostile_browser.mjs).

Serves the manager with the stand-in world of tests/rag_serve.py --hostile (127.0.0.1, signed-in
test administrator whose shown name is design_world.HOSTILE; an account, a collection and a
notification recipient carry the same value) and opens the account list, the collections and
the notification recipients in Chromium, in the light and the dark colour scheme. The value is
markup and script at once; every place shows it as text, no dialog opens, the policy reports
nothing and every page answers with the enforced policy. The dashboard of that world holds tiles
whose icon is on another site (design_world.OFFSITE_ICON); the dashboard, the tile editor and
its dialog show the bundled icon or the badge instead, so the policy reports nothing there
either. Needs the browser tools of
scripts/design-tools.sh; the CI job `csp` installs them.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))

import design_world  # noqa: E402

CHECK = REPO / "tests" / "csp_hostile_browser.mjs"
NODE = shutil.which("node")
TOOLS = REPO / ".pk" / "node_modules" / "playwright-core" / "index.mjs"

pytestmark = pytest.mark.skipif(
    NODE is None or not TOOLS.is_file(),
    reason="browser tools not installed (scripts/design-tools.sh)",
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@pytest.fixture(scope="module")
def manager(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    work = tmp_path_factory.mktemp("csp-hostile")
    state, log = work / "storage-state.json", work / "manager.log"
    port = _free_port()
    with log.open("w", encoding="utf-8") as out:
        server = subprocess.Popen(  # noqa: S603 -- fixed argv, no shell
            [
                sys.executable,
                str(REPO / "tests" / "rag_serve.py"),
                "--port",
                str(port),
                "--storage-state",
                str(state),
                "--hostile",
            ],
            cwd=REPO / "src",
            stdout=out,
            stderr=subprocess.STDOUT,
        )
        base = f"http://127.0.0.1:{port}"
        try:
            for _ in range(200):
                if server.poll() is not None:
                    pytest.fail("the manager did not start:\n" + log.read_text(encoding="utf-8"))
                try:
                    with urllib.request.urlopen(base + "/health", timeout=1):  # noqa: S310
                        if state.is_file() and state.stat().st_size:
                            break
                except OSError:
                    pass
                time.sleep(0.1)
            else:
                pytest.fail("the manager does not answer")
            yield base, state
        finally:
            server.terminate()
            server.wait(timeout=10)


def test_the_hostile_value_is_markup_and_script() -> None:
    assert "<script>" in design_world.HOSTILE
    assert "onerror=" in design_world.HOSTILE
    # It fits the name of a notification recipient.
    assert len(design_world.HOSTILE) <= 60  # noqa: PLR2004


def test_hostile_values_are_shown_as_text_and_never_run(manager: tuple[str, Path]) -> None:
    base, state = manager
    assert NODE is not None
    done = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [
            NODE,
            str(CHECK),
            "--base-url",
            base,
            "--storage-state",
            str(state),
            "--value",
            design_world.HOSTILE,
            "--icon",
            design_world.OFFSITE_ICON,
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
    assert done.returncode == 0
    names = {c["name"] for c in report["checks"]}
    for scheme in ("light", "dark"):
        for name in (
            "users: the page answers with the enforced policy",
            "display name: the frame shows the signed-in name as text",
            "display name: the account list shows the name as text",
            "collection name: the list shows the name as text",
            "notification: the recipient list shows the name as text",
            "notification: the toast shows name and reason as text",
            "dashboard: the page answers with the enforced policy",
            "off-site icon: the dashboard shows no image from another site",
            "off-site icon: the tiles show the badge and the bundled icon instead",
            "off-site icon: the editor shows no image from another site",
            "off-site icon: the tile dialog names the problem and shows no image from another site",
            "no dialog opened",
            "no report of the policy",
            "no script error",
        ):
            assert f"{scheme}: {name}" in names


def test_the_off_site_icon_is_an_address_on_another_site() -> None:
    assert design_world.OFFSITE_ICON.startswith("https://")
    assert "127.0.0.1" not in design_world.OFFSITE_ICON


def test_the_check_refuses_a_page_off_the_loopback() -> None:
    assert NODE is not None
    done = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [
            NODE,
            str(CHECK),
            "--base-url",
            "http://example.test",
            "--storage-state",
            "x",
            "--value",
            "x",
            "--icon",
            "x",
        ],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert done.returncode == 2  # noqa: PLR2004
    assert "loopback only" in done.stderr
