"""The Backup / Restore page in a real browser on the CSP build of Alpine (T-UI-03).

Serves the manager in the stand-in world with restore points (tests/design_serve.py
--with-backups) on 127.0.0.1 and walks the page with tests/backup_browser.mjs in both colour
schemes: start a backup and follow its job, change the schedule, list and open restore
points, select and clear them, start and confirm a restore. Fails on any step, any report of
the policy, refused Alpine expression or script error. Needs the browser tools of
scripts/design-tools.sh.
"""

from __future__ import annotations

import json
import shutil
import socket
import subprocess
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
UV = shutil.which("uv")
TOOLS = REPO / ".pk" / "node_modules" / "playwright-core" / "index.mjs"
ASSETS = REPO / "src" / "app" / "static" / "alpine-csp.min.js"

pytestmark = pytest.mark.skipif(
    NODE is None or UV is None or not TOOLS.is_file() or not ASSETS.is_file(),
    reason="browser tools not installed (scripts/design-tools.sh)",
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path]]:
    assert UV is not None
    work = tmp_path_factory.mktemp("backup-browser")
    state = work / "storage-state.json"
    port = _free_port()
    server = subprocess.Popen(  # noqa: S603 -- fixed argv, loopback only
        [UV, "run", "--frozen", "python", "../tests/design_serve.py", "--port", str(port),
         "--storage-state", str(state), "--with-backups"],
        cwd=REPO / "src",
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )  # fmt: skip
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(100):
            if server.poll() is not None:
                pytest.fail("the manager did not start")
            try:
                with urllib.request.urlopen(base + "/health", timeout=2):  # noqa: S310
                    if state.is_file() and state.stat().st_size:
                        break
            except OSError:
                pass
            time.sleep(0.3)
        else:
            pytest.fail("the manager does not answer")
        yield base, state
    finally:
        server.terminate()
        server.wait(timeout=20)


def test_the_backup_page_works_on_the_csp_build_in_both_schemes(
    served: tuple[str, Path], tmp_path: Path
) -> None:
    assert NODE is not None
    base, state = served
    report = tmp_path / "report.json"
    done = subprocess.run(  # noqa: S603 -- fixed argv
        [NODE, "tests/backup_browser.mjs", "--base-url", base, "--storage-state", str(state),
         "--report", str(report)],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr
    results = json.loads(report.read_text(encoding="utf-8"))["results"]
    assert [r["scheme"] for r in results] == ["light", "dark"]
    for result in results:
        assert result["failure"] is None
        assert result["steps"] == [
            "start a backup",
            "the job state reaches the header",
            "the job strip announces its state",
            "change the schedule",
            "list the restore points and open one",
            "select and clear restore points",
            "start and confirm a restore",
        ]
        assert result["violations"] == []
        assert result["alpine"] == []
        assert result["errors"] == []
        assert result["sent"]["backup"] == {"retention_days": 7}
        assert result["sent"]["restore"] == {
            "restore_point": "2026-07-30_10-19-38",
            "restart_clean": False,
        }
