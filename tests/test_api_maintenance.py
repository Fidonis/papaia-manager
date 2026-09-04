"""The delete-restore-points endpoint, and what it refuses.

Nothing here forks papaia-ctl. The queue is constructed but never started, so an
enqueued job stays queued and its callback is invoked by hand against a stubbed
`run_core_verb` -- the same shape as test_api_restore_scoped.py, which is how the
built argv gets asserted without a Docker daemon.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

_SESSION_SECRET = "test-session-secret-value"
_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-workspace-")
_BACKUP_DIR = tempfile.mkdtemp(prefix="papaia-backup-")

_POINTS = ("2026-07-30_10-19-38", "2026-07-31_11-00-00", "2026-08-01_12-30-00")

Path(_CONFIG_DIR, ".env").write_text(
    f"PAPAIA_HOST=https://papaia.test\nPAPAIA_BACKUP_DIR={_BACKUP_DIR}\n",
    encoding="utf-8",
)


def _seed_catalogue() -> None:
    for point in _POINTS:
        Path(_BACKUP_DIR, point).mkdir(parents=True, exist_ok=True)
    Path(_BACKUP_DIR, "backup.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "backups": [
                    {
                        "id": point,
                        "path": str(Path(_BACKUP_DIR, point)),
                        "created_at": f"2026-07-30T0{i}:00:00Z",
                        "papaia_version": "1.2.0",
                        "project": "papaia",
                        "size_mb": 12.0,
                        "result": "ok",
                        "artifacts": 3,
                        "addons": [],
                    }
                    for i, point in enumerate(_POINTS)
                ],
            }
        ),
        encoding="utf-8",
    )


_seed_catalogue()

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
from app.core.jobs import JobContext, JobQueue  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers import api_maintenance  # noqa: E402

_CSRF = "test-csrf-token"
_URL = "/api/v1/maintenance/restore-points/delete"


async def _no_runner(kind: object = None) -> None:
    return None


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PAPAIA_CONFIG_DIR", _CONFIG_DIR)
    monkeypatch.setenv("PAPAIA_WORKSPACE_DIR", _WORKSPACE_DIR)
    get_settings.cache_clear()
    _seed_catalogue()
    app_main._job_queue = JobQueue(config_dir=_CONFIG_DIR)  # noqa: SLF001
    monkeypatch.setattr(api_maintenance.runner, "find_runner", _no_runner)
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


def _delete(client: TestClient, ids: list[str]) -> Any:
    return _admin(client).post(
        _URL, json={"restore_points": ids}, headers={"X-CSRF-Token": _CSRF}
    )


def _run_callback(job_id: str) -> None:
    queue = app_main._job_queue  # noqa: SLF001
    assert queue is not None
    callback = queue._callbacks[job_id]  # noqa: SLF001
    queue.jobs_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(
        callback(
            JobContext(
                job=queue.get_job(job_id),
                log_path=queue.jobs_dir / f"{job_id}.log",
            )
        )
    )


# ---------------------------------------------------------------------------
# the restore-point list markup
# ---------------------------------------------------------------------------


def test_the_selection_checkbox_binding_is_well_formed(client: TestClient) -> None:
    """`{{ p.id | tojson }}` emits its own double quotes and is marked safe, so
    inside a double-quoted `:checked` / `@change` it closes the attribute early
    and the checkbox never wires up to Alpine -- which silently disables the
    bulk-delete bar. The id must be single-quoted instead."""
    html = _admin(client).get("/partials/backup/restore-points").text
    assert 'sel.includes("' not in html
    assert f":checked=\"sel.includes('{_POINTS[0]}')\"" in html
    assert f"@change.prevent=\"toggle('{_POINTS[0]}')\"" in html


def test_the_backup_page_carries_the_bulk_delete_scaffolding(client: TestClient) -> None:
    html = _admin(client).get("/backup").text
    assert 'id="backup-selection"' in html
    assert 'x-data="backupSelection()"' in html
    assert 'x-show="sel.length"' in html
    assert 'id="delete-backup-modal"' in html


# ---------------------------------------------------------------------------
# happy path + argv
# ---------------------------------------------------------------------------


def test_deleting_one_point_is_queued_as_a_job(client: TestClient) -> None:
    response = _delete(client, [_POINTS[0]])
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"

    queue = app_main._job_queue  # noqa: SLF001
    assert queue is not None
    job = queue.get_job(body["job_id"])
    assert job is not None
    assert job.action == "backup-delete"
    assert job.target == _POINTS[0]
    assert job.params["restore_points"] == [_POINTS[0]]


def test_the_built_command_is_backup_delete_with_one_flag_per_id(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen: dict[str, Any] = {}

    async def _fake_run_core_verb(**kwargs: Any) -> AsyncGenerator[str, None]:
        seen.update(kwargs)

        async def _lines() -> AsyncGenerator[str, None]:
            yield f"[ok] Deleted 2 restore point(s) from {_BACKUP_DIR}"

        return _lines()

    monkeypatch.setattr(api_maintenance, "run_core_verb", _fake_run_core_verb)

    body = _delete(client, [_POINTS[1], _POINTS[0]]).json()
    _run_callback(body["job_id"])

    assert seen["verb"] == "backup-delete"
    flags = seen["extra_flags"]
    assert f"--backup-dir={_BACKUP_DIR}" in flags
    # De-duplicated and order-preserving: the ids reach papaia-ctl as sent.
    assert flags.count("--restore-point=" + _POINTS[1]) == 1
    assert f"--restore-point={_POINTS[0]}" in flags
    assert flags[-1] == "-y"


def test_deleting_several_points_records_them_all_on_the_job(client: TestClient) -> None:
    body = _delete(client, [_POINTS[0], _POINTS[2], _POINTS[0]]).json()
    queue = app_main._job_queue  # noqa: SLF001
    assert queue is not None
    job = queue.get_job(body["job_id"])
    assert job is not None
    assert job.params["restore_points"] == [_POINTS[0], _POINTS[2]]


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad", ["../etc", "nope", "2026-07-30", "2026-7-1_1-1-1", "--restart-clean"]
)
def test_a_malformed_id_is_a_400_and_never_reaches_the_queue(
    client: TestClient, bad: str
) -> None:
    response = _delete(client, [bad])
    assert response.status_code == 400
    queue = app_main._job_queue  # noqa: SLF001
    assert queue is not None
    assert queue.active_job() is None


def test_a_well_formed_unknown_id_is_a_404(client: TestClient) -> None:
    response = _delete(client, ["2020-01-01_00-00-00"])
    assert response.status_code == 404


def test_a_mix_of_known_and_unknown_ids_is_a_404(client: TestClient) -> None:
    response = _delete(client, [_POINTS[0], "2020-01-01_00-00-00"])
    assert response.status_code == 404
    assert "2020-01-01_00-00-00" in response.json()["detail"]


def test_an_empty_list_is_rejected_by_the_schema(client: TestClient) -> None:
    response = _admin(client).post(
        _URL, json={"restore_points": []}, headers={"X-CSRF-Token": _CSRF}
    )
    assert response.status_code == 422


def test_too_many_ids_are_rejected_by_the_schema(client: TestClient) -> None:
    many = [f"2026-07-30_10-19-{i:02d}" for i in range(api_maintenance.ctl.MAX_SELECTORS + 1)]
    response = _admin(client).post(
        _URL, json={"restore_points": many}, headers={"X-CSRF-Token": _CSRF}
    )
    assert response.status_code == 422


def test_delete_needs_a_csrf_token(client: TestClient) -> None:
    response = _admin(client).post(_URL, json={"restore_points": [_POINTS[0]]})
    assert response.status_code == 403


def test_delete_needs_an_admin(client: TestClient) -> None:
    client.cookies.clear()
    response = client.post(
        _URL, json={"restore_points": [_POINTS[0]]}, headers={"X-CSRF-Token": _CSRF}
    )
    assert response.status_code in (401, 403)


def test_delete_is_refused_while_a_job_runs(client: TestClient) -> None:
    admin = _admin(client)
    assert _delete(client, [_POINTS[0]]).status_code == 202
    # The first job is still queued; a queued job holds the lock like a running
    # one, exactly as a second backup would be refused.
    second = admin.post(
        _URL, json={"restore_points": [_POINTS[1]]}, headers={"X-CSRF-Token": _CSRF}
    )
    assert second.status_code == 409


def test_delete_is_refused_while_a_restore_runner_is_active(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Runner:
        is_running = True
        target = "2026-01-01_00-00-00"

    async def _restore_running(kind: object = None) -> object:
        return _Runner() if kind == api_maintenance.runner.RESTORE_KIND else None

    monkeypatch.setattr(api_maintenance.runner, "find_runner", _restore_running)
    response = _delete(client, [_POINTS[0]])
    assert response.status_code == 409
