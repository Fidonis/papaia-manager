"""The ingest pages in a real browser on the CSP build of Alpine (T-UI-03).

Serves the manager with the fake ingester (tests/design_serve.py --with-ingest) on 127.0.0.1
and walks the pages with tests/ingest_browser.mjs, once per colour scheme on a fresh world: list
and filter the jobs, disable and enable one, start a run and abort it, filter the runs and
delete runs of a job, switch the tabs of a job, delete a job, remove a leftover, save the
defaults and check the job file, and see the panel of a list that cannot be loaded; then create
a job in the editor, edit one (the app's dialog asks before a link leaves unsaved changes),
store a credential from the editor, store, replace and delete one on the Credentials page, and
see the panel of that list when it cannot be loaded. Fails on any step, any report of the
policy, refused Alpine expression or script error. Needs the browser tools of
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

STEPS = [
    "list and filter the jobs",
    "disable and enable a job",
    "start a run and abort it",
    "filter the runs and delete those of a job",
    "switch the tabs of a job",
    "delete a job",
    "remove a leftover",
    "save the defaults and check the file",
    "a list that cannot be loaded says why",
    "create a job in the editor",
    "edit a job and keep the changes when a link would leave",
    "store a credential from the editor",
    "store, replace and delete a credential",
    "a credentials list that cannot be loaded says why",
]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    """A fresh world per walk: the walk deletes a job, runs and a leftover."""
    assert UV is not None
    state = tmp_path / "storage-state.json"
    port = _free_port()
    server = subprocess.Popen(  # noqa: S603 -- fixed argv, loopback only
        [UV, "run", "--frozen", "python", "../tests/design_serve.py", "--port", str(port),
         "--storage-state", str(state), "--with-ingest"],
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


@pytest.mark.parametrize("scheme", ["light", "dark"])
def test_the_ingest_lists_work_on_the_csp_build(
    served: tuple[str, Path], tmp_path: Path, scheme: str
) -> None:
    assert NODE is not None
    base, state = served
    report = tmp_path / "report.json"
    done = subprocess.run(  # noqa: S603 -- fixed argv
        [NODE, "tests/ingest_browser.mjs", "--base-url", base, "--storage-state", str(state),
         "--scheme", scheme, "--report", str(report)],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=300,
    )  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr
    result = json.loads(report.read_text(encoding="utf-8"))
    assert result["scheme"] == scheme
    assert result["failure"] is None
    assert result["steps"] == STEPS
    assert result["violations"] == []
    assert result["alpine"] == []
    assert result["errors"] == []
    assert result["sent"]["run"] == {
        "dry_run": False,
        "skip_sync": False,
        "delete_vanished": True,
        "confirm_rebuild": False,
        "confirm_collection": False,
    }
