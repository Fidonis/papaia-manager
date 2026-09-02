"""The scoped restore endpoint, and what it refuses.

Nothing here forks papaia-ctl. The queue is constructed but never started, so
an enqueued job stays queued and its callback is invoked by hand against a
stubbed `run_core_verb` -- which is how the built argv gets asserted without a
Docker daemon, the same way test_runner.py does it for the detached restore.

The refusals carry the weight. A scoped restore is the one restore that runs as
a child of this process, and it is only safe because it can never replace
`$PAPAIA_CONFIG_DIR` -- so every path that would let it is a test.
"""
from __future__ import annotations

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

_POINT = "2026-07-30_10-19-38"
_V1_POINT = "2026-07-29_09-00-00"

Path(_CONFIG_DIR, ".env").write_text(
    f"PAPAIA_HOST=https://papaia.test\n"
    f"PAPAIA_BACKUP_DIR={_BACKUP_DIR}\n"
    f"COMPOSE_PROFILES=keycloak,librechat,manager\n",
    encoding="utf-8",
)

# A workspace the inventory can read, so the selectors payload carries the
# target state the impact preview is drawn from.
_SRC = Path(_WORKSPACE_DIR, "papaia", "src")
(_SRC / "ai" / "librechat").mkdir(parents=True)
(_SRC / "docker-compose.yml").write_text(
    "include:\n  - path: ./ai/librechat/docker-compose.yml\n", encoding="utf-8"
)
(_SRC / "ai" / "librechat" / "docker-compose.yml").write_text(
    yaml.safe_dump(
        {
            "services": {
                "librechat": {
                    "image": "stub",
                    "profiles": ["librechat"],
                    "labels": {"de.fidonis.module": "papaia-librechat"},
                },
                "keycloak": {
                    "image": "stub",
                    "profiles": ["keycloak"],
                    "labels": {"de.fidonis.module": "papaia-keycloak"},
                },
                "papaia-manager": {
                    "image": "stub",
                    "profiles": ["manager"],
                    "labels": {"de.fidonis.module": "papaia-manager"},
                },
            }
        }
    ),
    encoding="utf-8",
)


def _artifact(kind, archive, target, *, owner="core", module="", profiles=None):
    return {
        "kind": kind,
        "archive": archive,
        "target": target,
        "owner": owner,
        "project": "papaia",
        "module": module,
        "services": [],
        "profiles": profiles or [],
    }


def _write_snapshot() -> None:
    for point in (_POINT, _V1_POINT):
        Path(_BACKUP_DIR, point).mkdir(parents=True, exist_ok=True)
    Path(_BACKUP_DIR, "backup.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "backups": [
                    {
                        "id": point,
                        "path": str(Path(_BACKUP_DIR, point)),
                        "created_at": created,
                        "papaia_version": "1.1.0",
                        "project": "papaia",
                        "size_mb": 24.3,
                        "result": "ok",
                        "artifacts": 4,
                        "addons": ["paperless"],
                    }
                    for point, created in (
                        (_POINT, "2026-07-30T08:20:33Z"),
                        (_V1_POINT, "2026-07-29T07:00:00Z"),
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    Path(_BACKUP_DIR, _POINT, "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 2,
                "id": _POINT,
                "core_project": "papaia",
                "artifacts": [
                    _artifact("configdir", "papaia-config.tar.gz", "/srv/papaia-config"),
                    _artifact(
                        "volume", "volumes/papaia_keycloak-postgresql.tar.gz",
                        "papaia_keycloak-postgresql",
                        module="keycloak", profiles=["keycloak"],
                    ),
                    _artifact(
                        "volume", "volumes/papaia_librechat-mongodb.tar.gz",
                        "papaia_librechat-mongodb",
                        module="librechat", profiles=["librechat"],
                    ),
                    _artifact(
                        "volume", "volumes/paperless-dir_paperless-data.tar.gz",
                        "paperless-dir_paperless-data",
                        owner="addon:paperless", module="paperless",
                    ),
                ],
            }
        ),
        encoding="utf-8",
    )
    # The same snapshot as an older core wrote it: no grouping at all.
    Path(_BACKUP_DIR, _V1_POINT, "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "id": _V1_POINT,
                "core_project": "papaia",
                "artifacts": [
                    {
                        "kind": "volume",
                        "archive": "volumes/papaia_librechat-mongodb.tar.gz",
                        "target": "papaia_librechat-mongodb",
                        "owner": "core",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )


_write_snapshot()

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
# PAPAIA_CONFIG_DIR and PAPAIA_WORKSPACE_DIR are deliberately *not* set here.
# Several test modules point them at their own temp directories at import time,
# and the last module imported would win for every module -- so this one sets
# them per test, where monkeypatch also puts them back afterwards.

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app import main as app_main  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core.jobs import JobQueue  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers import api_maintenance  # noqa: E402

_CSRF = "test-csrf-token"
_SCOPED = "/api/v1/maintenance/restore/scoped"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PAPAIA_CONFIG_DIR", _CONFIG_DIR)
    monkeypatch.setenv("PAPAIA_WORKSPACE_DIR", _WORKSPACE_DIR)
    get_settings.cache_clear()
    # Constructed, never started: enqueue only puts an id on a queue, so a job
    # stays queued and its callback is ours to invoke.
    app_main._job_queue = JobQueue(config_dir=_CONFIG_DIR)  # noqa: SLF001
    # No runner exists, and asking Docker about one would need a daemon.
    monkeypatch.setattr(api_maintenance.runner, "find_runner", _no_runner)
    yield TestClient(create_app(), follow_redirects=False)
    get_settings.cache_clear()
    app_main._job_queue = None  # noqa: SLF001


async def _no_runner() -> None:
    return None


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


def _post(client: TestClient, only: list[str], point: str = _POINT) -> Any:
    return _admin(client).post(
        _SCOPED,
        json={"restore_point": point, "only": only},
        headers={"X-CSRF-Token": _CSRF},
    )


# ---------------------------------------------------------------------------
# selectors endpoint
# ---------------------------------------------------------------------------


def test_the_selectors_endpoint_reports_what_a_snapshot_offers(client: TestClient) -> None:
    response = _admin(client).get(f"/api/v1/maintenance/restore-points/{_POINT}/selectors")
    assert response.status_code == 200
    body = response.json()
    assert body["supported"] is True
    assert {g["selector"] for g in body["groups"]} == {
        "config", "module:keycloak", "module:librechat", "addon:paperless"
    }
    assert body["notes"]


def test_the_payload_carries_the_target_state_for_the_impact_preview(
    client: TestClient,
) -> None:
    # The wizard has to say what keeps running while checkboxes are ticked, so
    # the mapping is handed over once instead of a round trip per click.
    body = _admin(client).get(
        f"/api/v1/maintenance/restore-points/{_POINT}/selectors"
    ).json()
    services = {s["service"]: s["profiles"] for s in body["services"]}
    assert services["librechat"] == ["librechat"]
    assert "papaia-manager" in services


def test_an_older_snapshot_offers_nothing_below_the_whole_point(
    client: TestClient,
) -> None:
    # It carries no grouping, so `supported` is false -- but it has no configdir
    # artifact either, so there is nothing at all to list.
    body = _admin(client).get(
        f"/api/v1/maintenance/restore-points/{_V1_POINT}/selectors"
    ).json()
    assert body["supported"] is False
    assert body["groups"] == []


def test_selectors_for_an_unknown_restore_point_are_a_404(client: TestClient) -> None:
    response = _admin(client).get(
        "/api/v1/maintenance/restore-points/2020-01-01_00-00-00/selectors"
    )
    assert response.status_code == 404


def test_selectors_need_an_admin(client: TestClient) -> None:
    client.cookies.clear()
    response = client.get(f"/api/v1/maintenance/restore-points/{_POINT}/selectors")
    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# refusals
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "selector",
    [
        "librechat",
        "profile:librechat",
        "module:../etc",
        "module:-y",
        "module:--restart-clean",
        "volume:../../etc/passwd",
        "volume:a b",
        "module:manager",
    ],
)
def test_a_malformed_or_forbidden_selector_is_a_400(
    client: TestClient, selector: str
) -> None:
    assert _post(client, [selector]).status_code == 400


def test_a_selector_absent_from_the_snapshot_is_a_400_not_a_500(
    client: TestClient,
) -> None:
    response = _post(client, ["module:litellm"])
    assert response.status_code == 400
    assert "not part of this restore point" in response.json()["detail"]


def test_an_empty_selection_is_rejected_by_the_schema(client: TestClient) -> None:
    response = _admin(client).post(
        _SCOPED,
        json={"restore_point": _POINT, "only": []},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert response.status_code == 422


def test_a_selection_needing_the_configuration_is_sent_to_the_full_restore(
    client: TestClient,
) -> None:
    """Keycloak's realm carries every client secret and is imported only on
    first start, so its database cannot be restored on its own. That makes it a
    whole-stack operation, which this endpoint must not attempt."""
    response = _post(client, ["module:keycloak"])
    assert response.status_code == 409
    assert "full restore" in response.json()["detail"]


def test_the_configuration_selector_itself_is_sent_to_the_full_restore(
    client: TestClient,
) -> None:
    response = _post(client, ["config"])
    assert response.status_code == 409


def test_an_older_snapshot_cannot_be_restored_in_part(client: TestClient) -> None:
    response = _post(client, ["module:librechat"], point=_V1_POINT)
    assert response.status_code == 409
    assert "as a whole" in response.json()["detail"]


def test_an_unknown_restore_point_is_a_404(client: TestClient) -> None:
    assert _post(client, ["module:librechat"], point="2020-01-01_00-00-00").status_code == 404


def test_a_scoped_restore_is_refused_while_a_job_runs(client: TestClient) -> None:
    admin = _admin(client)
    assert _post(client, ["module:librechat"]).status_code == 202
    # The first one is still queued, and a queued job holds the lock as much as
    # a running one does.
    second = admin.post(
        _SCOPED,
        json={"restore_point": _POINT, "only": ["addon:paperless"]},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert second.status_code == 409


def test_a_scoped_restore_needs_a_csrf_token(client: TestClient) -> None:
    response = _admin(client).post(
        _SCOPED, json={"restore_point": _POINT, "only": ["module:librechat"]}
    )
    assert response.status_code == 403


def test_a_scoped_restore_needs_an_admin(client: TestClient) -> None:
    client.cookies.clear()
    response = client.post(
        _SCOPED,
        json={"restore_point": _POINT, "only": ["module:librechat"]},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert response.status_code in (401, 403)


# ---------------------------------------------------------------------------
# the happy path, and the argv it builds
# ---------------------------------------------------------------------------


def test_a_scoped_restore_is_queued_as_an_ordinary_job(client: TestClient) -> None:
    response = _post(client, ["module:librechat", "addon:paperless"])
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    queue = app_main._job_queue  # noqa: SLF001
    assert queue is not None
    job = queue.get_job(body["job_id"])
    assert job is not None
    assert job.action == "restore-scoped"
    assert job.target == _POINT
    assert job.params["only"] == ["addon:paperless", "module:librechat"]


def test_the_built_command_is_the_scoped_verb_and_never_clears_volumes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The argv is the whole safety argument: a different verb, a mandatory
    --only, and no --restart-clean anywhere."""
    seen: dict[str, Any] = {}

    async def _fake_run_core_verb(**kwargs: Any) -> AsyncGenerator[str, None]:
        seen.update(kwargs)

        async def _lines() -> AsyncGenerator[str, None]:
            yield "RESTORE-STEP\tartifact\tpapaia_librechat-mongodb\tok"

        return _lines()

    monkeypatch.setattr(api_maintenance, "run_core_verb", _fake_run_core_verb)

    body = _post(client, ["module:librechat"]).json()
    queue = app_main._job_queue  # noqa: SLF001
    callback = queue._callbacks[body["job_id"]]  # noqa: SLF001

    import asyncio

    from app.core.jobs import JobContext

    queue.jobs_dir.mkdir(parents=True, exist_ok=True)
    asyncio.run(
        callback(
            JobContext(
                job=queue.get_job(body["job_id"]),
                log_path=queue.jobs_dir / f"{body['job_id']}.log",
            )
        )
    )

    assert seen["verb"] == "restore-scoped"
    flags = seen["extra_flags"]
    assert "--only=module:librechat" in flags
    assert f"--restore-point={_POINT}" in flags
    assert "-y" in flags
    assert not any("restart-clean" in flag for flag in flags)
