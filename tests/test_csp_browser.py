"""The browser check of the policy (tests/csp_browser.mjs) on seeded pages.

A small server on 127.0.0.1 answers with the manager's report-only policy: a page marked
converted with an inline script must fail the check, the same page unmarked is only counted,
and a clean converted page passes. Needs the browser tools of scripts/design-tools.sh; the
CI job `csp` installs them and runs this file before checking the manager's pages.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from app.csp import POLICY  # noqa: E402

CHECK = REPO / "tests" / "csp_browser.mjs"
NODE = shutil.which("node")
TOOLS = REPO / ".pk" / "node_modules" / "playwright-core" / "index.mjs"

pytestmark = pytest.mark.skipif(
    NODE is None or not TOOLS.is_file(),
    reason="browser tools not installed (scripts/design-tools.sh)",
)

_STRICT = '<meta name="papaia-csp" content="strict">'
PAGES = {
    "/seeded": f"<!doctype html><html><head>{_STRICT}</head><body>"
    "<script>document.body.dataset.ran = '1';</script><p>seeded</p></body></html>",
    "/seeded-unmarked": "<!doctype html><html><head></head><body>"
    "<script>document.body.dataset.ran = '1';</script><p>seeded</p></body></html>",
    "/clean": f"<!doctype html><html><head>{_STRICT}</head><body>"
    '<script src="/clean.js"></script><p>clean</p></body></html>',
    "/styled": f"<!doctype html><html><head>{_STRICT}</head><body>"
    '<p style="color: red">styled</p></body></html>',
}
SCRIPTS = {"/clean.js": "document.body.dataset.ran = '1';"}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 -- the name http.server calls
        if self.path in PAGES:
            body, kind = PAGES[self.path].encode(), "text/html; charset=utf-8"
        elif self.path in SCRIPTS:
            body, kind = SCRIPTS[self.path].encode(), "text/javascript"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Content-Security-Policy-Report-Only", POLICY)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return


@pytest.fixture(scope="module")
def base_url() -> Iterator[str]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def _check(base_url: str, tmp_path: Path, *pages: str) -> tuple[int, dict[str, object], str]:
    assert NODE is not None
    report = tmp_path / "report.json"
    argv = [NODE, str(CHECK), "--base-url", base_url, "--report", str(report)]
    for page in pages:
        argv += ["--page", page]
    done = subprocess.run(
        argv, cwd=REPO, capture_output=True, text=True, check=False, timeout=180
    )
    doc = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else {}
    return done.returncode, doc, done.stderr


def test_a_converted_page_with_an_inline_script_fails(base_url: str, tmp_path: Path) -> None:
    code, doc, stderr = _check(base_url, tmp_path, "/seeded", "/clean")
    assert code == 1, stderr
    seeded, clean = doc["results"]  # type: ignore[index]
    assert seeded["converted"] is True
    assert [v["directive"] for v in seeded["violations"]] == ["script-src-elem"]
    assert clean["violations"] == []
    assert "converted page /seeded is not clean" in stderr


def test_a_converted_page_with_a_style_attribute_fails(base_url: str, tmp_path: Path) -> None:
    code, doc, _ = _check(base_url, tmp_path, "/styled")
    assert code == 1
    (styled,) = doc["results"]  # type: ignore[index]
    assert [v["directive"] for v in styled["violations"]] == ["style-src-attr"]


def test_a_page_not_converted_is_only_counted(base_url: str, tmp_path: Path) -> None:
    code, doc, stderr = _check(base_url, tmp_path, "/seeded-unmarked", "/clean")
    assert code == 0, stderr
    unmarked, _ = doc["results"]  # type: ignore[index]
    assert unmarked["converted"] is False
    assert len(unmarked["violations"]) == 1


def test_a_page_that_does_not_load_is_an_error(base_url: str, tmp_path: Path) -> None:
    code, _, stderr = _check(base_url, tmp_path, "/missing")
    assert code == 2
    assert "/missing answered 404" in stderr


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["--base-url", "http://example.test"],
        ["--base-url", "http://127.0.0.1:1", "--page", "relative"],
        ["--base-url", "http://127.0.0.1:1", "--unknown", "x"],
    ],
)
def test_the_check_refuses_bad_arguments(argv: list[str]) -> None:
    assert NODE is not None
    done = subprocess.run(
        [NODE, str(CHECK), *argv], cwd=REPO, capture_output=True, text=True, check=False
    )
    assert done.returncode == 2
