"""Service groups are started and stopped through papaia-docker-gate, not through a socket.

The queue keeps the callback and the test runs it, the gate is the in-memory fake, so no
Docker daemon is needed. papaia-ctl is replaced by a stand-in that fails the test: on a
single host a group action must not reach for a Docker socket the manager does not have.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

_SESSION_SECRET = "test-session-secret-value"
_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-stack-gate-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-stack-gate-workspace-")

os.environ.update(
    OIDC_ISSUER_KC_AUTH="https://kc.test/auth",
    OIDC_ISSUER_KC_TOKEN="https://kc.test/token",
    OIDC_ISSUER_KC_CERTS="https://kc.test/certs",
    MANAGER_ADMIN_ROLE="admin",
    MANAGER_USER_ROLE="user",
    MANAGER_HOST="http://localhost:8120",
    MANAGER_OIDC_CLIENT_SECRET="client-secret",
    MANAGER_SESSION_SECRET=_SESSION_SECRET,
)

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app import main as app_main  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core import docker_gate, kit_audit, services  # noqa: E402
from app.core.docker_gate import GateClient  # noqa: E402
from app.core.jobs import Job, JobCallback, JobContext  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers import api_stack  # noqa: E402
from tests.fake_docker_gate import Container, FakeGate  # noqa: E402

_CSRF = "test-csrf-token"
_CORRELATION = "0190f0e0-aaaa-7bbb-8ccc-123456789abc"


class _FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, Any]] = []

    async def enqueue(
        self,
        *,
        action: str,
        target: str,
        user: str,
        params: dict[str, Any] | None = None,
        callback: JobCallback,
    ) -> Job:
        self.enqueued.append({"action": action, "callback": callback})
        return Job(
            id="job-1", action=action, target=target, user=user,
            created_at=datetime.now(tz=UTC), params=params or {},
        )


def _write_workspace() -> None:
    Path(_CONFIG_DIR, ".env").write_text(
        "COMPOSE_PROJECT_NAME=papaia\nCOMPOSE_PROFILES=librechat\n", encoding="utf-8"
    )
    src = Path(_WORKSPACE_DIR, "papaia", "src")
    (src / "chat").mkdir(parents=True, exist_ok=True)
    (src / "docker-compose.yml").write_text("include:\n  - chat/docker-compose.yml\n")
    (src / "chat" / "docker-compose.yml").write_text(
        "services:\n"
        "  librechat:\n"
        "    restart: unless-stopped\n"
        "    profiles: [librechat]\n"
        "    labels:\n"
        "      de.fidonis.module: papaia-librechat\n"
        "  librechat-rag:\n"
        "    restart: unless-stopped\n"
        "    profiles: [librechat]\n"
        "    labels:\n"
        "      de.fidonis.module: papaia-librechat\n",
        encoding="utf-8",
    )


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> _FakeQueue:
    fake = _FakeQueue()
    monkeypatch.setattr(app_main, "_job_queue", fake)
    return fake


@pytest.fixture
def gate(monkeypatch: pytest.MonkeyPatch) -> FakeGate:
    fake = FakeGate(
        containers=[
            Container("papaia-librechat-1", "papaia", "librechat", module="papaia-librechat"),
            Container("papaia-librechat-rag-1", "papaia", "librechat-rag",
                      module="papaia-librechat", state="exited",
                      status="Exited (0) 1 hour ago"),
            Container("other-librechat-1", "other", "librechat", module="papaia-librechat"),
        ]
    )
    monkeypatch.setattr(
        docker_gate,
        "default_client",
        lambda: GateClient("http://papaia-docker-gate:2375", transport=fake.transport()),
    )
    services.invalidate_snapshot()
    return fake


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch, queue: _FakeQueue, gate: FakeGate
) -> Iterator[TestClient]:
    monkeypatch.setenv("PAPAIA_CONFIG_DIR", _CONFIG_DIR)
    monkeypatch.setenv("PAPAIA_WORKSPACE_DIR", _WORKSPACE_DIR)
    get_settings.cache_clear()
    _write_workspace()
    Path(_CONFIG_DIR, "manager", "audit.log").unlink(missing_ok=True)

    async def no_ctl(**_: Any) -> Any:
        raise AssertionError("a group action on a single host must not run papaia-ctl")

    monkeypatch.setattr(api_stack, "run_core_verb", no_ctl)
    yield TestClient(create_app(), follow_redirects=False)
    get_settings.cache_clear()
    services.invalidate_snapshot()


def _as_admin(client: TestClient) -> TestClient:
    session = {
        "user": {
            "sub": "u-1",
            "preferred_username": "tester",
            "roles": ["admin"],
            "exp": int(time.time()) + 3600,
        },
        "_csrf_token": _CSRF,
    }
    payload = base64.b64encode(json.dumps(session).encode())
    client.cookies.set(
        "papaia_manager_session", TimestampSigner(_SESSION_SECRET).sign(payload).decode()
    )
    return client


def _post(client: TestClient, action: str, **body: Any) -> Any:
    return _as_admin(client).post(
        f"/api/v1/stack/groups/{action}",
        json={"groups": ["librechat"], **body},
        headers={"X-CSRF-Token": _CSRF},
    )


def _run_job(queue: _FakeQueue, log_path: Path) -> BaseException | None:
    (entry,) = queue.enqueued
    ctx = JobContext(
        job=Job(id="job-1", action=entry["action"], target="librechat", user="tester",
                created_at=datetime.now(tz=UTC)),
        log_path=log_path,
    )

    async def run() -> None:
        with kit_audit.bound(None, _CORRELATION):
            await entry["callback"](ctx)

    try:
        asyncio.run(run())
    except Exception as exc:  # noqa: BLE001 - the job queue records it as a failure
        return exc
    return None


def _audit() -> list[dict[str, Any]]:
    path = Path(_CONFIG_DIR, "manager", "audit.log")
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def test_stop_stops_the_running_containers_of_the_group_through_the_gate(
    client: TestClient, queue: _FakeQueue, gate: FakeGate, tmp_path: Path
) -> None:
    response = _post(client, "stop")
    assert response.status_code == 202, response.text
    assert _run_job(queue, tmp_path / "job.log") is None

    writes = [(m, p) for m, p in gate.calls() if m == "POST"]
    assert writes == [("POST", "/containers/papaia-librechat-1/stop")]
    stop = next(r for r in gate.requests if r.method == "POST")
    assert stop.headers["X-Papaia-Correlation-Id"] == _CORRELATION
    log = (tmp_path / "job.log").read_text()
    assert "[gate] stop papaia-librechat-1" in log
    (entry,) = _audit()
    assert (entry["action"], entry["target"], entry["result"]) == (
        "group-stop", "librechat", "ok"
    )


def test_start_starts_what_is_not_running(
    client: TestClient, queue: _FakeQueue, gate: FakeGate, tmp_path: Path
) -> None:
    assert _post(client, "start").status_code == 202
    assert _run_job(queue, tmp_path / "job.log") is None

    writes = [p for m, p in gate.calls() if m == "POST"]
    assert writes == ["/containers/papaia-librechat-rag-1/start"]
    assert gate.containers[1].state == "running"


def test_restart_stops_then_starts(
    client: TestClient, queue: _FakeQueue, gate: FakeGate, tmp_path: Path
) -> None:
    assert _post(client, "restart").status_code == 202
    assert _run_job(queue, tmp_path / "job.log") is None

    writes = [p for m, p in gate.calls() if m == "POST"]
    assert writes == [
        "/containers/papaia-librechat-1/stop",
        "/containers/papaia-librechat-1/start",
        "/containers/papaia-librechat-rag-1/start",
    ]


def test_a_container_that_does_not_exist_cannot_be_created_through_the_gate(
    client: TestClient, queue: _FakeQueue, gate: FakeGate, tmp_path: Path
) -> None:
    del gate.containers[1]

    assert _post(client, "start").status_code == 202
    failure = _run_job(queue, tmp_path / "job.log")

    assert failure is not None
    assert "papaia-ctl start" in str(failure)
    assert [p for m, p in gate.calls() if m == "POST"] == []
    (entry,) = _audit()
    assert entry["result"] == "failed"


def test_clean_up_removes_containers_which_the_gate_does_not_allow(
    client: TestClient, queue: _FakeQueue, gate: FakeGate
) -> None:
    response = _post(client, "stop", clean_up=True)

    assert response.status_code == 409
    assert "papaia-docker-gate" in response.json()["detail"]
    assert queue.enqueued == []


def test_a_refusal_of_the_gate_fails_the_job_and_is_audited(
    client: TestClient, queue: _FakeQueue, gate: FakeGate, tmp_path: Path
) -> None:
    assert _post(client, "stop").status_code == 202
    # Between the reading and the call the container moved to a project the gate does not
    # name: the gate decides, and the job reports its answer.
    gate.containers[0].project = "other"

    failure = _run_job(queue, tmp_path / "job.log")

    assert failure is not None
    assert "docker_gate.foreign_project" in str(failure)
    (entry,) = _audit()
    assert entry["result"] == "failed"


def test_a_missing_gate_refuses_the_action_before_it_is_queued(
    client: TestClient, queue: _FakeQueue, gate: FakeGate
) -> None:
    gate.down = True

    response = _post(client, "stop")

    assert response.status_code == 503
    assert "not supported" in response.json()["detail"]
    assert queue.enqueued == []
