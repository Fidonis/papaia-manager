"""The image report of the Upgrade page is bounded, and its checks never ask the host's Docker.

Two halves (MGR-033):

* Hermetic. The manager of the test world (tests/design_serve.py) runs `docker` from the
  stand-in of tests/fake_docker.py, whatever `docker` the host has: a host `docker` that sleeps
  for a minute on every call is never reached, and the stand-in's log shows the image calls.
* Bounded. `/partials/upgrade/images` gives Docker one time limit for all its calls together
  (`images.REPORT_LIMIT`). When it is reached, every `docker` process of the report is killed
  -- its children too -- and the partial shows the error state with the limit as the reason
  and no removal. The inspect chunks run a few at a time, so a host with 1200 images is read
  within the limit, with the same verdict as the pure `build_report` on the synthetic set.

The existing report, prune and API tests (test_images.py, test_api_upgrade_images.py) stay
as they were.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"
sys.path.insert(0, str(REPO / "tests"))

import design_serve  # noqa: E402
import design_world  # noqa: E402
import fake_docker  # noqa: E402

_SESSION_SECRET = "test-session-secret-value"
_CSRF = "test-csrf-token-value"
_ROOT = Path(tempfile.mkdtemp(prefix="papaia-images-bounded-"))
_CONFIG, _WORKSPACE = design_world.make_world(_ROOT)
design_world.add_core_compose(_WORKSPACE)

for _key, _value in design_world.environment(
    _CONFIG, _WORKSPACE, host="http://localhost:8120", secret=_SESSION_SECRET
).items():
    os.environ.setdefault(_key, _value)

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app import main as app_main  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core import images  # noqa: E402
from app.core.jobs import JobQueue  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers import api_upgrade  # noqa: E402

# What the small synthetic set leaves behind: the declared repositories' older versions.
_SMALL_OUTDATED = [
    "postgres:16",
    "ghcr.io/example/papaia-sample:1.9",
    "valkey/valkey:8-alpine",
]


def _alive(pid: int) -> bool:
    """Whether `pid` still exists (a zombie counts: it was never reaped)."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:  # pragma: no cover -- a pid reused by another user
        return True
    return True


def _gone(pids: list[int], within: float = 2.0) -> list[int]:
    deadline = time.monotonic() + within
    left = [pid for pid in pids if _alive(pid)]
    while left and time.monotonic() < deadline:
        time.sleep(0.05)
        left = [pid for pid in left if _alive(pid)]
    return left


@pytest.fixture
def stand_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The stand-in first on PATH of this process, a slow host docker behind it; its log."""
    host = tmp_path / "host-bin"
    fake_docker.write_slow_host_docker(host)
    environ: dict[str, str] = {"PATH": os.pathsep.join([str(host), os.environ["PATH"]])}
    log = tmp_path / "docker.log"
    design_serve.use_fake_docker(environ, log=log)
    for key in ("PATH", "PAPAIA_FAKE_DOCKER_PYTHON", "PAPAIA_FAKE_DOCKER_LOG"):
        monkeypatch.setenv(key, environ[key])
    monkeypatch.delenv(fake_docker.ENV_DELAY, raising=False)
    monkeypatch.delenv(fake_docker.ENV_IMAGES, raising=False)
    return log


@pytest.fixture
def client(stand_in: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PAPAIA_CONFIG_DIR", str(_CONFIG))
    monkeypatch.setenv("PAPAIA_WORKSPACE_DIR", str(_WORKSPACE))
    get_settings.cache_clear()
    app_main._job_queue = JobQueue(config_dir=str(_CONFIG))  # noqa: SLF001

    async def idle(kind: object = None) -> None:
        return None

    monkeypatch.setattr(api_upgrade.runner, "find_runner", idle)
    yield TestClient(create_app(), follow_redirects=False)
    get_settings.cache_clear()
    app_main._job_queue = None  # noqa: SLF001


def _admin(client: TestClient) -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": "u-1",
            "preferred_username": "tester",
            "roles": ["admin"],
            "exp": int(time.time()) + 3600,
        },
        "_csrf_token": _CSRF,
    }
    payload = base64.b64encode(json.dumps(session).encode())
    client.cookies.clear()
    client.cookies.set(
        "papaia_manager_session", TimestampSigner(_SESSION_SECRET).sign(payload).decode()
    )
    return client


# -- the stand-in --------------------------------------------------------------------------


def test_the_stand_in_answers_the_image_calls_from_its_synthetic_set() -> None:
    environ: dict[str, str] = {}
    code, out, _ = fake_docker.answer(
        ["image", "ls", "--all", "--no-trunc", "--format", "{{json .}}"], environ
    )
    assert code == 0
    rows = [json.loads(line) for line in out.splitlines()]
    assert {f"{r['Repository']}:{r['Tag']}" for r in rows} >= {"postgres:16", "alpine:3.20"}
    code, out, _ = fake_docker.answer(
        ["image", "inspect", "--format", "{{json .}}", rows[0]["ID"]], environ
    )
    assert code == 0
    assert json.loads(out)["Id"] == rows[0]["ID"]
    code, out, _ = fake_docker.answer(
        ["compose", "-f", "x.yml", "--env-file", ".env", "config", "--images"], environ
    )
    assert (code, out.split()) == (0, list(fake_docker.DECLARED))


@pytest.mark.parametrize(
    "argv",
    [
        ["ps", "--format", "{{json .}}"],
        ["compose", "-p", "papaia", "up", "-d"],
        ["image", "rm", "postgres:16"],
        ["image", "pull", "postgres:18.3"],
        ["info"],
        [],
    ],
)
def test_every_other_call_is_an_unreachable_daemon(argv: list[str]) -> None:
    code, out, err = fake_docker.answer(argv, {})
    assert (code, out) == (1, "")
    assert "Cannot connect to the Docker daemon" in err


def test_the_server_resolves_docker_to_the_stand_in(tmp_path: Path) -> None:
    host = tmp_path / "host-bin"
    fake_docker.write_slow_host_docker(host)
    environ = {"PATH": os.pathsep.join([str(host), "/usr/bin", "/bin"])}
    design_serve.use_fake_docker(environ, log=tmp_path / "docker.log")
    found = shutil.which("docker", path=environ["PATH"])
    assert found is not None
    assert Path(found).resolve() == (design_serve.FAKE_DOCKER_BIN / "docker").resolve()
    assert environ["PAPAIA_FAKE_DOCKER_PYTHON"] == sys.executable
    assert environ["PAPAIA_FAKE_DOCKER_LOG"] == str(tmp_path / "docker.log")


def test_the_stand_in_is_a_test_tool_only() -> None:
    """Never copied into the image, and the product still calls `docker` by name."""
    ignored = (REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "tests" in ignored
    dockerfile = (REPO / "docker" / "Dockerfile").read_text(encoding="utf-8")
    assert "tests/" not in dockerfile
    assert "fake_docker" not in dockerfile
    product = (SRC / "app" / "core" / "images.py").read_text(encoding="utf-8")
    assert "fake_docker" not in product
    assert "PAPAIA_FAKE_DOCKER" not in product


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
    return port


def test_the_served_manager_asks_the_stand_in_and_never_the_host(tmp_path: Path) -> None:
    """design_serve.py with a host docker that sleeps a minute first on the inherited PATH."""
    host = tmp_path / "host-bin"
    fake_docker.write_slow_host_docker(host)
    log, state, out = tmp_path / "docker.log", tmp_path / "state.json", tmp_path / "server.log"
    port = _free_port()
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PAPAIA_FAKE_DOCKER")
    }
    env["PATH"] = os.pathsep.join([str(host), env.get("PATH", "")])
    env[fake_docker.ENV_LOG] = str(log)
    with out.open("wb") as server_log:
        server = subprocess.Popen(  # noqa: S603 -- fixed argv, the interpreter of this run
            [
                sys.executable,
                str(REPO / "tests" / "design_serve.py"),
                "--port",
                str(port),
                "--storage-state",
                str(state),
            ],
            cwd=SRC,
            env=env,
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        try:
            base = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 60
            while True:
                assert server.poll() is None, out.read_text()[-2000:]
                try:
                    with urllib.request.urlopen(f"{base}/health", timeout=2) as answer:  # noqa: S310
                        if answer.status == 200 and state.is_file() and state.stat().st_size:
                            break
                except OSError:
                    pass
                assert time.monotonic() < deadline, "the manager did not answer within 60 s"
                time.sleep(0.3)
            cookie = json.loads(state.read_text(encoding="utf-8"))["cookies"][0]
            request = urllib.request.Request(  # noqa: S310 -- loopback
                f"{base}/partials/upgrade/images",
                headers={"Cookie": f"{cookie['name']}={cookie['value']}"},
            )
            started = time.monotonic()
            with urllib.request.urlopen(request, timeout=30) as answer:  # noqa: S310
                html = answer.read().decode()
            assert time.monotonic() - started < 10  # noqa: PLR2004
        finally:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:  # pragma: no cover
                server.kill()
                server.wait()
    for name in _SMALL_OUTDATED:
        assert name in html
    assert "alpine:3.20" not in html
    calls = [entry["argv"][:2] for entry in fake_docker.read_log(log)]
    assert ["image", "ls"] in calls
    assert ["image", "inspect"] in calls
    assert not (host / "host-docker.log").exists()


# -- bounded -------------------------------------------------------------------------------


def test_the_limit_is_below_the_browser_checks_wait() -> None:
    assert 0 < images.REPORT_LIMIT < 30  # noqa: PLR2004


def test_the_report_of_the_small_set_lists_the_outdated_images(client: TestClient) -> None:
    html = _admin(client).get("/partials/upgrade/images").text
    for name in _SMALL_OUTDATED:
        assert name in html
    assert "alpine:3.20" not in html
    assert "valkey/valkey:7-alpine" not in html
    assert "Remove all (3)" in html


@pytest.mark.parametrize(
    ("language", "reason"),
    [
        ("en", "Docker did not answer within 2 s; the list is not shown"),
        ("de", "Docker hat nicht innerhalb von 2 s geantwortet; die Liste wird nicht angezeigt"),
    ],
)
def test_a_slow_docker_ends_at_the_limit_with_the_reason_and_no_removal(
    client: TestClient,
    stand_in: Path,
    monkeypatch: pytest.MonkeyPatch,
    language: str,
    reason: str,
) -> None:
    monkeypatch.setattr(images, "REPORT_LIMIT", 2.0)
    # Each call alone is within its own limit; together they are not.
    monkeypatch.setenv(fake_docker.ENV_DELAY, "5")
    started = time.monotonic()
    answer = _admin(client).get(
        "/partials/upgrade/images", headers={"Accept-Language": language}
    )
    elapsed = time.monotonic() - started
    assert answer.status_code == 200
    assert elapsed < 2.0 + 2.0, elapsed
    html = answer.text
    assert reason in html
    assert "prune-selected-button" not in html
    assert 'data-upgrade-action="prune-all"' not in html
    assert "prune-image-check" not in html
    pids = [entry["pid"] for entry in fake_docker.read_log(stand_in)]
    assert pids, "the report asked no docker"
    assert _gone(pids) == []


def test_the_api_reports_the_limit_too(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(images, "REPORT_LIMIT", 2.0)
    monkeypatch.setenv(fake_docker.ENV_DELAY, "5")
    started = time.monotonic()
    body = _admin(client).get("/api/v1/upgrade/images").json()
    assert time.monotonic() - started < 2.0 + 2.0
    assert body["outdated"] == []
    assert body["errors"] == ["Docker did not answer within 2 s; the list is not shown"]


def test_a_host_with_1200_images_is_read_within_the_limit_with_the_same_verdict(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(fake_docker.ENV_IMAGES, "1200")
    synthetic, containers = fake_docker.large_set(1200)
    expected = images.build_report(
        {images.CORE_SOURCE: frozenset(fake_docker.DECLARED)},
        [
            images.LocalImage(
                id=i.id,
                tags=(i.ref,),
                size=images.parse_size(i.size),
                created=i.created,
            )
            for i in synthetic
        ],
        set(containers.values()),
    )
    assert len(expected.outdated) > 500  # noqa: PLR2004 -- the set is what it claims
    started = time.monotonic()
    body = _admin(client).get("/api/v1/upgrade/images").json()
    assert time.monotonic() - started < images.REPORT_LIMIT
    assert body["errors"] == []
    assert [i["id"] for i in body["outdated"]] == [i.id for i in expected.outdated]
    assert [i["name"] for i in body["outdated"]] == [i.name for i in expected.outdated]


async def test_the_inspect_chunks_overlap_but_stay_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ids = [f"sha256:{n:064x}" for n in range(images._INSPECT_CHUNK * 10)]  # noqa: SLF001
    running = 0
    peak = 0

    async def run(args: list[str], *, limit: float) -> str:
        nonlocal running, peak
        if args[1:3] == ["image", "ls"]:
            return "".join(json.dumps({"ID": i, "Size": "1MB"}) + "\n" for i in ids)
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.05)
        running -= 1
        return "".join(json.dumps({"Id": i, "RepoTags": [], "RepoDigests": []}) + "\n"
                       for i in args[5:])

    monkeypatch.setattr(images, "_run", run)
    local = await images.local_images()
    assert sorted(image.id for image in local) == ids
    assert 1 < peak <= images._INSPECT_PARALLELISM  # noqa: SLF001


async def test_a_cancelled_docker_call_leaves_no_process_behind(tmp_path: Path) -> None:
    """The time limit cancels a running call; the process and its children are killed."""
    pids = tmp_path / "pids"
    script = tmp_path / "docker"
    script.write_text(
        "#!/bin/sh\n"
        f'sleep 60 & echo "$!" >> "{pids}"\n'
        f'echo "$$" >> "{pids}"\n'
        "wait\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    task = asyncio.ensure_future(images._run([str(script), "image", "ls"], limit=30))  # noqa: SLF001
    for _ in range(100):  # up to 5 s for the script to start its child
        if pids.is_file() and len(pids.read_text().split()) == 2:  # noqa: PLR2004
            break
        await asyncio.sleep(0.05)
    started = [int(pid) for pid in pids.read_text().split()]
    assert len(started) == 2  # noqa: PLR2004
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    left = _gone(started)
    for pid in left:  # pragma: no cover -- clean up before failing
        os.kill(pid, signal.SIGKILL)
    assert left == []


async def test_a_call_over_its_own_limit_leaves_no_process_behind(tmp_path: Path) -> None:
    pids = tmp_path / "pids"
    script = tmp_path / "docker"
    script.write_text(
        "#!/bin/sh\n"
        f'sleep 60 & echo "$!" >> "{pids}"\n'
        f'echo "$$" >> "{pids}"\n'
        "wait\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    # Long enough for the script to start its child even on a busy host.
    with pytest.raises(images.ImagesError, match="timed out"):
        await images._run([str(script), "image", "ls"], limit=5)  # noqa: SLF001
    started = [int(pid) for pid in pids.read_text().split()]
    left = _gone(started)
    for pid in left:  # pragma: no cover -- clean up before failing
        os.kill(pid, signal.SIGKILL)
    assert left == []
