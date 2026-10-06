"""The job management API: who may call it, what it answers, and what it never says.

Through the real application against a fake ingester that reads the real `jobs.yaml`. The
service rules are tested one level down; here it is the wire: the tier of every route, the CSRF
check on every change, the status code of every refusal, the field-by-field problems a refused
job carries, and that a credential never comes back.
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
import yaml

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-api-jobs-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-api-jobs-workspace-")

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

from app.config import get_settings  # noqa: E402
from app.core.audit import audit_path  # noqa: E402
from app.core.ingest import catalog, runs  # noqa: E402
from app.core.vectordb.ingest_file import CONNECTIONS_RELPATH  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers.rag_deps import get_http_transport  # noqa: E402
from tests.fake_ingest import TOKEN, FakeIngest  # noqa: E402
from tests.fake_qdrant import Fleet  # noqa: E402

_CSRF = "test-csrf-token-value"
_CSRF_HEADER = {"X-CSRF-Token": _CSRF}
_BASE = "/api/v1/rag/ingest"
VALUE = "s3cr3t-value-that-must-never-leak"


class Deployment:
    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self.ingest = FakeIngest(config_dir)
        self.fleet = Fleet()
        self.fleet.add("qdrant-ingest", self.ingest)

    @property
    def jobs_path(self) -> Path:
        return self.config_dir / catalog.JOBS_RELPATH

    def write_catalog(self, *jobs: dict[str, Any]) -> None:
        self.jobs_path.write_text(
            yaml.safe_dump({"version": 1, "jobs": list(jobs)}, sort_keys=False), encoding="utf-8"
        )
        self.ingest.load_catalog()

    def file_jobs(self) -> list[dict[str, Any]]:
        return yaml.safe_load(self.jobs_path.read_text(encoding="utf-8"))["jobs"]

    def audit_text(self) -> str:
        path = audit_path(str(self.config_dir))
        return path.read_text(encoding="utf-8") if path.exists() else ""


@pytest.fixture
def deployment(tmp_path: Path) -> Deployment:
    config_dir = tmp_path / "config"
    (config_dir / "manager").mkdir(parents=True)
    (config_dir / CONNECTIONS_RELPATH.parent).mkdir(parents=True, exist_ok=True)
    (config_dir / ".env").write_text("COMPOSE_PROFILES=keycloak,rag\n", encoding="utf-8")
    (config_dir / "ai" / "rag" / ".env").write_text(
        f"QI_CONNECTIONS_SECRET=s\nQI_API_TOKEN={TOKEN}\nQI_TIMEZONE=Europe/Berlin\n",
        encoding="utf-8",
    )
    (config_dir / CONNECTIONS_RELPATH).write_text(
        yaml.safe_dump({"version": 1, "connections": [{"name": "default", "url": "http://q:6333"}]}),
        encoding="utf-8",
    )
    return Deployment(config_dir)


@pytest.fixture
def client(deployment: Deployment, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    async def idle(_kind: object) -> None:
        return None

    monkeypatch.setattr(runs.runner, "find_runner", idle)
    get_settings.cache_clear()
    settings = get_settings().model_copy(
        update={
            "papaia_config_dir": str(deployment.config_dir),
            "qdrant_ingest_url": "http://qdrant-ingest:8300",
        }
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_http_transport] = lambda: deployment.fleet.transport()
    yield TestClient(app, follow_redirects=False)
    get_settings.cache_clear()


def _as(client: TestClient, *roles: str) -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": "u-1",
            "preferred_username": "Tester",
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


def _state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "id": "handbook",
        "enabled": True,
        "description": "",
        "source": {"type": "local", "label": "handbook", "path": "/data/local/handbook"},
        "target": {"collection": "kb", "connection": "default"},
        "mode": "upsert",
        "schedule": {"mode": "manual"},
        "embedding": {"model": "nomic-embed-text"},
    }
    state.update(overrides)
    return state


def _authored(**overrides: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "id": "handbook",
        "source": {"type": "local", "label": "handbook", "path": "/data/local/handbook"},
        "target": {"collection": "kb", "connection": "default"},
        "mode": "upsert",
        "embedding": {"model": "nomic-embed-text"},
    }
    job.update(overrides)
    return job


# ---------------------------------------------------------------------------
# Who may call what
# ---------------------------------------------------------------------------

_READS = [
    f"{_BASE}/ingester",
    f"{_BASE}/form-spec",
    f"{_BASE}/jobs",
    f"{_BASE}/jobs/handbook/editor",
    f"{_BASE}/jobs/handbook/files",
    f"{_BASE}/jobs/handbook/preview",
    f"{_BASE}/jobs/handbook/runs",
    f"{_BASE}/job-runs",
    f"{_BASE}/job-runs/some-run",
    f"{_BASE}/orphans",
    f"{_BASE}/secrets",
    f"{_BASE}/catalog/raw",
    f"{_BASE}/catalog/defaults",
]
_WRITES = [
    ("post", f"{_BASE}/reload", None),
    ("post", f"{_BASE}/schedule/preview", {"schedule": {"mode": "manual"}}),
    ("post", f"{_BASE}/jobs/validate", {"job": {}}),
    ("post", f"{_BASE}/jobs", {"job": _state()}),
    ("put", f"{_BASE}/jobs/handbook", {"job": _state()}),
    ("delete", f"{_BASE}/jobs/handbook", None),
    ("post", f"{_BASE}/jobs/handbook/enable", None),
    ("post", f"{_BASE}/jobs/handbook/disable", None),
    ("post", f"{_BASE}/jobs/handbook/run", {}),
    ("delete", f"{_BASE}/job-runs/some-run", None),
    ("delete", f"{_BASE}/orphans/gone", None),
    ("put", f"{_BASE}/secrets/DAV", {"value": "x"}),
    ("delete", f"{_BASE}/secrets/DAV", None),
    ("post", f"{_BASE}/catalog/validate", {"text": ""}),
    ("put", f"{_BASE}/catalog/raw", {"text": "", "revision": ""}),
    ("put", f"{_BASE}/catalog/defaults", {"defaults": {}}),
]


@pytest.mark.parametrize("path", _READS)
def test_every_read_needs_an_administrator(client: TestClient, path: str) -> None:
    assert client.get(path).status_code == 401
    assert _as(client, "user").get(path).status_code == 403
    assert _as(client).get(path).status_code == 403


@pytest.mark.parametrize(("method", "path", "body"), _WRITES)
def test_every_change_needs_an_administrator_and_the_csrf_token(
    client: TestClient, method: str, path: str, body: Any
) -> None:
    send = getattr(client, method)
    kwargs: dict[str, Any] = {} if body is None else {"json": body}
    assert send(path, **kwargs).status_code == 401
    as_user = _as(client, "user")
    assert as_user.request(method, path, headers=_CSRF_HEADER, **kwargs).status_code == 403
    # An administrator without the token is refused before the handler does anything.
    assert _admin(client).request(method, path, **kwargs).status_code == 403


def test_the_pages_are_not_there_when_the_rag_system_is_off(
    client: TestClient, deployment: Deployment
) -> None:
    (deployment.config_dir / ".env").write_text("COMPOSE_PROFILES=keycloak\n", encoding="utf-8")

    assert _admin(client).get(f"{_BASE}/jobs").status_code == 404


# ---------------------------------------------------------------------------
# The list and the editor's inputs
# ---------------------------------------------------------------------------


def test_the_list_merges_the_file_and_the_ingester(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_authored(schedule={"cron": "0 3 * * 1"}))

    body = _admin(client).get(f"{_BASE}/jobs").json()

    assert body["editable"] and body["ingester"]["usable"]
    assert "run_progress" in body["ingester"]["features"]
    [job] = body["jobs"]
    assert (job["id"], job["state"], job["schedule"]) == (
        "handbook",
        "loaded",
        "Every Tuesday at 03:00",
    )
    assert job["etag"] and job["runnable"] and job["source_detail"] == "/data/local/handbook"


def test_a_down_ingester_is_a_state_of_the_page_not_an_error(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_authored())
    deployment.ingest.down = True

    response = _admin(client).get(f"{_BASE}/jobs")

    assert response.status_code == 200
    body = response.json()
    assert not body["ingester"]["reachable"] and body["jobs"][0]["state"] == "unknown"
    assert _admin(client).get(f"{_BASE}/ingester").json()["reachable"] is False


def test_the_form_spec_describes_every_source_type(client: TestClient) -> None:
    body = _admin(client).get(f"{_BASE}/form-spec").json()

    assert len(body["source_types"]) == 9
    s3 = next(s for s in body["sources"] if s["type"] == "s3")
    assert {"bucket", "access_key_id", "secret_access_key"} <= {f["key"] for f in s3["fields"]}
    assert body["modes"] == ["append", "upsert", "full"]


def test_the_editor_opens_a_blank_job_with_the_ingesters_timezone(client: TestClient) -> None:
    body = _admin(client).get(f"{_BASE}/jobs/new/editor").json()

    assert body["state"]["id"] == "" and body["etag"] == ""
    assert body["timezone"] == "Europe/Berlin"
    assert body["state"]["schedule"]["mode"] == "manual"


def test_the_editor_opens_an_existing_job_with_its_etag(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_authored(description="Docs"))

    body = _admin(client).get(f"{_BASE}/jobs/handbook/editor").json()

    assert body["state"]["description"] == "Docs" and body["etag"]
    assert _admin(client).get(f"{_BASE}/jobs/nope/editor").status_code == 404


def test_the_schedule_preview_answers_in_words_and_with_runs(client: TestClient) -> None:
    body = _admin(client).post(
        f"{_BASE}/schedule/preview",
        headers=_CSRF_HEADER,
        json={"schedule": {"mode": "weekly", "time": "08:30", "weekdays": ["mon", "fri"]}},
    ).json()

    assert body["ok"] and body["description"] == "Every Monday, Friday at 08:30"
    assert body["cron"] == "30 8 * * mon,fri"
    assert body["timezone"] == "Europe/Berlin" and len(body["runs"]) == 3

    bad = _admin(client).post(
        f"{_BASE}/schedule/preview",
        headers=_CSRF_HEADER,
        json={"schedule": {"mode": "cron", "cron": "nonsense"}},
    ).json()
    assert not bad["ok"] and bad["error"]


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


def test_a_job_is_created_and_the_answer_says_it_is_active(
    client: TestClient, deployment: Deployment
) -> None:
    response = _admin(client).post(f"{_BASE}/jobs", headers=_CSRF_HEADER, json={"job": _state()})

    assert response.status_code == 201, response.text
    assert response.json() == {
        "job_id": "handbook",
        "created": True,
        "applied": True,
        "verified": True,
        "elsewhere": [],
        "note": "",
    }
    assert deployment.file_jobs() == [_authored()]


def test_a_refused_job_answers_422_with_the_field_of_every_problem(
    client: TestClient, deployment: Deployment
) -> None:
    bad = _state(
        source={"type": "s3", "label": "x"},
        target={"collection": "", "connection": "nowhere"},
    )

    response = _admin(client).post(f"{_BASE}/jobs", headers=_CSRF_HEADER, json={"job": bad})

    assert response.status_code == 422
    detail = response.json()["detail"]
    fields = {issue["field"] for issue in detail["issues"]}
    assert {"source.bucket", "target.collection"} <= fields
    assert detail["message"]
    assert not deployment.jobs_path.exists()


def test_an_existing_id_is_a_422_and_a_stale_edit_a_409(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_authored())
    api = _admin(client)

    clash = api.post(f"{_BASE}/jobs", headers=_CSRF_HEADER, json={"job": _state()})
    assert clash.status_code == 422
    assert clash.json()["detail"]["issues"][0]["field"] == "id"

    etag = api.get(f"{_BASE}/jobs/handbook/editor").json()["etag"]
    deployment.write_catalog(_authored(description="changed elsewhere"))
    stale = api.put(
        f"{_BASE}/jobs/handbook", headers=_CSRF_HEADER, json={"job": _state(), "etag": etag}
    )
    assert stale.status_code == 409
    assert "changed by somebody else" in stale.json()["detail"]


def test_an_edit_goes_through_with_the_etag_it_opened(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_authored())
    api = _admin(client)
    etag = api.get(f"{_BASE}/jobs/handbook/editor").json()["etag"]

    response = api.put(
        f"{_BASE}/jobs/handbook",
        headers=_CSRF_HEADER,
        json={"job": _state(description="Edited"), "etag": etag},
    )

    assert response.status_code == 200 and response.json()["created"] is False
    assert deployment.file_jobs()[0]["description"] == "Edited"


def test_validating_reports_problems_and_writes_nothing(
    client: TestClient, deployment: Deployment
) -> None:
    api = _admin(client)

    good = api.post(
        f"{_BASE}/jobs/validate", headers=_CSRF_HEADER, json={"job": _state(), "create": True}
    ).json()
    bad = api.post(
        f"{_BASE}/jobs/validate",
        headers=_CSRF_HEADER,
        json={"job": _state(id="Not A Slug"), "create": True},
    ).json()

    assert good == {"ok": True, "issues": []}
    assert not bad["ok"] and bad["issues"][0]["field"] == "id"
    assert not deployment.jobs_path.exists()


def test_a_job_the_ingester_refuses_is_a_502_and_leaves_the_file_alone(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_authored())
    before = deployment.jobs_path.read_bytes()
    deployment.ingest.catalog_errors = [
        {"job_id": "second", "field": "source.path", "message": "no such folder"}
    ]
    second = _state(
        id="second",
        source={"type": "local", "label": "s", "path": "/data/local/s"},
        target={"collection": "kb2", "connection": "default"},
    )

    response = _admin(client).post(f"{_BASE}/jobs", headers=_CSRF_HEADER, json={"job": second})

    assert response.status_code == 502
    assert "no such folder" in response.json()["detail"]
    assert deployment.jobs_path.read_bytes() == before


# ---------------------------------------------------------------------------
# Enable, disable, delete, run, abort
# ---------------------------------------------------------------------------


def test_disable_enable_and_delete(client: TestClient, deployment: Deployment) -> None:
    deployment.write_catalog(_authored())
    api = _admin(client)

    off = api.post(f"{_BASE}/jobs/handbook/disable", headers=_CSRF_HEADER)
    assert off.status_code == 200 and deployment.file_jobs()[0]["enabled"] is False
    on = api.post(f"{_BASE}/jobs/handbook/enable", headers=_CSRF_HEADER)
    assert on.status_code == 200 and "enabled" not in deployment.file_jobs()[0]

    gone = api.delete(f"{_BASE}/jobs/handbook", headers=_CSRF_HEADER)
    assert gone.status_code == 200 and gone.json()["job_id"] == "handbook"
    assert deployment.file_jobs() == []
    assert api.delete(f"{_BASE}/jobs/handbook", headers=_CSRF_HEADER).status_code == 404


def test_a_run_is_accepted_and_the_destructive_ones_have_to_be_confirmed(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_authored())
    api = _admin(client)

    refused = api.post(f"{_BASE}/jobs/handbook/run", headers=_CSRF_HEADER, json={"mode": "full"})
    assert refused.status_code == 422 and "confirmed" in refused.json()["detail"]
    assert deployment.ingest.run_bodies == []

    started = api.post(
        f"{_BASE}/jobs/handbook/run", headers=_CSRF_HEADER, json={"dry_run": True}
    )
    assert started.status_code == 202
    assert started.json()["run_id"] in deployment.ingest.runs
    busy = api.post(f"{_BASE}/jobs/handbook/run", headers=_CSRF_HEADER, json={})
    assert busy.status_code == 409


def test_a_run_can_be_read_listed_and_aborted(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_authored())
    run_id = deployment.ingest.start_run("handbook")
    deployment.ingest.progress(run_id, phase="embedding", files_seen=10, files_done=5)
    api = _admin(client)

    one = api.get(f"{_BASE}/job-runs/{run_id}").json()
    assert (one["phase"], one["progress"], one["active"]) == ("embedding", 0.5, True)
    by_job = api.get(f"{_BASE}/job-runs?job_id=handbook").json()["runs"]
    of_job = api.get(f"{_BASE}/jobs/handbook/runs").json()["runs"]
    assert [r["run_id"] for r in by_job] == [r["run_id"] for r in of_job] == [run_id]

    assert api.delete(f"{_BASE}/job-runs/{run_id}", headers=_CSRF_HEADER).status_code == 204
    assert api.delete(f"{_BASE}/job-runs/{run_id}", headers=_CSRF_HEADER).status_code == 409
    assert api.get(f"{_BASE}/job-runs/unknown").status_code == 404


def test_the_files_of_a_job_are_listed_and_an_old_ingester_says_so(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_authored())
    deployment.ingest.documents["handbook"] = [
        {"rel_path": "a.md", "status": "indexed", "chunk_count": 2, "source": "x"},
        {"rel_path": "b.pdf", "status": "failed_extract", "chunk_count": 0, "source": "y",
         "last_error": "Tika answered 500"},
    ]
    api = _admin(client)

    body = api.get(f"{_BASE}/jobs/handbook/files?status=failed_extract").json()
    assert body["total"] == 1 and body["items"][0]["last_error"] == "Tika answered 500"
    assert body["counts"] == {"indexed": 1, "failed_extract": 1}

    deployment.ingest.features = []
    old = api.get(f"{_BASE}/jobs/handbook/files")
    assert old.status_code == 404 and "newer release" in old.json()["detail"]
    assert api.get(f"{_BASE}/jobs/handbook/files?limit=0").status_code == 422


def test_the_preview_lists_what_a_run_would_pick_up(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_authored())
    deployment.ingest.preview_files["handbook"] = [{"rel_path": "a.md", "source": "s", "size": 3}]

    body = _admin(client).get(f"{_BASE}/jobs/handbook/preview").json()

    assert body == {"files": [{"rel_path": "a.md", "source": "s", "size": 3}], "count": 1}


# ---------------------------------------------------------------------------
# Leftovers
# ---------------------------------------------------------------------------


def test_leftovers_are_listed_and_a_job_of_the_file_is_not_deletable(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_authored())
    deployment.ingest.jobs.pop("handbook")
    deployment.ingest.orphans = [
        {"job_id": "handbook", "collection": "kb", "state_rows": 2, "points": 5},
        {"job_id": "old", "collection": "kb", "state_rows": 1, "points": 1},
    ]
    api = _admin(client)

    listed = api.get(f"{_BASE}/orphans").json()["orphans"]
    assert {o["job_id"]: o["in_file"] for o in listed} == {"handbook": True, "old": False}

    assert api.delete(f"{_BASE}/orphans/handbook", headers=_CSRF_HEADER).status_code == 409
    done = api.delete(f"{_BASE}/orphans/old", headers=_CSRF_HEADER)
    assert done.status_code == 200 and done.json()["deleted_points"] == 1


# ---------------------------------------------------------------------------
# Credentials: a value goes in and never comes out
# ---------------------------------------------------------------------------


def test_a_credential_is_stored_listed_by_name_and_deleted(
    client: TestClient, deployment: Deployment
) -> None:
    api = _admin(client)

    stored = api.put(f"{_BASE}/secrets/dav", headers=_CSRF_HEADER, json={"value": VALUE})
    assert stored.status_code == 200 and stored.json() == {"name": "QI_SECRET_DAV"}

    listing = api.get(f"{_BASE}/secrets")
    assert listing.status_code == 200
    assert [i["name"] for i in listing.json()["items"]] == ["QI_SECRET_DAV"]
    assert listing.json()["supported"] is True

    assert api.delete(f"{_BASE}/secrets/dav", headers=_CSRF_HEADER).status_code == 204
    assert api.get(f"{_BASE}/secrets").json()["items"] == []
    assert VALUE not in listing.text and VALUE not in stored.text
    assert VALUE not in deployment.audit_text()


def test_credential_refusals(client: TestClient, deployment: Deployment) -> None:
    api = _admin(client)
    (deployment.config_dir / "ai" / "rag" / ".env").write_text(
        f"QI_CONNECTIONS_SECRET=s\nQI_API_TOKEN={TOKEN}\nQI_SECRET_FROM_ENV=x\n", encoding="utf-8"
    )

    def put(name: str, value: str) -> int:
        response = api.put(
            f"{_BASE}/secrets/{name}", headers=_CSRF_HEADER, json={"value": value}
        )
        return response.status_code

    assert put("from_env", "v") == 409
    assert put("bad%2Fname", "v") in (404, 422)
    assert put("dav", "  ") == 422
    assert api.delete(f"{_BASE}/secrets/nope", headers=_CSRF_HEADER).status_code == 404
    deployment.ingest.features = []
    assert put("dav", "v") == 409


# ---------------------------------------------------------------------------
# The catalog file
# ---------------------------------------------------------------------------


def test_the_raw_text_is_read_validated_and_replaced(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.write_catalog(_authored())
    api = _admin(client)
    raw = api.get(f"{_BASE}/catalog/raw").json()
    assert raw["exists"] and raw["revision"] and "handbook" in raw["text"]

    bad = api.post(
        f"{_BASE}/catalog/validate", headers=_CSRF_HEADER, json={"text": "jobs: [x"}
    ).json()
    assert not bad["ok"] and bad["issues"][0]["field"] == "jobs_file"

    typed = "# my notes\n" + raw["text"]
    saved = api.put(
        f"{_BASE}/catalog/raw",
        headers=_CSRF_HEADER,
        json={"text": typed, "revision": raw["revision"]},
    )
    assert saved.status_code == 200 and saved.json()["applied"]
    assert deployment.jobs_path.read_bytes() == typed.encode("utf-8")

    stale = api.put(
        f"{_BASE}/catalog/raw",
        headers=_CSRF_HEADER,
        json={"text": typed, "revision": raw["revision"]},
    )
    assert stale.status_code == 409


def test_the_defaults_are_read_and_replaced(client: TestClient, deployment: Deployment) -> None:
    deployment.write_catalog(_authored())
    api = _admin(client)

    done = api.put(
        f"{_BASE}/catalog/defaults",
        headers=_CSRF_HEADER,
        json={"defaults": {"chunking": {"words": 300}}},
    )

    assert done.status_code == 200 and done.json()["defaults"] == {"chunking": {"words": 300}}
    assert api.get(f"{_BASE}/catalog/defaults").json() == {"defaults": {"chunking": {"words": 300}}}
    bad = api.put(
        f"{_BASE}/catalog/defaults", headers=_CSRF_HEADER, json={"defaults": {"surprise": {}}}
    )
    assert bad.status_code == 422
