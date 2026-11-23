"""The browser path of Settings -> Overlay and Mail -> Server (tests/settings_mail_browser.mjs).

Serves the manager in the stand-in world with the mail relay switched on (tests/design_serve.py
--mail-relay, 127.0.0.1, signed-in test administrator) and drives both pages on the CSP build
of Alpine in Chromium, in both themes: open the overlay, edit, check, preview and apply it and
follow the job; probe the mail server, send a test mail, save and follow the job; the job
strips announce their state. No policy report, refused Alpine expression or script error.
Needs the browser tools of scripts/design-tools.sh; the CI job `csp` installs them.
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
CHECK = REPO / "tests" / "settings_mail_browser.mjs"
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
def manager(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path, Path]]:
    work = tmp_path_factory.mktemp("settings-mail-browser")
    state, log = work / "storage-state.json", work / "manager.log"
    port = _free_port()
    with log.open("w", encoding="utf-8") as out:
        server = subprocess.Popen(  # noqa: S603 -- fixed argv, no shell
            [
                sys.executable,
                str(REPO / "tests" / "design_serve.py"),
                "--port",
                str(port),
                "--storage-state",
                str(state),
                "--mail-relay",
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
            yield base, state, log
        finally:
            server.terminate()
            server.wait(timeout=10)


def test_overlay_and_mail_in_a_browser(manager: tuple[str, Path, Path]) -> None:
    base, state, _log = manager
    assert NODE is not None
    done = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [NODE, str(CHECK), "--base-url", base, "--storage-state", str(state)],
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
        assert f"overlay ({theme}): apply writes the draft against the revision opened" in names
        assert f"mail ({theme}): a test mail is sent to the address typed" in names
    assert done.returncode == 0


def test_the_check_refuses_a_page_off_the_loopback() -> None:
    assert NODE is not None
    done = subprocess.run(  # noqa: S603 -- fixed argv, no shell
        [NODE, str(CHECK), "--base-url", "http://example.test", "--storage-state", "x"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=30,
    )
    assert done.returncode == 2
    assert "loopback only" in done.stderr
