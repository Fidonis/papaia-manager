"""The stand-in world of the browser checks never leaves 127.0.0.1 (MGR-034).

tests/design_serve.py serves the manager for the design check, the CSP check and the browser
tests. Every service the manager may call points at one stand-in on 127.0.0.1 that closes
each connection at once (tests/design_net.py), so a page shows its "unreachable" state as fast
on every host, whatever its name service does with `qdrant` or `litellm`. A guard in the
server process refuses every other lookup and connection at once and logs it to the file
named by PAPAIA_DESIGN_NET_LOG: after every page of ui/pages.yaml and the requests the job
editor makes on its own, that log is empty. The browser walk of tests/test_settle_browser.py
checks the same log after a real browser opened every page.
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
TESTS = REPO / "tests"
sys.path.insert(0, str(TESTS))

import design_net  # noqa: E402
import design_serve  # noqa: E402
import design_world  # noqa: E402

UV = shutil.which("uv")


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def test_every_address_of_a_service_the_manager_calls_points_at_the_stand_in() -> None:
    from app.config import Settings

    # Every setting that names an address the server itself calls; the sign-in address
    # (oidc_issuer_kc_auth) is where the browser is sent, never called by the server.
    called = {
        name.upper()
        for name in Settings.model_fields
        if name.endswith("_url") or name in ("oidc_issuer_kc_token", "oidc_issuer_kc_certs")
    }
    assert called == set(design_net.SERVICES)
    environ: dict[str, str] = {"INVENTORY_SOURCES": "studio=https://studio.example.test:8443"}
    design_net.point_services_at(environ, 52690)
    for name in called:
        assert environ[name].split("/")[2] == "127.0.0.1:52690", name
    assert environ["INVENTORY_SOURCES"] == ""
    kept: dict[str, str] = {}
    design_net.point_services_at(kept, 52690, with_ingest=True)
    assert set(design_net.SERVICES) - set(kept) == set(design_net.WITH_INGEST_KEEPS)


_PROBE = r"""
import json, socket, sys, time
sys.path.insert(0, sys.argv[1])
import design_net
import httpx
log = design_net.guard(__import__("pathlib").Path(sys.argv[2]))
stand_in = design_net.stand_in()
out = {}
def timed(name, fn):
    started = time.monotonic()
    try:
        fn()
        out[name] = ["ok", time.monotonic() - started]
    except Exception as exc:
        out[name] = [type(exc).__name__, time.monotonic() - started]
timed("lookup", lambda: socket.getaddrinfo("qdrant", 6333))
timed("connect", lambda: socket.create_connection(("192.0.2.10", 80), timeout=30))
timed("http", lambda: httpx.get("http://litellm:4000/v1/models", timeout=30))
timed("loopback", lambda: socket.create_connection(("127.0.0.1", stand_in.port), timeout=5).close())
timed("stand-in", lambda: httpx.get(f"http://127.0.0.1:{stand_in.port}/collections", timeout=30))
print(json.dumps(out))
"""


def test_the_guard_refuses_a_lookup_or_connection_beyond_the_loopback_at_once(
    tmp_path: Path,
) -> None:
    log = tmp_path / "net.log"
    done = subprocess.run(  # noqa: S603 -- fixed argv
        [sys.executable, "-c", _PROBE, str(TESTS), str(log)],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    )
    seen = json.loads(done.stdout)
    assert seen["lookup"][0] == "gaierror"
    assert seen["connect"][0] == "ConnectionRefusedError"
    assert seen["http"][0] == "ConnectError"
    assert seen["loopback"][0] == "ok"
    # The stand-in takes the connection and closes it: the client's "unreachable".
    assert seen["stand-in"][0] in ("RemoteProtocolError", "ReadError", "ConnectError")
    for name, (_, took) in seen.items():
        assert took < 1.0, f"{name} took {took:.2f}s"
    refused = design_net.refused(log)
    assert [(r["call"], r["target"]) for r in refused] == [
        ("getaddrinfo", "qdrant"),
        ("connect", "192.0.2.10:80"),
        ("getaddrinfo", "litellm"),
    ]


@pytest.fixture
def served(tmp_path: Path) -> Iterator[tuple[str, dict[str, Path], Path]]:
    """tests/design_serve.py as the design check starts it, with the guard's log in tmp_path."""
    assert UV is not None
    persons = design_serve.design_persons()
    states = {person: tmp_path / f"{person}.json" for person in persons}
    log = tmp_path / "net.log"
    port = _free_port()
    argv = [UV, "run", "--frozen", "python", "../tests/design_serve.py", "--port", str(port)]
    for person, state in states.items():
        argv += ["--storage-state", f"{person}={state}"]
    server = subprocess.Popen(  # noqa: S603 -- fixed argv, loopback only
        argv,
        cwd=REPO / "src",
        env={**os.environ, "PAPAIA_DESIGN_NET_LOG": str(log)},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(150):
            if server.poll() is not None:
                pytest.fail("the manager did not start")
            try:
                with urllib.request.urlopen(base + "/health", timeout=2):  # noqa: S310
                    if all(s.is_file() and s.stat().st_size for s in states.values()):
                        break
            except OSError:
                pass
            time.sleep(0.3)
        else:
            pytest.fail("the manager does not answer")
        yield base, states, log
    finally:
        server.terminate()
        server.wait(timeout=20)


def _cookie(state: Path) -> str:
    cookie = json.loads(state.read_text(encoding="utf-8"))["cookies"][0]
    return f"{cookie['name']}={cookie['value']}"


def _call(
    base: str, path: str, cookie: str, body: dict[str, object] | None = None
) -> tuple[int, float]:
    request = urllib.request.Request(  # noqa: S310 -- loopback only
        base + path,
        data=None if body is None else json.dumps(body).encode(),
        method="GET" if body is None else "POST",
        headers={
            "Cookie": cookie,
            "Content-Type": "application/json",
            "X-CSRF-Token": design_world.CSRF,
        },
    )
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=60) as response:  # noqa: S310
            status: int = response.status
    except urllib.error.HTTPError as exc:
        status = exc.code
    return status, time.monotonic() - started


# What the pages ask for after the load, beyond their own address: the frame's partials and
# the job editor's own requests.
_FRAME = [
    "/partials/service-status",
    "/partials/nav/host-indicator",
    "/partials/nav/job-indicator",
    "/partials/nav/addon-upgrades",
    "/partials/nav/upgrade-indicator",
]
_EDITOR_GET = ["/api/v1/rag/ingest/collections?connection=default"]
_EDITOR_POST = {
    "/api/v1/rag/ingest/schedule/preview": {"schedule": {"mode": "manual"}},
    "/api/v1/rag/ingest/jobs/validate": {
        "job": {
            "id": "handbook",
            "source": {"type": "local", "label": "handbook", "path": "/data/local/handbook"},
            "target": {"collection": "kb", "connection": "default"},
            "mode": "upsert",
            "schedule": {"mode": "manual"},
            "embedding": {"model": "nomic-embed-text"},
        },
        "create": True,
    },
}


def test_every_page_of_the_list_is_served_without_leaving_the_loopback(
    served: tuple[str, dict[str, Path], Path],
) -> None:
    base, states, log = served
    assert log.is_file(), "the server runs without the guard"
    for person, state in states.items():
        cookie = _cookie(state)
        for page in design_serve.design_pages(person=person):
            status, took = _call(base, page, cookie)
            assert status == 200, (person, page, status)
            assert took < 10, (page, took)
    anna = _cookie(states["anna"])
    for path in _FRAME + _EDITOR_GET:
        status, took = _call(base, path, anna)
        assert status == 200, (path, status)
        assert took < 10, (path, took)
    for path, body in _EDITOR_POST.items():
        status, took = _call(base, path, anna, body)
        assert status == 200, (path, status)
        assert took < 10, (path, took)
    assert design_net.refused(log) == []
