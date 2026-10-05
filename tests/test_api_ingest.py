"""The Embedding API and its pages: who may call what, what the status codes are, and that
nothing leaks.

The rules themselves are pinned in `test_ingest_*.py`; this is the HTTP surface around them:
the access tiers, the CSRF check on every change, the upload endpoint (which reads its own
body, after the checks), the answers for each kind of failure, and that the ingester's token
reaches no response, audit entry or log line.

The second half is the pages. The page is a shell around partials that the browser fetches,
so those tests fetch them the way it does and look at what is rendered: that each state of the
deployment says what is wrong instead of failing, that a file name cannot break out of the
markup it is put in, and that the strip which watches a run also does the clean-up when the
run is over.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-ingest-api-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-ingest-api-workspace-")

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
from app.core.rag_collections import meta_point_id  # noqa: E402
from app.core.vectordb.ingest_file import CONNECTIONS_RELPATH  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers.rag_deps import get_http_transport  # noqa: E402
from tests.fake_ingest import TOKEN, FakeIngest  # noqa: E402
from tests.fake_qdrant import API_KEY, FakeQdrant, Fleet  # noqa: E402

_CSRF = "test-csrf-token-value"
_CSRF_HEADER = {"X-CSRF-Token": _CSRF}
_BASE = "/api/v1/rag/ingest"
_REACH = "http://qdrant.test:6333"
MODEL = "nomic-embed-text"


class Deployment:
    def __init__(self, config_dir: Path) -> None:
        self.config_dir = config_dir
        self.docs = config_dir / "ai" / "rag" / "documents"
        self.qdrant = FakeQdrant(API_KEY)
        self.qdrant.add("kb", size=4)
        self.qdrant.add("_collection_meta", size=1)
        self.qdrant.put(
            "_collection_meta",
            meta_point_id("kb"),
            {"collection": "kb", "embedding_model": MODEL, "vector_dimension": 4},
        )
        self.ingest = FakeIngest(config_dir)
        self.fleet = Fleet()
        self.fleet.add("qdrant.test", self.qdrant)
        self.fleet.add("qdrant-ingest", self.ingest)

    def audit(self) -> list[dict[str, Any]]:
        path = audit_path(str(self.config_dir))
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def jobs_exist(self) -> bool:
        return (self.config_dir / catalog.JOBS_RELPATH).exists()


@pytest.fixture
def deployment(tmp_path: Path) -> Deployment:
    config_dir = tmp_path / "config"
    (config_dir / "manager").mkdir(parents=True)
    (config_dir / "ai" / "rag" / "documents").mkdir(parents=True)
    (config_dir / CONNECTIONS_RELPATH.parent).mkdir(parents=True, exist_ok=True)
    (config_dir / ".env").write_text("COMPOSE_PROFILES=keycloak,rag\n", encoding="utf-8")
    (config_dir / "ai" / "rag" / ".env").write_text(
        f"QDRANT_JWT_SECRET={API_KEY}\nQI_CONNECTIONS_SECRET=s\nQI_API_TOKEN={TOKEN}\n",
        encoding="utf-8",
    )
    return Deployment(config_dir)


@pytest.fixture
def client(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    async def idle(_kind: object) -> None:
        return None

    monkeypatch.setattr(runs.runner, "find_runner", idle)
    get_settings.cache_clear()
    settings = get_settings().model_copy(
        update={
            "papaia_config_dir": str(deployment.config_dir),
            "qdrant_url": _REACH,
            "qdrant_ingest_url": "http://qdrant-ingest:8300",
            "ingest_max_upload_mb": 1,
            "ingest_max_batch_mb": 2,
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


def _new_upload(client: TestClient, name: str = "") -> dict[str, Any]:
    response = _admin(client).post(f"{_BASE}/uploads", headers=_CSRF_HEADER, json={"name": name})
    assert response.status_code == 201, response.text
    return response.json()  # type: ignore[no-any-return]


def _put_file(
    client: TestClient, batch_id: str, path: str, data: bytes = b"# a", **kwargs: Any
) -> Any:
    return _admin(client).post(
        f"{_BASE}/uploads/{batch_id}/files",
        headers=_CSRF_HEADER,
        files={"file": ("ignored.md", data)},
        data={"path": path},
        **kwargs,
    )


def _run_body(batch_id: str, **changes: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "collection": "kb",
        "mode": "add",
        "source": {"kind": "upload", "batch": batch_id, "paths": []},
    }
    body.update(changes)
    return body


# ---------------------------------------------------------------------------
# Who may call what
# ---------------------------------------------------------------------------

_READS = [
    ("get", f"{_BASE}/status", None),
    ("get", f"{_BASE}/uploads", None),
    ("get", f"{_BASE}/tree", None),
    ("get", f"{_BASE}/runs?collection=kb", None),
    ("get", f"{_BASE}/runs/some-run", None),
]
_WRITES = [
    ("post", f"{_BASE}/uploads", {"name": ""}),
    ("post", f"{_BASE}/uploads/20261005-100000-aaaaaaaa/files", None),
    ("delete", f"{_BASE}/uploads/20261005-100000-aaaaaaaa", None),
    ("post", f"{_BASE}/runs", _run_body("20261005-100000-aaaaaaaa")),
    ("delete", f"{_BASE}/runs/some-run", None),
]


def _call(client: TestClient, method: str, url: str, body: Any, headers: dict[str, str]) -> Any:
    return client.request(method, url, headers=headers, json=body)


@pytest.mark.parametrize(("method", "url", "body"), [*_READS, *_WRITES])
def test_an_anonymous_caller_gets_a_401(
    client: TestClient, method: str, url: str, body: Any
) -> None:
    client.cookies.clear()

    assert _call(client, method, url, body, _CSRF_HEADER).status_code == 401


@pytest.mark.parametrize(("method", "url", "body"), [*_READS, *_WRITES])
def test_a_user_without_the_admin_role_gets_a_403(
    client: TestClient, method: str, url: str, body: Any
) -> None:
    assert _call(_as(client, "user"), method, url, body, _CSRF_HEADER).status_code == 403


@pytest.mark.parametrize(("method", "url", "body"), [*_READS, *_WRITES])
def test_an_administrator_gets_a_404_without_the_rag_profile(
    client: TestClient, deployment: Deployment, method: str, url: str, body: Any
) -> None:
    (deployment.config_dir / ".env").write_text("COMPOSE_PROFILES=keycloak\n", encoding="utf-8")

    assert _call(_admin(client), method, url, body, _CSRF_HEADER).status_code == 404
    assert not deployment.jobs_exist()


@pytest.mark.parametrize(("method", "url", "body"), _WRITES)
def test_a_change_without_the_csrf_token_is_refused_and_changes_nothing(
    client: TestClient, deployment: Deployment, method: str, url: str, body: Any
) -> None:
    response = _call(_admin(client), method, url, body, {})

    assert response.status_code == 403
    assert not deployment.jobs_exist()
    # The default connection is seeded on first use of any RAG route (before the CSRF check,
    # as on the Collections API); that is not a change this request asked for.
    assert [e for e in deployment.audit() if e["action"].startswith("rag.ingest")] == []
    assert not (deployment.docs / "uploads").exists()


def test_a_multipart_body_is_not_read_before_the_checks(
    client: TestClient, deployment: Deployment
) -> None:
    batch = _new_upload(client)

    response = _admin(client).post(
        f"{_BASE}/uploads/{batch['id']}/files",
        files={"file": ("a.md", b"x")},
        data={"path": "a.md"},
    )

    assert response.status_code == 403
    assert not list((deployment.docs / "uploads").rglob("a.md"))


# ---------------------------------------------------------------------------
# The upload area
# ---------------------------------------------------------------------------


def test_an_upload_is_created_for_the_signed_in_user(
    client: TestClient, deployment: Deployment
) -> None:
    batch = _new_upload(client, "Quarterly reports")

    assert batch["owner"] == "tester"
    assert batch["owner_name"] == "Tester"
    assert batch["name"] == "Quarterly reports"
    assert batch["state"] == "staged" and batch["files"] == 0
    assert (deployment.docs / "uploads" / "tester" / batch["id"]).is_dir()
    assert [e["action"] for e in deployment.audit()] == ["rag.ingest.upload.create"]


def test_files_are_stored_at_their_relative_paths_and_listed(
    client: TestClient, deployment: Deployment
) -> None:
    batch = _new_upload(client)

    first = _put_file(client, batch["id"], "handbook/chapters/one.md", b"# one")
    second = _put_file(client, batch["id"], "notes.txt", b"hello")
    again = _put_file(client, batch["id"], "notes.txt", b"hello again")

    assert first.status_code == 201
    assert first.json() == {"path": "handbook/chapters/one.md", "bytes": 5, "replaced": False}
    assert second.json()["replaced"] is False and again.json()["replaced"] is True
    stored = deployment.docs / "uploads" / "tester" / batch["id"]
    assert (stored / "handbook" / "chapters" / "one.md").read_bytes() == b"# one"
    assert (stored / "notes.txt").read_bytes() == b"hello again"
    listing = _admin(client).get(f"{_BASE}/uploads").json()["uploads"]
    assert [(u["id"], u["files"], u["bytes"]) for u in listing] == [(batch["id"], 2, 5 + 11)]


def test_the_form_path_defaults_to_the_file_name(client: TestClient) -> None:
    batch = _new_upload(client)

    response = _admin(client).post(
        f"{_BASE}/uploads/{batch['id']}/files",
        headers=_CSRF_HEADER,
        files={"file": ("report.pdf", b"%PDF")},
    )

    assert response.status_code == 201 and response.json()["path"] == "report.pdf"


@pytest.mark.parametrize(
    "path", ["../escape.md", "/etc/passwd", "a\\b.md", "what?.md", "dir/", "", "x/../../y"]
)
def test_a_name_that_cannot_be_stored_is_a_422_and_stores_nothing(
    client: TestClient, deployment: Deployment, path: str
) -> None:
    batch = _new_upload(client)

    response = _admin(client).post(
        f"{_BASE}/uploads/{batch['id']}/files",
        headers=_CSRF_HEADER,
        files={"file": ("", b"x")},
        data={"path": path},
    )

    assert response.status_code == 422, response.text
    assert not [p for p in deployment.docs.rglob("*") if p.is_file()]


def test_a_file_over_the_limit_is_a_413_whether_declared_or_only_found_while_reading(
    client: TestClient, deployment: Deployment
) -> None:
    batch = _new_upload(client)  # the fixture's limit is 1 MiB per file

    declared = _put_file(client, batch["id"], "huge.bin", b"x" * (3 * 2**20))
    found = _put_file(client, batch["id"], "big.bin", b"x" * (1 * 2**20 + 500))

    assert declared.status_code == 413 and "INGEST_MAX_UPLOAD_MB" in declared.json()["detail"]
    assert found.status_code == 413
    stored = deployment.docs / "uploads" / "tester" / batch["id"]
    assert list(stored.iterdir()) == []


def test_an_upload_over_its_total_is_a_413(client: TestClient) -> None:
    batch = _new_upload(client)  # 2 MiB per upload in the fixture
    assert _put_file(client, batch["id"], "a.bin", b"x" * (1 * 2**20)).status_code == 201
    assert _put_file(client, batch["id"], "b.bin", b"x" * (1 * 2**20)).status_code == 201

    response = _put_file(client, batch["id"], "c.bin", b"x")

    assert response.status_code == 413 and "INGEST_MAX_BATCH_MB" in response.json()["detail"]


def test_a_request_without_a_file_part_is_a_422(client: TestClient) -> None:
    batch = _new_upload(client)

    response = _admin(client).post(
        f"{_BASE}/uploads/{batch['id']}/files", headers=_CSRF_HEADER, data={"path": "a.md"}
    )

    assert response.status_code == 422


def test_a_file_for_an_unknown_upload_is_a_404(client: TestClient) -> None:
    assert _put_file(client, "20261005-100000-deadbeef", "a.md").status_code == 404
    assert _put_file(client, "..%2F..%2Fetc", "a.md").status_code in (404, 422)


def test_discarding_removes_the_upload_and_is_audited(
    client: TestClient, deployment: Deployment
) -> None:
    batch = _new_upload(client)
    _put_file(client, batch["id"], "secret/plan.pdf", b"confidential")

    response = _admin(client).delete(f"{_BASE}/uploads/{batch['id']}", headers=_CSRF_HEADER)

    assert response.status_code == 204
    assert not (deployment.docs / "uploads" / "tester").exists()
    assert _admin(client).get(f"{_BASE}/uploads").json() == {"uploads": []}
    [entry] = [e for e in deployment.audit() if e["action"] == "rag.ingest.upload.discard"]
    assert entry["params"]["files"] == 1
    assert "plan.pdf" not in json.dumps(entry)


def test_an_upload_that_is_being_embedded_cannot_be_discarded(client: TestClient) -> None:
    batch = _new_upload(client)
    _put_file(client, batch["id"], "a.md")
    started = _admin(client).post(
        f"{_BASE}/runs", headers=_CSRF_HEADER, json=_run_body(batch["id"])
    )
    assert started.status_code == 202, started.text

    response = _admin(client).delete(f"{_BASE}/uploads/{batch['id']}", headers=_CSRF_HEADER)

    assert response.status_code == 409


def test_the_upload_area_says_why_it_is_unavailable(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.docs.rmdir()

    response = _admin(client).post(f"{_BASE}/uploads", headers=_CSRF_HEADER, json={})

    assert response.status_code == 503 and "does not exist" in response.json()["detail"]


def test_a_documents_folder_the_manager_cannot_see_is_reported(
    client: TestClient, deployment: Deployment
) -> None:
    env = deployment.config_dir / "ai" / "rag" / ".env"
    env.write_text(env.read_text(encoding="utf-8") + "QI_LOCAL_MOUNT=/srv/elsewhere\n")

    response = _admin(client).get(f"{_BASE}/uploads")

    assert response.status_code == 503 and "cannot see" in response.json()["detail"]


# ---------------------------------------------------------------------------
# The tree
# ---------------------------------------------------------------------------


def test_the_tree_lists_the_documents_folder_without_the_staging_area(
    client: TestClient, deployment: Deployment
) -> None:
    (deployment.docs / "handbook").mkdir()
    (deployment.docs / "handbook" / "a.md").write_text("a", encoding="utf-8")
    (deployment.docs / "notes.txt").write_text("hello", encoding="utf-8")
    _new_upload(client)

    root = _admin(client).get(f"{_BASE}/tree").json()
    nested = _admin(client).get(f"{_BASE}/tree", params={"path": "handbook"}).json()

    assert [(e["name"], e["dir"]) for e in root["entries"]] == [
        ("handbook", True),
        ("notes.txt", False),
    ]
    assert nested["entries"] == [{"name": "a.md", "path": "handbook/a.md", "dir": False, "size": 1}]


def test_the_tree_of_an_upload_lists_its_files(client: TestClient) -> None:
    batch = _new_upload(client)
    _put_file(client, batch["id"], "a/one.md")

    tree = _admin(client).get(
        f"{_BASE}/tree", params={"source": "upload", "upload": batch["id"], "path": "a"}
    ).json()

    assert [e["path"] for e in tree["entries"]] == ["a/one.md"]


@pytest.mark.parametrize("path", ["../..", "a/../..", "/etc", "uploads", "a\\b"])
def test_the_tree_refuses_a_path_outside_the_folder(client: TestClient, path: str) -> None:
    response = _admin(client).get(f"{_BASE}/tree", params={"path": path})

    assert response.status_code in (404, 422)


def test_the_tree_of_a_missing_path_and_of_a_missing_upload_is_a_404(client: TestClient) -> None:
    assert _admin(client).get(f"{_BASE}/tree", params={"path": "nope"}).status_code == 404
    assert _admin(client).get(
        f"{_BASE}/tree", params={"source": "upload", "upload": "20261005-100000-deadbeef"}
    ).status_code == 404
    assert _admin(client).get(f"{_BASE}/tree", params={"source": "upload"}).status_code == 422


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def test_the_status_reports_the_ingester_and_the_folder(client: TestClient) -> None:
    body = _admin(client).get(f"{_BASE}/status").json()

    assert body == {
        "ready": True,
        "reason": "",
        "supports_add": True,
        "documents": {"available": True, "reason": "", "writable": True},
    }


def test_a_run_is_started_and_its_status_is_readable(
    client: TestClient, deployment: Deployment
) -> None:
    batch = _new_upload(client)
    _put_file(client, batch["id"], "a.md")

    started = _admin(client).post(
        f"{_BASE}/runs", headers=_CSRF_HEADER, json=_run_body(batch["id"])
    )

    assert started.status_code == 202
    body = started.json()
    assert body["files"] == 1 and body["job_id"].startswith("mgr-")
    assert deployment.ingest.run_bodies == [
        (body["job_id"], {"mode": "upsert", "delete_vanished": False})
    ]
    deployment.qdrant.put("kb", "p1", {"ingest_run": body["run_id"]})
    status = _admin(client).get(f"{_BASE}/runs/{body['run_id']}", params={"collection": "kb"})
    assert status.status_code == 200
    assert status.json()["active"] is True and status.json()["live_chunks"] == 1
    history = _admin(client).get(f"{_BASE}/runs", params={"collection": "kb"}).json()["runs"]
    assert [r["run_id"] for r in history] == [body["run_id"]]


def test_the_run_can_be_aborted(client: TestClient, deployment: Deployment) -> None:
    batch = _new_upload(client)
    _put_file(client, batch["id"], "a.md")
    run_id = _admin(client).post(
        f"{_BASE}/runs", headers=_CSRF_HEADER, json=_run_body(batch["id"])
    ).json()["run_id"]

    first = _admin(client).delete(f"{_BASE}/runs/{run_id}", headers=_CSRF_HEADER)
    second = _admin(client).delete(f"{_BASE}/runs/{run_id}", headers=_CSRF_HEADER)

    assert (first.status_code, second.status_code) == (204, 409)
    assert _admin(client).delete(f"{_BASE}/runs/nope", headers=_CSRF_HEADER).status_code == 404


@pytest.mark.parametrize(
    ("changes", "status", "text"),
    [
        ({"collection": "nope"}, 404, "nope"),
        ({"mode": "merge"}, 422, ""),
        ({"mode": "replace"}, 422, "confirmed"),
        ({"model": "other-model"}, 422, "Replace"),
        ({"source": {"kind": "ftp"}}, 422, ""),
        ({"collection": ""}, 422, ""),
    ],
)
def test_a_request_that_cannot_work_is_refused_with_the_matching_status(
    client: TestClient, deployment: Deployment, changes: dict[str, Any], status: int, text: str
) -> None:
    batch = _new_upload(client)
    _put_file(client, batch["id"], "a.md")

    response = _admin(client).post(
        f"{_BASE}/runs", headers=_CSRF_HEADER, json=_run_body(batch["id"], **changes)
    )

    assert response.status_code == status, response.text
    assert text in response.text
    assert not deployment.jobs_exist() and deployment.ingest.run_bodies == []


def test_a_busy_collection_is_a_409(client: TestClient) -> None:
    first = _new_upload(client)
    _put_file(client, first["id"], "a.md")
    second = _new_upload(client)
    _put_file(client, second["id"], "b.md")
    assert _admin(client).post(
        f"{_BASE}/runs", headers=_CSRF_HEADER, json=_run_body(first["id"])
    ).status_code == 202

    response = _admin(client).post(
        f"{_BASE}/runs", headers=_CSRF_HEADER, json=_run_body(second["id"])
    )

    assert response.status_code == 409 and "still working" in response.json()["detail"]


def test_an_older_ingester_is_a_409_for_add_and_works_for_replace(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.ingest.supports_delete_vanished = False
    batch = _new_upload(client)
    _put_file(client, batch["id"], "a.md")

    add = _admin(client).post(f"{_BASE}/runs", headers=_CSRF_HEADER, json=_run_body(batch["id"]))
    replace = _admin(client).post(
        f"{_BASE}/runs",
        headers=_CSRF_HEADER,
        json=_run_body(batch["id"], mode="replace", confirm_replace=True),
    )

    assert add.status_code == 409 and "delete_vanished" in add.json()["detail"]
    assert replace.status_code == 202


def test_a_catalog_the_ingester_refuses_is_a_502_and_leaves_the_file_alone(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.ingest.catalog_errors = [
        {"job_id": "someone-elses", "field": "source", "message": "unsupported"}
    ]
    batch = _new_upload(client)
    _put_file(client, batch["id"], "a.md")

    response = _admin(client).post(
        f"{_BASE}/runs", headers=_CSRF_HEADER, json=_run_body(batch["id"])
    )

    assert response.status_code == 502 and "someone-elses" in response.json()["detail"]
    assert not deployment.jobs_exist()


def test_an_ingester_that_is_down_or_refuses_the_token_is_a_503(
    client: TestClient, deployment: Deployment
) -> None:
    batch = _new_upload(client)
    _put_file(client, batch["id"], "a.md")
    deployment.ingest.down = True

    down = _admin(client).post(f"{_BASE}/runs", headers=_CSRF_HEADER, json=_run_body(batch["id"]))
    status = _admin(client).get(f"{_BASE}/status").json()

    assert down.status_code == 503 and "not reachable" in down.json()["detail"]
    assert status["ready"] is False and "not reachable" in status["reason"]


def test_without_a_token_the_runs_are_a_503_that_names_the_setting(
    client: TestClient, deployment: Deployment
) -> None:
    env = deployment.config_dir / "ai" / "rag" / ".env"
    env.write_text(f"QDRANT_JWT_SECRET={API_KEY}\nQI_CONNECTIONS_SECRET=s\n", encoding="utf-8")
    batch = _new_upload(client)
    _put_file(client, batch["id"], "a.md")

    response = _admin(client).post(
        f"{_BASE}/runs", headers=_CSRF_HEADER, json=_run_body(batch["id"])
    )

    assert response.status_code == 503 and "QI_API_TOKEN" in response.json()["detail"]


def test_a_folder_run_selects_paths_of_the_documents_folder(
    client: TestClient, deployment: Deployment
) -> None:
    (deployment.docs / "handbook").mkdir()
    (deployment.docs / "handbook" / "a.md").write_text("a", encoding="utf-8")

    response = _admin(client).post(
        f"{_BASE}/runs",
        headers=_CSRF_HEADER,
        json={
            "collection": "kb",
            "mode": "add",
            "source": {"kind": "folder", "paths": ["handbook"]},
        },
    )

    assert response.status_code == 202, response.text
    job = yaml.safe_load((deployment.config_dir / catalog.JOBS_RELPATH).read_text())["jobs"][0]
    assert job["filters"]["include"] == ["handbook/**"]


# ---------------------------------------------------------------------------
# Nothing leaks
# ---------------------------------------------------------------------------


def test_the_ingesters_token_is_in_no_response_audit_entry_or_log_line(
    client: TestClient, deployment: Deployment, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    batch = _new_upload(client)
    _put_file(client, batch["id"], "a.md")
    responses = [
        _admin(client).get(f"{_BASE}/status"),
        _admin(client).post(f"{_BASE}/runs", headers=_CSRF_HEADER, json=_run_body(batch["id"])),
        _admin(client).get(f"{_BASE}/runs", params={"collection": "kb"}),
    ]
    deployment.ingest.down = True
    responses.append(
        _admin(client).post(f"{_BASE}/runs", headers=_CSRF_HEADER, json=_run_body(batch["id"]))
    )

    for response in responses:
        assert TOKEN not in response.text
    assert TOKEN not in json.dumps(deployment.audit())
    assert TOKEN not in caplog.text
    assert API_KEY not in json.dumps(deployment.audit())


# ---------------------------------------------------------------------------
# The page and its body
# ---------------------------------------------------------------------------


def _init(html: str) -> dict[str, object]:
    """The state the body hands to the page's Alpine scope."""
    match = re.search(r"x-init='loaded\((.*?)\)'", html, re.S)
    assert match, "the body does not tell the page what it loaded"
    return json.loads(match.group(1).replace("\\u0027", "'"))  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# The page and its body
# ---------------------------------------------------------------------------


def test_the_page_names_its_connection_and_asks_for_its_body(client: TestClient) -> None:
    html = _admin(client).get("/embedding?collection=kb").text

    assert "<title>Embedding" in html
    assert 'id="embed-connection"' in html
    assert 'hx-get="/partials/embedding?connection=default&collection=kb"' in html
    assert 'x-data="embedPage()"' in html


def test_an_unknown_connection_falls_back_to_the_default(client: TestClient) -> None:
    html = _admin(client).get("/embedding?connection=nope").text

    assert "connection=default" in html


def test_the_body_offers_both_ways_to_put_files_in(client: TestClient) -> None:
    html = _admin(client).get("/partials/embedding?collection=kb").text

    assert "Add files" in html and "Add folder" in html and "webkitdirectory" in html
    assert "deleted after a successful run" in html
    assert "Add and update" in html and "Replace the collection" in html
    assert "Start embedding" in html
    state = _init(html)
    assert state["collection"] == "kb"
    assert state["model"] == "nomic-embed-text"
    assert state["ready"] is True and state["supportsAdd"] is True
    assert state["otherJobs"] == [] and state["activeRun"] == ""


def test_the_body_is_never_cached(client: TestClient) -> None:
    response = _admin(client).get("/partials/embedding")

    assert response.headers["cache-control"] == "no-store"


def test_a_collection_that_is_asked_for_but_does_not_exist_falls_back_to_the_first(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.qdrant.add("another", size=4)

    state = _init(_admin(client).get("/partials/embedding?collection=nope").text)

    assert state["collection"] == "another"


def test_a_collection_without_a_recorded_model_asks_for_one(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.qdrant.add("fresh", size=4)

    html = _admin(client).get("/partials/embedding?collection=fresh").text

    assert "no embedding model yet" in html
    assert _init(html)["model"] == ""


def test_the_other_jobs_of_a_collection_are_handed_to_the_confirmation(
    client: TestClient, deployment: Deployment
) -> None:
    (deployment.config_dir / "ai" / "rag" / "catalog").mkdir(parents=True, exist_ok=True)
    (deployment.config_dir / "ai" / "rag" / "catalog" / "jobs.yaml").write_text(
        "version: 1\njobs:\n  - id: nightly\n    source: {type: s3, label: b, bucket: b}\n"
        "    target: {collection: kb, connection: default}\n",
        encoding="utf-8",
    )

    assert _init(_admin(client).get("/partials/embedding?collection=kb").text)["otherJobs"] == [
        "nightly"
    ]


def test_there_is_a_way_forward_when_no_collection_exists(
    client: TestClient, deployment: Deployment
) -> None:
    del deployment.qdrant.collections["kb"]

    html = _admin(client).get("/partials/embedding").text

    assert "no collection to embed into yet" in html
    assert 'href="/collections?connection=default"' in html
    assert "Start embedding" not in html


def test_a_qdrant_that_is_down_is_reported_not_raised(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.qdrant.down = True

    response = _admin(client).get("/partials/embedding")

    assert response.status_code == 200
    assert "Embedding is not available" in response.text
    assert "not reachable" in response.text


def test_an_ingester_that_cannot_be_used_says_why_and_disables_the_run(
    client: TestClient, deployment: Deployment
) -> None:
    env = deployment.config_dir / "ai" / "rag" / ".env"
    env.write_text(f"QDRANT_JWT_SECRET={API_KEY}\nQI_CONNECTIONS_SECRET=s\n", encoding="utf-8")

    html = _admin(client).get("/partials/embedding").text

    assert "The ingester cannot be used" in html and "QI_API_TOKEN" in html
    assert _init(html)["ready"] is False


def test_a_token_the_ingester_refuses_is_told_apart_from_a_missing_one(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.ingest.token = "another-token"

    html = _admin(client).get("/partials/embedding").text

    assert "refused the API token" in html


def test_an_ingester_that_is_too_old_disables_add_and_says_what_it_needs(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.ingest.supports_delete_vanished = False

    html = _admin(client).get("/partials/embedding").text

    assert "too old" in html and "delete_vanished" in html
    assert _init(html)["supportsAdd"] is False


def test_a_documents_folder_that_is_missing_is_reported_and_the_folder_tab_is_empty(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.docs.rmdir()

    html = _admin(client).get("/partials/embedding").text

    assert "The documents folder cannot be used" in html and "does not exist" in html
    assert 'id="embed-folder-tree"' not in html
    assert _init(html)["ready"] is False


def test_a_read_only_documents_folder_still_lets_files_be_picked(
    client: TestClient, deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = os.access
    def access(path: Any, mode: int) -> bool:
        return False if str(path) == str(deployment.docs) else real(path, mode)

    monkeypatch.setattr(os, "access", access)

    html = _admin(client).get("/partials/embedding").text

    assert "read-only for the manager" in html
    assert 'id="embed-folder-tree"' in html
    assert ":disabled=\"true || uploading" in html


def test_a_staged_upload_shows_who_made_it_and_when_it_goes(client: TestClient) -> None:
    batch = _new_upload(client, "Quarterly reports")
    _put_file(client, batch["id"], "a/one.md", b"# one")

    html = _admin(client).get("/partials/embedding/uploads").text

    assert "Quarterly reports" in html and ">Tester<" in html
    assert "1 file" in html and "ready" in html
    assert "deleted automatically in about 23 h" in html or "in about 24 h" in html
    assert batch["id"] in html


def test_an_upload_that_is_kept_says_why_and_one_that_is_embedding_cannot_be_chosen(
    client: TestClient, deployment: Deployment
) -> None:
    kept = _new_upload(client)
    _put_file(client, kept["id"], "a.md")
    busy = _new_upload(client)
    _put_file(client, busy["id"], "b.md")
    store = _store(client)
    store.mark_kept(kept["id"], "2 document(s) failed")
    store.mark_embedding(
        busy["id"], run_id="r", job_id="j", collection="kb", connection="default", mode="add"
    )

    html = _admin(client).get("/partials/embedding/uploads").text

    assert "kept for a retry" in html and "2 document(s) failed" in html
    assert "being embedded: kb" in html
    row = html[html.index(busy["id"]) : html.index(busy["id"]) + 600]
    assert "disabled" in row


def _store(client: TestClient):  # type: ignore[no-untyped-def]
    from app.config import get_settings
    from app.core.ingest.uploads import new_store

    settings = client.app.dependency_overrides[get_settings]()  # type: ignore[attr-defined]
    return new_store(settings, Path(settings.papaia_config_dir) / "ai" / "rag" / "documents")


# ---------------------------------------------------------------------------
# The tree
# ---------------------------------------------------------------------------


def test_the_tree_has_a_checkbox_per_entry_and_loads_a_folder_when_it_is_opened(
    client: TestClient, deployment: Deployment
) -> None:
    (deployment.docs / "hand book").mkdir()
    (deployment.docs / "hand book" / "a.md").write_text("a", encoding="utf-8")
    (deployment.docs / "notes.txt").write_text("hello", encoding="utf-8")

    html = _admin(client).get("/partials/embedding/tree?source=folder").text

    assert html.count('type="checkbox"') == 2
    assert "hand book/" in html and "notes.txt" in html and "5 Bytes" in html
    assert "path=hand%20book" in html and "hx-trigger=\"click once\"" in html
    assert ">Nothing here" not in html


def test_the_tree_of_an_upload_is_fetched_with_the_upload_in_its_links(
    client: TestClient,
) -> None:
    batch = _new_upload(client)
    _put_file(client, batch["id"], "dir/inner.md")

    html = _admin(client).get(f"/partials/embedding/tree?source=upload&upload={batch['id']}").text

    assert f"upload={batch['id']}" in html and "dir/" in html


def test_a_file_name_cannot_break_out_of_the_markup(
    client: TestClient, deployment: Deployment
) -> None:
    name = "o'brien & \"sons\" <b>.md"
    try:
        (deployment.docs / name).write_text("x", encoding="utf-8")
    except OSError:
        name = "o'brien & sons.md"
        (deployment.docs / name).write_text("x", encoding="utf-8")

    html = _admin(client).get("/partials/embedding/tree?source=folder").text

    assert "<b>.md" not in html
    row = html[html.index("<li"):]
    assert "&amp;" in row or "\\u0026" in row
    # The path reaches the Alpine expressions as JSON in a single-quoted attribute: a quote in
    # the name must not close it.
    for attribute in re.findall(r"""(?:isOn|covered|toggle)\((.*?)\)['"]""", row):
        assert "'" not in attribute, attribute


def test_a_path_outside_the_folder_is_explained_not_served(client: TestClient) -> None:
    for path in ("../..", "uploads", "missing"):
        response = _admin(client).get("/partials/embedding/tree", params={"path": path})

        assert response.status_code == 200
        assert "text-warning" in response.text


def test_an_unknown_source_is_refused(client: TestClient) -> None:
    assert _admin(client).get("/partials/embedding/tree?source=elsewhere").status_code == 422


# ---------------------------------------------------------------------------
# The strip that watches a run
# ---------------------------------------------------------------------------


def _start(client: TestClient, files: dict[str, bytes] | None = None) -> tuple[str, str]:
    batch = _new_upload(client)
    for path, data in (files or {"a.md": b"x"}).items():
        _put_file(client, batch["id"], path, data)
    response = _admin(client).post(
        "/api/v1/rag/ingest/runs", headers=_CSRF_HEADER, json=_run_body(batch["id"])
    )
    assert response.status_code == 202, response.text
    return batch["id"], response.json()["run_id"]


def test_a_working_run_polls_and_shows_the_chunks_so_far(
    client: TestClient, deployment: Deployment
) -> None:
    _batch, run_id = _start(client)
    deployment.qdrant.put("kb", "p1", {"ingest_run": run_id})

    html = _admin(client).get(f"/partials/embedding/status?collection=kb&run={run_id}").text

    assert 'hx-trigger="every 2s"' in html
    assert "Running" in html and "<b>1</b> chunk written so far" in html
    assert "Abort" in html
    assert 'active: true' in html


def test_a_finished_run_stops_polling_shows_its_counts_and_cleans_up(
    client: TestClient, deployment: Deployment
) -> None:
    batch_id, run_id = _start(client, {"secret/plan.pdf": b"confidential"})
    folder = deployment.docs / "uploads" / "tester" / batch_id
    assert folder.is_dir()
    deployment.ingest.finish(run_id, files_seen=1, docs_indexed=1, chunks_upserted=4)

    html = _admin(client).get(f"/partials/embedding/status?collection=kb&run={run_id}").text

    assert "hx-trigger" not in html, "a finished run is not polled"
    assert "Finished" in html and "<b>4</b>" in html
    assert "The upload this run read has been deleted" in html
    assert 'active: false' in html
    assert not folder.exists(), "the status pass that sees the end removes the upload"


def test_a_finished_folder_run_does_not_claim_that_an_upload_was_deleted(
    client: TestClient, deployment: Deployment
) -> None:
    (deployment.docs / "handbook").mkdir()
    (deployment.docs / "handbook" / "a.md").write_text("a", encoding="utf-8")
    run_id = _admin(client).post(
        "/api/v1/rag/ingest/runs",
        headers=_CSRF_HEADER,
        json={
            "collection": "kb",
            "mode": "add",
            "source": {"kind": "folder", "paths": ["handbook"]},
        },
    ).json()["run_id"]
    deployment.ingest.finish(run_id, docs_indexed=1)

    html = _admin(client).get(f"/partials/embedding/status?collection=kb&run={run_id}").text

    assert "Finished" in html and "Add / update" in html
    assert "upload" not in html.lower().replace("earlier runs", "")


def test_a_failed_run_keeps_the_upload_and_lists_what_the_ingester_said(
    client: TestClient, deployment: Deployment
) -> None:
    batch_id, run_id = _start(client)
    deployment.ingest.finish(
        run_id,
        "failed",
        error="the embedding endpoint is down",
        events=[
            {"ts": "t", "level": "error", "source": "embed", "message": "connection refused"}
        ],
    )

    html = _admin(client).get(f"/partials/embedding/status?collection=kb&run={run_id}").text

    assert "Failed" in html and "the embedding endpoint is down" in html
    assert "connection refused" in html
    assert "is kept, so the run can be repeated" in html
    assert (deployment.docs / "uploads" / "tester" / batch_id).is_dir()


def test_the_strip_lists_earlier_runs_of_the_collection(
    client: TestClient, deployment: Deployment
) -> None:
    _batch, run_id = _start(client)
    deployment.ingest.finish(run_id, docs_indexed=2)

    html = _admin(client).get("/partials/embedding/status?collection=kb").text

    assert "Earlier runs" in html and "2 embedded" in html


def test_a_run_the_ingester_forgot_is_reported(client: TestClient) -> None:
    html = _admin(client).get("/partials/embedding/status?collection=kb&run=gone").text

    assert "no longer has this run" in html


def test_an_ingester_that_is_down_is_a_message_on_the_strip(
    client: TestClient, deployment: Deployment
) -> None:
    _batch, run_id = _start(client)
    deployment.ingest.down = True

    response = _admin(client).get(f"/partials/embedding/status?collection=kb&run={run_id}")

    assert response.status_code == 200
    assert "not reachable" in response.text


def test_the_token_is_in_no_page(client: TestClient, deployment: Deployment) -> None:
    _batch, run_id = _start(client)
    pages = [
        "/embedding",
        "/partials/embedding",
        "/partials/embedding/uploads",
        f"/partials/embedding/status?collection=kb&run={run_id}",
    ]

    for page in pages:
        assert TOKEN not in _admin(client).get(page).text, page


# ---------------------------------------------------------------------------
# From the other pages
# ---------------------------------------------------------------------------


def test_a_collection_links_to_the_page_that_embeds_into_it(
    client: TestClient, deployment: Deployment
) -> None:
    deployment.qdrant.add("Finance 2026", size=4)

    html = _admin(client).get("/partials/collections").text

    assert 'href="/embedding?connection=default&collection=Finance%202026"' in html
    assert "Embed files" in html
