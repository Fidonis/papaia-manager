"""Audit coverage for the stack-wide runner routes: started, and clearing it.

The request guards in front of these routes are covered in `test_api_stack.py`.
This module only adds what M1 changed -- the runner is mocked out the same way
`test_api_upgrade_images.py` mocks the upgrade runner, so no Docker daemon or
job worker is needed.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_SESSION_SECRET = "test-session-secret-value"
_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-stack-audit-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-stack-audit-workspace-")

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
from app.core import runner  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers import api_stack  # noqa: E402

_CSRF = "test-csrf-token"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PAPAIA_CONFIG_DIR", _CONFIG_DIR)
    monkeypatch.setenv("PAPAIA_WORKSPACE_DIR", _WORKSPACE_DIR)
    get_settings.cache_clear()
    app_main._job_queue = None  # noqa: SLF001

    async def idle(kind: object = None) -> None:
        return None

    monkeypatch.setattr(api_stack.runner, "find_runner", idle)
    yield TestClient(create_app(), follow_redirects=False)
    get_settings.cache_clear()


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


def _post(client: TestClient, url: str, body: dict[str, Any] | None = None) -> Any:
    return _admin(client).post(url, json=body or {}, headers={"X-CSRF-Token": _CSRF})


def _audit() -> list[dict[str, Any]]:
    path = Path(_CONFIG_DIR, "manager", "audit.log")
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


@pytest.fixture(autouse=True)
def _fresh_audit_log() -> None:
    Path(_CONFIG_DIR, "manager", "audit.log").unlink(missing_ok=True)


def _status(*, running: bool) -> runner.RunnerStatus:
    return runner.RunnerStatus(
        name="papaia-stack-start",
        target="start",
        status="running" if running else "exited",
        exit_code=None if running else 0,
        started_at="2026-09-20T10:00:00Z",
        finished_at="",
    )


def test_starting_a_stack_action_is_audited_as_started(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def start(**kwargs: Any) -> runner.RunnerStatus:
        return _status(running=True)

    monkeypatch.setattr(api_stack.runner, "start_stack_action", start)

    response = _post(client, "/api/v1/stack/start")
    assert response.status_code == 202, response.text

    (entry,) = [e for e in _audit() if e["action"] == "stack-start"]
    assert entry["result"] == "started"


def test_clearing_a_finished_stack_runner_is_audited(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def find(kind: runner.RunnerKind) -> runner.RunnerStatus | None:
        return _status(running=False) if kind is runner.STACK_KIND else None

    cleared: list[str] = []

    async def clear(name: str, kind: runner.RunnerKind = runner.RESTORE_KIND) -> None:
        cleared.append(name)

    monkeypatch.setattr(api_stack.runner, "find_runner", find)
    monkeypatch.setattr(api_stack.runner, "clear_runner", clear)

    response = _post(client, "/api/v1/stack/runner/clear")
    assert response.status_code == 200
    assert cleared == ["papaia-stack-start"]

    (entry,) = [e for e in _audit() if e["action"] == "stack-runner-clear"]
    assert entry["target"] == "start"
