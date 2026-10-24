"""`POST /api/v1/addons/{name}/remove` takes the containers down before it deactivates.

`papaia-ctl addon remove` alone only marks the add-on inactive and leaves its
containers running, so the job has to run `addon stop --clean-up` first. The
queue and `run_addon_verb` are replaced, so no Docker daemon or worker is needed.
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
def _fresh_audit_log() -> None:
    Path(_CONFIG_DIR, "manager", "audit.log").unlink(missing_ok=True)


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
