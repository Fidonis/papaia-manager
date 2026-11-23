"""The browser path of Embedding, Collections and Models & Usage (tests/rag_browser.mjs).

Serves the manager with the stand-in RAG system of tests/rag_serve.py (127.0.0.1, signed-in
test administrator; Qdrant, the ingester and LiteLLM are the in-memory fakes of the tests) and
drives the three pages in Chromium on the CSP build of Alpine, in the light and the dark colour
scheme: a collection is created and deleted; the documents folder is embedded into a
collection with another model after the typed confirmation, the run strip shows the run and
Abort ends it; the chart's tooltip follows the pointer and the keyboard. No report of the
policy, no Alpine expression error, no script error. Needs the browser tools of
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
CHECK = REPO / "tests" / "rag_browser.mjs"
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
    work = tmp_path_factory.mktemp("rag-browser")
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


def test_embedding_collections_and_models_in_a_browser(manager: tuple[str, Path, Path]) -> None:
    base, state, _ = manager
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
    assert done.returncode == 0
    names = {c["name"] for c in report["checks"]}
    for scheme in ("light", "dark"):
        for name in (
            "collections: the collection is deleted and gone from the list",
            "embedding: the strip ends the run and the page takes it",
            "models: the arrow keys move the tooltip",
            "no Alpine expression error",
        ):
            assert f"{scheme}: {name}" in names


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
