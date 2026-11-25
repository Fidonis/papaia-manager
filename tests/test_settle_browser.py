"""Every page settles for the kit's design check, and the job editor stays quiet (MGR-034).

The design check (scripts/design-check.sh) hung on /ingest/new: the kit's `settle` waits for
every finite animation of the page to finish, and the checkmark of a box inside the closed
section "Processing" of the job editor never did, because Chromium does not advance an
animation in the hidden part of a closed <details>. tests/settle_browser.mjs changes the state
of every box inside a closed section of every page of ui/pages.yaml, reads the styles there
and runs the kit's `settle` with a limit, with reduced motion and without: no page may keep it
waiting. The same walk leaves the network guard's log of tests/design_serve.py empty: the
pages and everything they ask for on their own stay on 127.0.0.1. And /ingest/new, left alone
for 10 s after the load, sends nothing beyond its start requests.

Needs the browser tools of scripts/design-tools.sh.
"""

from __future__ import annotations

import json
import os
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

import design_net  # noqa: E402
import design_serve  # noqa: E402

NODE = shutil.which("node")
UV = shutil.which("uv")
TOOLS = REPO / ".pk" / "node_modules" / "playwright-core" / "index.mjs"
SETTLE = REPO / ".pk" / "package" / "src" / "conform" / "collect.ts"
ASSETS = REPO / "src" / "app" / "static" / "alpine-csp.min.js"

pytestmark = pytest.mark.skipif(
    NODE is None
    or UV is None
    or not TOOLS.is_file()
    or not SETTLE.is_file()
    or not ASSETS.is_file(),
    reason="browser tools not installed (scripts/design-tools.sh)",
)

# What the job editor asks for on its own when it opens a new job; nothing else after that.
START_REQUESTS = [
    "GET /api/v1/rag/ingest/collections?connection=default",
    "POST /api/v1/rag/ingest/schedule/preview",
]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


@pytest.fixture(scope="module")
def served(tmp_path_factory: pytest.TempPathFactory) -> Iterator[tuple[str, Path, Path]]:
    """The manager as the design check serves it, every person signed in, the guard's log."""
    assert UV is not None
    work = tmp_path_factory.mktemp("settle")
    log = work / "net.log"
    persons = {
        person: {
            "state": str(work / f"{person}.json"),
            "pages": design_serve.design_pages(person=person),
        }
        for person in design_serve.design_persons()
    }
    pages = work / "pages.json"
    pages.write_text(json.dumps(persons), encoding="utf-8")
    port = _free_port()
    argv = [UV, "run", "--frozen", "python", "../tests/design_serve.py", "--port", str(port)]
    for person, entry in persons.items():
        argv += ["--storage-state", f"{person}={entry['state']}"]
    server = subprocess.Popen(  # noqa: S603 -- fixed argv, loopback only
        argv,
        cwd=REPO / "src",
        env={**os.environ, "PAPAIA_DESIGN_NET_LOG": str(log)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    states = [Path(entry["state"]) for entry in persons.values()]
    try:
        for _ in range(150):
            if server.poll() is not None:
                pytest.fail("the manager did not start")
            try:
                with urllib.request.urlopen(base + "/health", timeout=2):  # noqa: S310
                    if all(s.is_file() and s.stat().st_size for s in states):
                        break
            except OSError:
                pass
            time.sleep(0.3)
        else:
            pytest.fail("the manager does not answer")
        yield base, pages, log
    finally:
        server.terminate()
        server.wait(timeout=20)


def _walk(base: str, pages: Path, report: Path, *extra: str) -> dict[str, object]:
    assert NODE is not None
    done = subprocess.run(  # noqa: S603 -- fixed argv
        [NODE, "tests/settle_browser.mjs", "--base-url", base, "--pages", str(pages),
         "--report", str(report), *extra],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=900,
    )  # fmt: skip
    assert done.returncode == 0, done.stdout + done.stderr
    result: dict[str, object] = json.loads(report.read_text(encoding="utf-8"))
    return result


def test_every_page_settles_after_a_change_in_a_closed_section(
    served: tuple[str, Path, Path], tmp_path: Path
) -> None:
    base, pages, log = served
    result = _walk(base, pages, tmp_path / "report.json")
    assert result["errors"] == []
    assert result["hung"] == []
    persons = json.loads(pages.read_text(encoding="utf-8"))
    every = sum(len(entry["pages"]) for entry in persons.values())
    assert result["checked"] == 2 * every
    assert isinstance(result["touched"], int) and result["touched"] > 0
    # Everything the pages asked for on their own stayed on the loopback.
    assert log.is_file(), "the server runs without the guard"
    assert design_net.refused(log) == []


def test_the_job_editor_sends_nothing_more_when_left_alone(
    served: tuple[str, Path, Path], tmp_path: Path
) -> None:
    base, pages, _ = served
    result = _walk(base, pages, tmp_path / "idle.json", "--idle", "/ingest/new")
    assert sorted(result["idle"]) == sorted(START_REQUESTS)  # type: ignore[call-overload]
