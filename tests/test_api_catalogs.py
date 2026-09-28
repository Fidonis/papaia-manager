"""The catalog CRUD/refresh audit trail.

The CRUD routes themselves are exercised elsewhere; what this module checks is
the side effect M1 adds -- an entry lands for every mutation, only the fields
this feature names are recorded, and a URL carrying git credentials never
reaches the log verbatim.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-catalogs-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-catalogs-workspace-")

# `setdefault`, not `update`: another test module may have imported first and
# pointed the process at its own directories. Every test here works through the
# dependency override below, so whose values win does not matter.
for _key, _value in {
    "OIDC_ISSUER_KC_AUTH": "https://kc.test/auth",
    "OIDC_ISSUER_KC_TOKEN": "https://kc.test/token",
    "OIDC_ISSUER_KC_CERTS": "https://kc.test/certs",
    "MANAGER_ADMIN_ROLE": "admin",
    "MANAGER_USER_ROLE": "user",
    "MANAGER_HOST": "http://localhost:8120",
    "MANAGER_OIDC_CLIENT_SECRET": "client-secret",
    "MANAGER_SESSION_SECRET": "test-session-secret-value",
    "PAPAIA_CONFIG_DIR": _CONFIG_DIR,
    "PAPAIA_WORKSPACE_DIR": _WORKSPACE_DIR,
}.items():
    os.environ.setdefault(_key, _value)

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app import main as app_main  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core.jobs import JobContext, JobQueue  # noqa: E402
from app.main import create_app  # noqa: E402

_CSRF = "test-csrf-token-value"


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    (directory / "manager").mkdir(parents=True)
    (directory / ".env").write_text("PAPAIA_HOST=https://papaia.test\n", encoding="utf-8")
    return directory


@pytest.fixture
def client(config_dir: Path) -> Iterator[TestClient]:
    get_settings.cache_clear()
    # Both overridden explicitly: another test module may have already pointed
    # the process env at its own workspace directory, and the local-catalog
    # refresh test needs one it actually controls.
    settings = get_settings().model_copy(
        update={"papaia_config_dir": str(config_dir), "papaia_workspace_dir": _WORKSPACE_DIR}
    )

    # No `with` block: skipping the lifespan keeps the job-queue worker thread
    # out of these tests, the same reason test_api_tiles.py skips it.
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    yield TestClient(app, follow_redirects=False)
    get_settings.cache_clear()
    app_main._job_queue = None  # noqa: SLF001


def _as(client: TestClient, *roles: str) -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": "u-1",
            "preferred_username": "tester",
            "roles": list(roles),
            "exp": int(time.time()) + 3600,
        },
        "_csrf_token": _CSRF,
    }
    payload = base64.b64encode(json.dumps(session).encode())
    signed = TimestampSigner(get_settings().manager_session_secret).sign(payload).decode()

    client.cookies.clear()
    client.cookies.set("papaia_manager_session", signed)
    return client


def _admin(client: TestClient) -> TestClient:
    return _as(client, "admin")


def _headers() -> dict[str, str]:
    return {"X-CSRF-Token": _CSRF}


def _entries(config_dir: Path) -> list[dict[str, Any]]:
    path = config_dir / "manager" / "audit.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _create(client: TestClient, **overrides: Any) -> Any:
    body = {"name": "acme", "type": "git", "url": "https://github.com/acme/catalog.git"}
    body.update(overrides)
    return _admin(client).post("/api/v1/catalogs", headers=_headers(), json=body)


def _run_callback(job_id: str) -> None:
    queue = app_main._job_queue  # noqa: SLF001
    assert queue is not None
    callback = queue._callbacks[job_id]  # noqa: SLF001
    queue.jobs_dir.mkdir(parents=True, exist_ok=True)
    job = queue.get_job(job_id)
    assert job is not None
    asyncio.run(callback(JobContext(job=job, log_path=queue.jobs_dir / f"{job_id}.log")))


# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------


def test_creating_a_catalog_is_audited(client: TestClient, config_dir: Path) -> None:
    response = _create(client)
    assert response.status_code == 201

    entry = _entries(config_dir)[-1]
    assert entry["action"] == "catalog-create"
    assert entry["target"] == "acme"
    assert entry["params"]["url"] == "https://github.com/acme/catalog.git"
    assert entry["params"]["type"] == "git"


def test_a_url_with_embedded_credentials_is_redacted(
    client: TestClient, config_dir: Path
) -> None:
    response = _create(
        client, url="https://x-access-token:ghp_supersecrettoken@github.com/acme/catalog.git"
    )
    assert response.status_code == 201

    entry = _entries(config_dir)[-1]
    assert "ghp_supersecrettoken" not in json.dumps(entry)
    assert entry["params"]["url"] == "https://***@github.com/acme/catalog.git"


# ---------------------------------------------------------------------------
# update
# ---------------------------------------------------------------------------


def test_updating_a_catalog_is_audited_with_only_the_changed_fields(
    client: TestClient, config_dir: Path
) -> None:
    _create(client)

    response = _admin(client).put(
        "/api/v1/catalogs/acme", headers=_headers(), json={"enabled": False}
    )
    assert response.status_code == 200

    entry = _entries(config_dir)[-1]
    assert entry["action"] == "catalog-update"
    assert entry["target"] == "acme"
    assert entry["params"] == {"enabled": False}


def test_an_updated_url_is_redacted_too(client: TestClient, config_dir: Path) -> None:
    _create(client)

    _admin(client).put(
        "/api/v1/catalogs/acme",
        headers=_headers(),
        json={"url": "https://user:s3cr3t@github.com/acme/catalog.git"},
    )

    entry = _entries(config_dir)[-1]
    assert entry["params"]["url"] == "https://***@github.com/acme/catalog.git"


# ---------------------------------------------------------------------------
# delete
# ---------------------------------------------------------------------------


def test_deleting_a_catalog_is_audited(client: TestClient, config_dir: Path) -> None:
    _create(client)

    response = _admin(client).delete("/api/v1/catalogs/acme", headers=_headers())
    assert response.status_code == 204

    entry = _entries(config_dir)[-1]
    assert entry["action"] == "catalog-delete"
    assert entry["target"] == "acme"


# ---------------------------------------------------------------------------
# refresh
# ---------------------------------------------------------------------------


def test_refreshing_a_local_catalog_is_audited_without_a_url(
    client: TestClient, config_dir: Path
) -> None:
    local_dir = Path(_WORKSPACE_DIR, "local-catalog")
    local_dir.mkdir(parents=True, exist_ok=True)
    _create(client, name="local", type="local", url=None, path=str(local_dir))
    app_main._job_queue = JobQueue(config_dir=str(config_dir))  # noqa: SLF001

    response = _admin(client).post("/api/v1/catalogs/local/refresh", headers=_headers())
    assert response.status_code == 202
    job_id = response.json()["job_id"]
    _run_callback(job_id)

    entry = _entries(config_dir)[-1]
    assert entry["action"] == "catalog-refresh"
    assert entry["target"] == "local"
    assert entry["job_id"] == job_id
    assert "params" not in entry
