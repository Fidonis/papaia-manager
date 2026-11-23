"""The browser check of the policy (tests/csp_browser.mjs) on seeded pages.

A small server on 127.0.0.1 answers with the manager's enforced policy. The check opens every
page in the light and the dark colour scheme and fails on any `securitypolicyviolation` event:
a page with an inline script or a `style` attribute fails, marked or not, and so does a page
that answers without the enforced policy; a clean page passes. Needs the browser tools of
scripts/design-tools.sh; the CI job `csp` installs them and runs this file before checking the
manager's pages.
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
# Pages served with the policy only reported, not enforced.
REPORT_ONLY = {"/report-only": PAGES["/clean"]}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 -- the name http.server calls
        header = "Content-Security-Policy"
        if self.path in REPORT_ONLY:
            body, kind = REPORT_ONLY[self.path].encode(), "text/html; charset=utf-8"
            header = "Content-Security-Policy-Report-Only"
        elif self.path in PAGES:
            body, kind = PAGES[self.path].encode(), "text/html; charset=utf-8"
        elif self.path in SCRIPTS:
            body, kind = SCRIPTS[self.path].encode(), "text/javascript"
        else:
            self.send_error(404)
            return
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header(header, POLICY)
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
    done = subprocess.run(argv, cwd=REPO, capture_output=True, text=True, check=False, timeout=180)
    doc = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else {}
    return done.returncode, doc, done.stderr


def _by_scheme(doc: dict[str, object], path: str) -> dict[str, dict[str, object]]:
    results = doc["results"]
    assert isinstance(results, list)
    return {r["scheme"]: r for r in results if r["page"] == path}


def test_a_page_with_an_inline_script_fails_in_both_schemes(base_url: str, tmp_path: Path) -> None:
    code, doc, stderr = _check(base_url, tmp_path, "/seeded", "/clean")
    assert code == 1, stderr
    seeded, clean = _by_scheme(doc, "/seeded"), _by_scheme(doc, "/clean")
    assert set(seeded) == set(clean) == {"light", "dark"}
    for scheme in ("light", "dark"):
        assert seeded[scheme]["policy"] == POLICY
        assert [v["directive"] for v in seeded[scheme]["violations"]] == ["script-src-elem"]
        assert clean[scheme]["violations"] == []
        assert f"page /seeded ({scheme}) is not clean" in stderr


def test_a_page_with_a_style_attribute_fails(base_url: str, tmp_path: Path) -> None:
    code, doc, _ = _check(base_url, tmp_path, "/styled")
    assert code == 1
    for result in _by_scheme(doc, "/styled").values():
        assert [v["directive"] for v in result["violations"]] == ["style-src-attr"]


def test_an_unmarked_page_fails_as_well(base_url: str, tmp_path: Path) -> None:
    code, doc, stderr = _check(base_url, tmp_path, "/seeded-unmarked", "/clean")
    assert code == 1, stderr
    for result in _by_scheme(doc, "/seeded-unmarked").values():
        assert result["converted"] is False
        assert len(result["violations"]) == 1


def test_a_page_without_the_enforced_policy_fails(base_url: str, tmp_path: Path) -> None:
    code, doc, stderr = _check(base_url, tmp_path, "/report-only")
    assert code == 1, stderr
    for result in _by_scheme(doc, "/report-only").values():
        assert result["policy"] == ""
    assert "answered without the enforced policy" in stderr


def test_a_clean_page_passes(base_url: str, tmp_path: Path) -> None:
    code, doc, stderr = _check(base_url, tmp_path, "/clean")
    assert code == 0, stderr
    assert set(_by_scheme(doc, "/clean")) == {"light", "dark"}


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
