"""`POST /api/v1/addons/{name}/actions/{action}` runs a maintenance action the add-on declares.

Only a declared action of an installed, active add-on is accepted, and the request carries the
action name only: papaia-ctl looks it up in the manifest again and runs it. The queue and
`run_addon_action` are replaced, so no Docker daemon or worker is needed.
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
_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-addon-actions-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-addon-actions-workspace-")

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
from app.core.snapshots import record_installed  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers import api_addons  # noqa: E402
from app.templating import templates  # noqa: E402

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
    Path(_CONFIG_DIR, "installed.yaml").unlink(missing_ok=True)


_MANIFEST = {
    "name": "n8n",
    "version": "1.0.0",
    "actions": {
        "reset": {
            "service": "job",
            "args": ["--reset"],
            "description": "Reset",
            "destructive": True,
        },
        "check": {"service": "job", "description": "Self-check"},
    },
}


def _install(path: Path, *, active: bool = True) -> None:
    """Register `n8n` with the manifest above, installed and (by default) active."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "papaia-app.yaml").write_text(yaml.safe_dump(_MANIFEST), encoding="utf-8")
    Path(_CONFIG_DIR, "deployment.yaml").write_text(
        yaml.safe_dump({"addons": [{"name": "n8n", "path": str(path), "active": active}]}),
        encoding="utf-8",
    )
    record_installed(_CONFIG_DIR, name="n8n", catalog="cat", commit="abc", manifest_version="1.0.0")


@pytest.fixture
def addon_dir(tmp_path: Path) -> Path:
    path = tmp_path / "n8n"
    _install(path)
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


def _run(client: TestClient, action: str, *, role: str = "admin", csrf: str | None = _CSRF) -> Any:
    headers = {"X-CSRF-Token": csrf} if csrf else {}
    return _as(client, role).post(f"/api/v1/addons/n8n/actions/{action}", json={}, headers=headers)


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
            action="addon_action",
            target="n8n",
            user="tester",
            created_at=datetime.now(tz=UTC),
        ),
        log_path=log_path,
    )
    asyncio.run(entry["callback"](ctx))


def _record_actions(
    monkeypatch: pytest.MonkeyPatch, *, failing: bool = False
) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    async def run_addon_action(*, name: str, action: str, **_: Any) -> AsyncIterator[str]:
        calls.append((name, action))

        async def lines() -> AsyncIterator[str]:
            yield f"{action} output"
            if failing:
                raise CtlError(f"papaia-ctl addon run {name} {action} failed", 3)

        return lines()

    monkeypatch.setattr(api_addons, "run_addon_action", run_addon_action)
    return calls


@pytest.mark.usefixtures("addon_dir")
def test_a_declared_action_runs_by_name_and_is_audited(
    client: TestClient, queue: _FakeQueue, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _record_actions(monkeypatch)

    response = _run(client, "reset")
    assert response.status_code == 202, response.text
    assert queue.enqueued[0]["action"] == "addon_action"
    _run_callback(queue, tmp_path / "job.log")

    assert calls == [("n8n", "reset")]
    log = (tmp_path / "job.log").read_text(encoding="utf-8")
    assert "papaia-ctl addon run n8n reset" in log
    assert "reset output" in log
    (entry,) = _audit()
    assert entry["action"] == "addon_action"
    assert entry["target"] == "n8n"
    assert entry["params"] == {"action": "reset"}


@pytest.mark.usefixtures("addon_dir")
def test_an_undeclared_action_is_refused_and_nothing_runs(
    client: TestClient, queue: _FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_actions(monkeypatch)

    for action in ("purge", "reset;rm", "..%2Fx"):
        response = _run(client, action)
        assert response.status_code in (404, 405), (action, response.text)

    assert queue.enqueued == []
    assert calls == []


def test_an_inactive_addon_offers_no_action(
    client: TestClient, queue: _FakeQueue, tmp_path: Path
) -> None:
    _install(tmp_path / "n8n", active=False)

    assert _run(client, "reset").status_code == 404
    assert queue.enqueued == []


@pytest.mark.usefixtures("addon_dir")
def test_a_failing_action_fails_the_job_and_writes_no_audit_entry(
    client: TestClient, queue: _FakeQueue, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _record_actions(monkeypatch, failing=True)

    assert _run(client, "check").status_code == 202
    with pytest.raises(CtlError) as raised:
        _run_callback(queue, tmp_path / "job.log")

    assert raised.value.exit_code == 3
    assert _audit() == []


@pytest.mark.usefixtures("addon_dir")
def test_actions_need_the_admin_role_and_the_csrf_token(
    client: TestClient, queue: _FakeQueue
) -> None:
    assert _run(client, "check", role="user").status_code == 403
    assert _run(client, "check", csrf=None).status_code == 403
    assert queue.enqueued == []


@pytest.mark.usefixtures("addon_dir")
def test_actions_are_refused_while_the_stack_is_locked(
    client: TestClient, queue: _FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.core.nodes.lock_reason", lambda config_dir, workspace_dir: "an upgrade is running"
    )

    assert _run(client, "check").status_code == 409
    assert queue.enqueued == []


def test_the_page_lists_the_actions_and_marks_destructive_ones() -> None:
    html = templates.env.get_template("partials/addon_detail_content.html").render(
        addon={
            "name": "n8n",
            "status": "running",
            "actions": [
                {"name": "check", "description": "Self-check", "destructive": False},
                {"name": "reset", "description": "Reset", "destructive": True},
            ],
        },
        request=None,
    )
    assert "Maintenance actions" in html
    assert "actions/reset" in html
    assert "btn-error" in html
    assert "confirm(" in html
