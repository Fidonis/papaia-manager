"""The job queue's notion of "something is in flight", and who honours it.

`active_job()` is what the backup endpoint refuses on and what the UI keeps
saying across a page change. Before it existed each caller carried its own
version of the predicate, and the page carried none at all -- which is how a
running backup could be forgotten by a reload and started a second time.

The endpoint test goes through the real dependency stack: the guard is only
worth anything if the route actually reaches it.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.core.jobs import Job, JobQueue, JobStatus

_SESSION_SECRET = "test-session-secret-value"
_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-workspace-")
_BACKUP_DIR = tempfile.mkdtemp(prefix="papaia-backup-")

# The backup route resolves its target before it looks at the queue, so the
# config bundle has to name a directory that exists -- otherwise the 409 under
# test is masked by the 409 for an unusable backup location.
Path(_CONFIG_DIR, ".env").write_text(
    f"PAPAIA_HOST=https://papaia.test\nPAPAIA_BACKUP_DIR={_BACKUP_DIR}\n", encoding="utf-8"
)

os.environ.update(
    OIDC_ISSUER_KC_AUTH="https://kc.test/auth",
    OIDC_ISSUER_KC_TOKEN="https://kc.test/token",
    OIDC_ISSUER_KC_CERTS="https://kc.test/certs",
    MANAGER_ADMIN_ROLE="admin",
    MANAGER_USER_ROLE="user",
    MANAGER_HOST="http://localhost:8120",
    MANAGER_OIDC_CLIENT_SECRET="client-secret",
    MANAGER_SESSION_SECRET=_SESSION_SECRET,
    PAPAIA_CONFIG_DIR=_CONFIG_DIR,
    PAPAIA_WORKSPACE_DIR=_WORKSPACE_DIR,
)

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app import main as app_main  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.main import create_app  # noqa: E402

_CSRF = "test-csrf-token"


def _job(action: str, status: JobStatus, *, minutes_ago: int = 0) -> Job:
    created = datetime.now(tz=UTC) - timedelta(minutes=minutes_ago)
    job = Job(
        id=f"{action}-{status.value}-{minutes_ago}",
        action=action,
        target="/backup",
        user="tester",
        created_at=created,
        status=status,
    )
    if status in (JobStatus.SUCCEEDED, JobStatus.FAILED):
        job.started_at = created
        job.finished_at = created
    return job


def _queue_with(*jobs: Job) -> JobQueue:
    # Constructed, never started: these tests are about the registry, not the
    # worker, and a running worker would need an event loop of its own.
    queue = JobQueue(config_dir=_CONFIG_DIR)
    for job in jobs:
        queue._jobs[job.id] = job  # noqa: SLF001
    return queue


# ---------------------------------------------------------------------------
# active_job()
# ---------------------------------------------------------------------------


def test_an_idle_queue_has_no_active_job() -> None:
    assert _queue_with().active_job() is None


@pytest.mark.parametrize("status", [JobStatus.QUEUED, JobStatus.RUNNING])
def test_queued_counts_as_active_just_like_running(status: JobStatus) -> None:
    # A queued job is seconds away from holding the lock. Treating only `running`
    # as active is what let a second request slip in between the two states.
    queue = _queue_with(_job("backup", status))
    assert queue.active_job() is not None


@pytest.mark.parametrize("status", [JobStatus.SUCCEEDED, JobStatus.FAILED])
def test_a_finished_job_leaves_the_queue_idle(status: JobStatus) -> None:
    assert _queue_with(_job("backup", status)).active_job() is None


def test_the_oldest_unfinished_job_is_the_active_one() -> None:
    # Single-flight FIFO: the oldest one holds the lock, so its action is the
    # one worth naming in "a <...> job is already running".
    queue = _queue_with(
        _job("backup", JobStatus.QUEUED, minutes_ago=1),
        _job("addon-install", JobStatus.RUNNING, minutes_ago=5),
    )
    active = queue.active_job()
    assert active is not None
    assert active.action == "addon-install"


# ---------------------------------------------------------------------------
# The backup endpoint honours it
# ---------------------------------------------------------------------------


@pytest.fixture
def client() -> Iterator[TestClient]:
    get_settings.cache_clear()
    # No `with` block: the lifespan would start the real worker and hand the
    # routes a queue these tests want to control.
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


def _post_backup(client: TestClient) -> Any:
    return client.post(
        "/api/v1/maintenance/backup", json={}, headers={"X-CSRF-Token": _CSRF}
    )


def test_a_second_backup_is_refused_while_a_job_runs(client: TestClient) -> None:
    app_main._job_queue = _queue_with(_job("backup", JobStatus.RUNNING))  # noqa: SLF001
    response = _post_backup(_admin(client))
    assert response.status_code == 409
    assert "already running" in response.json()["detail"]


def test_the_refusal_names_the_job_that_is_in_the_way(client: TestClient) -> None:
    # An add-on install blocks a backup just as well -- the worker runs one job at
    # a time -- and the operator is told which one rather than being left to guess.
    app_main._job_queue = _queue_with(_job("addon-install", JobStatus.RUNNING))  # noqa: SLF001
    response = _post_backup(_admin(client))
    assert response.status_code == 409
    assert "addon-install" in response.json()["detail"]


def test_a_finished_backup_does_not_block_the_next_one(client: TestClient) -> None:
    app_main._job_queue = _queue_with(_job("backup", JobStatus.SUCCEEDED))  # noqa: SLF001
    # 202 means the guard let it through and the job was enqueued; the callback
    # only runs once a worker picks it up, which never happens here.
    assert _post_backup(_admin(client)).status_code == 202
