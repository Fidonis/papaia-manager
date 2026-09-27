"""End-to-end tests for the audit API: list, export and prune.

Fixtures follow test_api_tiles.py: a fresh `config_dir` per test and a `client`
with `get_settings` overridden to point at it, so tests never share state
through the process environment.
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-audit-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-audit-workspace-")

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
from app.core.audit import audit_path, write_audit_entry  # noqa: E402
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
    settings = get_settings().model_copy(update={"papaia_config_dir": str(config_dir)})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app, follow_redirects=False)
    get_settings.cache_clear()


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


def _seed(config_dir: Path, count: int = 3) -> None:
    for i in range(count):
        write_audit_entry(
            str(config_dir), user="tester", action="install", target=f"addon-{i}", job_id=f"job-{i}"
        )


# ---------------------------------------------------------------------------
# Access
# ---------------------------------------------------------------------------


def test_anonymous_is_rejected(client: TestClient) -> None:
    assert client.get("/api/v1/audit").status_code == 401


def test_user_role_is_denied(client: TestClient) -> None:
    assert _as(client, "user").get("/api/v1/audit").status_code == 403


def test_prune_without_csrf_is_refused(client: TestClient, config_dir: Path) -> None:
    _seed(config_dir, 1)
    response = _admin(client).post("/api/v1/audit/prune", json={"before": "2026-01-01"})
    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Listing
# ---------------------------------------------------------------------------


def test_listing_shape(client: TestClient, config_dir: Path) -> None:
    _seed(config_dir, 3)
    body = _admin(client).get("/api/v1/audit").json()
    assert body["total"] == 3
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert body["corrupt_lines"] == 0
    assert set(body["facets"]) == {"user", "action", "result"}
    assert len(body["entries"]) == 3
    # Ordering itself (newest first, incl. ties) is covered precisely in
    # test_audit.py with controlled timestamps; three real `write_audit_entry`
    # calls in a tight loop can land on the same microsecond.
    assert {e["target"] for e in body["entries"]} == {"addon-0", "addon-1", "addon-2"}


def test_listing_respects_filters(client: TestClient, config_dir: Path) -> None:
    write_audit_entry(str(config_dir), user="alice", action="install", target="n8n")
    write_audit_entry(str(config_dir), user="bob", action="stop", target="paperless")
    body = _admin(client).get("/api/v1/audit", params={"user": "ali"}).json()
    assert body["total"] == 1
    assert body["entries"][0]["user"] == "alice"


def test_the_limit_is_capped_at_200(client: TestClient) -> None:
    response = _admin(client).get("/api/v1/audit", params={"limit": 500})
    assert response.status_code == 422


def test_an_unparsable_since_is_a_422(client: TestClient) -> None:
    response = _admin(client).get("/api/v1/audit", params={"since": "not-a-date"})
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_csv_export_header_and_content(client: TestClient, config_dir: Path) -> None:
    write_audit_entry(str(config_dir), user="tester", action="install", target="n8n")
    response = _admin(client).get("/api/v1/audit/export", params={"format": "csv"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "attachment" in response.headers["content-disposition"]
    assert ".csv" in response.headers["content-disposition"]
    lines = response.text.splitlines()
    assert lines[0] == "ts,user,action,target,result,job_id,params"
    assert "install" in lines[1]
    assert "n8n" in lines[1]


def test_jsonl_export_content(client: TestClient, config_dir: Path) -> None:
    write_audit_entry(str(config_dir), user="tester", action="install", target="n8n")
    response = _admin(client).get("/api/v1/audit/export", params={"format": "jsonl"})
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert ".jsonl" in response.headers["content-disposition"]
    row = json.loads(response.text.splitlines()[0])
    assert row["action"] == "install"


def test_a_credentialed_catalog_url_never_reaches_the_export(
    client: TestClient, config_dir: Path
) -> None:
    # Written unredacted, straight to the file -- as if a caller predating this
    # feature (or a hand-edited line) had put a raw credential into `params`.
    # The export must redact on the way out regardless.
    path = audit_path(str(config_dir))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {
                    "ts": "2026-01-01T00:00:00+00:00",
                    "user": "tester",
                    "action": "catalog-create",
                    "target": "acme",
                    "result": "ok",
                    "params": {"url": "https://x-access-token:ghp_leaked@github.com/acme.git"},
                }
            )
            + "\n"
        )
    csv_response = _admin(client).get("/api/v1/audit/export", params={"format": "csv"})
    jsonl_response = _admin(client).get("/api/v1/audit/export", params={"format": "jsonl"})
    assert "ghp_leaked" not in csv_response.text
    assert "ghp_leaked" not in jsonl_response.text
    assert "https://***@github.com/acme.git" in jsonl_response.text


def test_a_csv_formula_looking_cell_is_neutralised(client: TestClient, config_dir: Path) -> None:
    write_audit_entry(str(config_dir), user="tester", action="install", target="=cmd()")
    response = _admin(client).get("/api/v1/audit/export", params={"format": "csv"})
    assert "'=cmd()" in response.text


def test_export_respects_filters(client: TestClient, config_dir: Path) -> None:
    write_audit_entry(str(config_dir), user="alice", action="install", target="n8n")
    write_audit_entry(str(config_dir), user="bob", action="stop", target="paperless")
    response = _admin(client).get(
        "/api/v1/audit/export", params={"format": "jsonl", "user": "bob"}
    )
    rows = [json.loads(line) for line in response.text.splitlines()]
    assert [r["user"] for r in rows] == ["bob"]


# ---------------------------------------------------------------------------
# Prune
# ---------------------------------------------------------------------------


def _write_old_entry(config_dir: Path, ts: str, target: str = "old") -> None:
    path = audit_path(str(config_dir))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(
            json.dumps(
                {"ts": ts, "user": "tester", "action": "install", "target": target, "result": "ok"}
            )
            + "\n"
        )


def test_a_future_cutoff_is_rejected(client: TestClient, config_dir: Path) -> None:
    future = (datetime.now(tz=UTC).year + 1)
    response = _admin(client).post(
        "/api/v1/audit/prune", headers=_headers(), json={"before": f"{future}-01-01"}
    )
    assert response.status_code == 422


def test_a_malformed_cutoff_is_rejected(client: TestClient, config_dir: Path) -> None:
    response = _admin(client).post(
        "/api/v1/audit/prune", headers=_headers(), json={"before": "not-a-date"}
    )
    assert response.status_code == 422


def test_dry_run_reports_without_deleting(client: TestClient, config_dir: Path) -> None:
    _write_old_entry(config_dir, "2020-01-01T00:00:00+00:00")
    response = _admin(client).post(
        "/api/v1/audit/prune",
        headers=_headers(),
        json={"before": "2026-01-01", "dry_run": True},
    )
    body = response.json()
    assert body == {
        "before": "2026-01-01",
        "dry_run": True,
        "removed": 1,
        "kept": 0,
        "oldest_removed": "2020-01-01T00:00:00+00:00",
        "newest_removed": "2020-01-01T00:00:00+00:00",
    }
    # Nothing actually removed, and no audit-prune entry for a run that changed nothing.
    remaining = _admin(client).get("/api/v1/audit").json()
    assert remaining["total"] == 1
    assert not any(e["action"] == "audit-prune" for e in remaining["entries"])


def test_a_real_prune_deletes_and_leaves_its_own_entry(
    client: TestClient, config_dir: Path
) -> None:
    _write_old_entry(config_dir, "2020-01-01T00:00:00+00:00")
    _write_old_entry(config_dir, "2026-06-01T00:00:00+00:00", target="new")

    response = _admin(client).post(
        "/api/v1/audit/prune", headers=_headers(), json={"before": "2026-01-01"}
    )
    body = response.json()
    assert (body["removed"], body["kept"]) == (1, 1)

    after = _admin(client).get("/api/v1/audit").json()
    # The surviving original entry, plus the audit-prune entry this call wrote.
    assert after["total"] == 2
    actions = {e["action"] for e in after["entries"]}
    assert "audit-prune" in actions
    prune_entry = next(e for e in after["entries"] if e["action"] == "audit-prune")
    assert prune_entry["target"] == "audit.log"
    assert prune_entry["params"]["removed"] == 1
    assert prune_entry["user"] == "tester"


# ---------------------------------------------------------------------------
# UI: the /audit page and its /partials/audit counterpart
# ---------------------------------------------------------------------------


def test_the_audit_page_needs_an_admin(client: TestClient) -> None:
    client.cookies.clear()
    assert client.get("/audit").status_code == 307
    assert _as(client, "user").get("/audit").status_code == 403


def test_the_audit_page_renders_the_nav_entry_and_filter_form(client: TestClient) -> None:
    html = _admin(client).get("/audit").text
    assert 'aria-label="Audit log"' in html
    assert 'id="audit-filters"' in html
    assert 'id="audit-prune-modal"' in html


def test_the_partial_lists_entries_and_respects_filters(
    client: TestClient, config_dir: Path
) -> None:
    write_audit_entry(str(config_dir), user="alice", action="install", target="n8n")
    write_audit_entry(str(config_dir), user="bob", action="stop", target="paperless")
    html = _admin(client).get("/partials/audit").text
    assert "n8n" in html
    assert "paperless" in html

    filtered = _admin(client).get("/partials/audit", params={"user": "bob"}).text
    assert "paperless" in filtered
    assert "n8n" not in filtered


def test_the_partial_reports_a_malformed_filter_as_422(client: TestClient) -> None:
    response = _admin(client).get("/partials/audit", params={"since": "garbage"})
    assert response.status_code == 422


def test_the_empty_state_names_the_reason(client: TestClient) -> None:
    empty = _admin(client).get("/partials/audit").text
    assert "No audit entries yet" in empty


def test_no_match_state_differs_from_the_truly_empty_one(
    client: TestClient, config_dir: Path
) -> None:
    write_audit_entry(str(config_dir), user="alice", action="install", target="n8n")
    filtered = _admin(client).get("/partials/audit", params={"user": "nobody"}).text
    assert "No entries match" in filtered
