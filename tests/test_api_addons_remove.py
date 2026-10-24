"""`POST /api/v1/addons/{name}/remove` takes the containers down before it deactivates.

`papaia-ctl addon remove` alone only marks the add-on inactive and leaves its
containers running, so the job has to run `addon stop --clean-up` first. That
teardown is only safe while the add-on's `docker-compose.yml` exists: without it
Compose falls back to the core project and takes the whole stack down, so the
route and the job refuse. The queue and `run_addon_verb` are replaced, so no
Docker daemon or worker is needed.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

_SESSION_SECRET = "test-session-secret-value"
_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-addon-remove-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-addon-remove-workspace-")

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
# PAPAIA_CONFIG_DIR / PAPAIA_WORKSPACE_DIR are set per test by the fixture, for
# the reason spelled out in test_api_restore_scoped.py.

from fastapi import HTTPException  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app import main as app_main  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core.ctl import CtlError  # noqa: E402
from app.core.jobs import Job, JobCallback, JobContext  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers import api_addons  # noqa: E402

_CSRF = "test-csrf-token"


class _FakeQueue:
    """Keeps the callback instead of handing it to a worker."""

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
        self.enqueued.append({"action": action, "target": target, "callback": callback})
        return Job(
            id="job-1",
            action=action,
            target=target,
            user=user,
            created_at=datetime.now(tz=UTC),
            params=params or {},
        )


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> _FakeQueue:
    fake = _FakeQueue()
    monkeypatch.setattr(app_main, "_job_queue", fake)
    return fake


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, queue: _FakeQueue) -> Iterator[TestClient]:
    monkeypatch.setenv("PAPAIA_CONFIG_DIR", _CONFIG_DIR)
    monkeypatch.setenv("PAPAIA_WORKSPACE_DIR", _WORKSPACE_DIR)
    get_settings.cache_clear()
    yield TestClient(create_app(), follow_redirects=False)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_state() -> None:
    Path(_CONFIG_DIR, "manager", "audit.log").unlink(missing_ok=True)
    Path(_CONFIG_DIR, "deployment.yaml").unlink(missing_ok=True)


def _install(path: str, *, compose_in: Path | None) -> None:
    """Register `n8n` in deployment.yaml, with or without its compose file."""
    Path(_CONFIG_DIR, "deployment.yaml").write_text(
        yaml.safe_dump({"addons": [{"name": "n8n", "path": path, "active": True}]}),
        encoding="utf-8",
    )
    if compose_in is not None:
        compose_in.mkdir(parents=True, exist_ok=True)
        (compose_in / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")


@pytest.fixture
def addon_dir(tmp_path: Path) -> Path:
    path = tmp_path / "n8n"
    _install(str(path), compose_in=path)
    return path


def _as(client: TestClient, role: str) -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": "u-1",
            "preferred_username": "tester",
            "roles": [role],
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


def _remove(client: TestClient, *, role: str = "admin", csrf: str | None = _CSRF) -> Any:
    headers = {"X-CSRF-Token": csrf} if csrf else {}
    return _as(client, role).post("/api/v1/addons/n8n/remove", json={}, headers=headers)


def _audit() -> list[dict[str, Any]]:
    path = Path(_CONFIG_DIR, "manager", "audit.log")
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _run_callback(queue: _FakeQueue, log_path: Path) -> None:
    (entry,) = queue.enqueued
    ctx = JobContext(
        job=Job(
            id="job-1",
            action="remove",
            target="n8n",
            user="tester",
            created_at=datetime.now(tz=UTC),
        ),
        log_path=log_path,
    )
    asyncio.run(entry["callback"](ctx))


def _record_verbs(
    monkeypatch: pytest.MonkeyPatch, *, failing: str | None = None
) -> list[tuple[str, str, tuple[str, ...]]]:
    calls: list[tuple[str, str, tuple[str, ...]]] = []

    async def run_addon_verb(
        *, verb: str, name: str, extra_flags: list[str] | None = None, **_: Any
    ) -> AsyncIterator[str]:
        calls.append((verb, name, tuple(extra_flags or ())))

        async def lines() -> AsyncIterator[str]:
            yield f"{verb} {name} output"
            if verb == failing:
                raise CtlError(f"papaia-ctl addon {verb} failed", 1)

        return lines()

    monkeypatch.setattr(api_addons, "run_addon_verb", run_addon_verb)
    return calls


@pytest.mark.usefixtures("addon_dir")
def test_remove_takes_the_containers_down_before_it_deactivates(
    client: TestClient, queue: _FakeQueue, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _record_verbs(monkeypatch)

    response = _remove(client)
    assert response.status_code == 202, response.text
    _run_callback(queue, tmp_path / "job.log")

    assert calls == [("stop", "n8n", ("--clean-up",)), ("remove", "n8n", ())]
    log = (tmp_path / "job.log").read_text(encoding="utf-8").splitlines()
    assert log == [
        "[ctl] papaia-ctl addon stop n8n --clean-up",
        "stop n8n output",
        "[ctl] papaia-ctl addon remove n8n",
        "remove n8n output",
    ]
    (entry,) = [e for e in _audit() if e["action"] == "remove"]
    assert entry["target"] == "n8n"
    assert entry["result"] == "ok"


@pytest.mark.usefixtures("addon_dir")
def test_remove_does_not_deactivate_when_the_containers_cannot_be_removed(
    client: TestClient, queue: _FakeQueue, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _record_verbs(monkeypatch, failing="stop")

    assert _remove(client).status_code == 202
    with pytest.raises(CtlError):
        _run_callback(queue, tmp_path / "job.log")

    assert calls == [("stop", "n8n", ("--clean-up",))]
    assert [e for e in _audit() if e["action"] == "remove"] == []


def test_remove_is_denied_to_a_plain_user(client: TestClient, queue: _FakeQueue) -> None:
    assert _remove(client, role="user").status_code == 403
    assert queue.enqueued == []


def test_remove_needs_the_csrf_token(client: TestClient, queue: _FakeQueue) -> None:
    assert _remove(client, csrf=None).status_code == 403
    assert queue.enqueued == []


def test_remove_is_refused_while_the_compose_file_is_missing(
    client: TestClient, queue: _FakeQueue, tmp_path: Path
) -> None:
    _install(str(tmp_path / "n8n"), compose_in=None)

    response = _remove(client)

    assert response.status_code == 409
    assert "docker-compose.yml" in response.json()["detail"]
    assert queue.enqueued == []


def test_remove_of_an_add_on_that_is_not_installed_is_a_404(
    client: TestClient, queue: _FakeQueue
) -> None:
    assert _remove(client).status_code == 404
    assert queue.enqueued == []


def test_remove_resolves_a_relative_path_against_the_workspace(
    client: TestClient, queue: _FakeQueue
) -> None:
    _install("addons/n8n", compose_in=Path(_WORKSPACE_DIR, "addons", "n8n"))

    assert _remove(client).status_code == 202
    assert len(queue.enqueued) == 1


def test_the_job_refuses_when_the_files_are_gone_by_the_time_it_runs(
    client: TestClient,
    queue: _FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
    addon_dir: Path,
    tmp_path: Path,
) -> None:
    """An install queued ahead of the remove can fail and leave the directory empty."""
    calls = _record_verbs(monkeypatch)

    assert _remove(client).status_code == 202
    (addon_dir / "docker-compose.yml").unlink()
    with pytest.raises(HTTPException) as refused:
        _run_callback(queue, tmp_path / "job.log")

    assert refused.value.status_code == 409
    assert calls == []
    assert [e for e in _audit() if e["action"] == "remove"] == []
