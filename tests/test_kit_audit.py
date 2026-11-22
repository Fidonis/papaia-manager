"""Every write of the manager leaves one `papaia.audit/1` row in the kit's outbox.

The manager has no database of its own, so the rows go to an outbox file written with the
kit's `FileAuditWriter` (governance GV-01, decision E-52) right after the entry of the local
audit log. These tests pin what a row carries: who acted (person from the session, or the
manager's own service identity outside a request), the correlation id of the request, the
operation and its result -- and what it never carries: targets, parameters, secrets.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sqlite3
import tempfile
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

for _key, _value in {
    "OIDC_ISSUER_KC_AUTH": "https://kc.test/auth",
    "OIDC_ISSUER_KC_TOKEN": "https://kc.test/token",
    "OIDC_ISSUER_KC_CERTS": "https://kc.test/certs",
    "MANAGER_ADMIN_ROLE": "admin",
    "MANAGER_USER_ROLE": "user",
    "MANAGER_HOST": "http://localhost:8120",
    "MANAGER_OIDC_CLIENT_SECRET": "client-secret",
    "MANAGER_SESSION_SECRET": "test-session-secret-value",
    "PAPAIA_CONFIG_DIR": tempfile.mkdtemp(prefix="papaia-kit-audit-config-"),
    "PAPAIA_WORKSPACE_DIR": tempfile.mkdtemp(prefix="papaia-kit-audit-workspace-"),
}.items():
    os.environ.setdefault(_key, _value)

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402
from papaia_kit.audit import validate_event  # noqa: E402
from papaia_kit.correlation import is_valid_correlation_id  # noqa: E402

from app import main as app_main  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core import kit_audit  # noqa: E402
from app.core.audit import write_audit_entry  # noqa: E402
from app.core.jobs import JobContext, JobQueue, JobStatus  # noqa: E402
from app.main import create_app  # noqa: E402

_CSRF = "test-csrf-token-value"
_SUB = "6f1c2a9e-0000-4000-8000-000000000001"


def rows(config_dir: Path) -> list[dict[str, Any]]:
    path = kit_audit.outbox_path(str(config_dir))
    if not path.exists():
        return []
    with sqlite3.connect(path) as conn:
        found = conn.execute("SELECT body FROM audit_outbox ORDER BY source_seq").fetchall()
    return [json.loads(body) for (body,) in found]


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
    app_main._job_queue = None  # noqa: SLF001


def _admin(client: TestClient) -> TestClient:
    session = {
        "user": {
            "sub": _SUB,
            "preferred_username": "dora",
            "roles": ["admin"],
            "exp": int(time.time()) + 3600,
        },
        "_csrf_token": _CSRF,
    }
    payload = base64.b64encode(json.dumps(session).encode())
    signed = TimestampSigner(get_settings().manager_session_secret).sign(payload).decode()
    client.cookies.clear()
    client.cookies.set("papaia_manager_session", signed)
    return client


def _create_catalog(client: TestClient, name: str, **headers: str) -> Any:
    return _admin(client).post(
        "/api/v1/catalogs",
        json={"name": name, "type": "git", "url": "https://addons.example.org/private.git"},
        headers={"X-CSRF-Token": _CSRF, **headers},
    )


# -- one row per write, from a request ---------------------------------------------------


def test_a_write_through_the_api_leaves_one_row_for_the_person(
    client: TestClient, config_dir: Path
) -> None:
    response = _create_catalog(client, "extra", **{"X-Papaia-Correlation-Id": "corr-mgr-0001"})
    assert response.status_code == 201
    assert response.headers["X-Papaia-Correlation-Id"] == "corr-mgr-0001"

    [row] = rows(config_dir)
    validate_event(row)
    assert row["format"] == "papaia.audit/1"
    assert row["source_seq"] == 1
    assert row["writer"] == "papaia-manager"
    assert row["event"] == "operation"
    assert row["role"] == "caller"
    assert row["sub"] == _SUB
    assert row["identity_kind"] == "person"
    assert row["service_account"] is None
    assert row["azp"] == "papaia-manager"
    assert row["catalog_id"] == "manager"
    assert row["operation"] == "catalog_create"
    assert row["effect"] == "write"
    assert row["result"] == "allowed"
    assert row["correlation_id"] == "corr-mgr-0001"


def test_a_row_carries_no_target_parameter_or_name(client: TestClient, config_dir: Path) -> None:
    assert _create_catalog(client, "secretive").status_code == 201
    text = json.dumps(rows(config_dir))
    for value in ("secretive", "addons.example.org", "dora"):
        assert value not in text


def test_each_write_gets_its_own_row_in_order(client: TestClient, config_dir: Path) -> None:
    assert _create_catalog(client, "first").status_code == 201
    deleted = _admin(client).delete("/api/v1/catalogs/first", headers={"X-CSRF-Token": _CSRF})
    assert deleted.status_code in (200, 204)

    found = rows(config_dir)
    assert [r["source_seq"] for r in found] == [1, 2]
    assert [(r["operation"], r["effect"]) for r in found] == [
        ("catalog_create", "write"),
        ("catalog_delete", "delete"),
    ]
    # Each request is its own correlation.
    assert found[0]["correlation_id"] != found[1]["correlation_id"]
    assert all(is_valid_correlation_id(r["correlation_id"]) for r in found)


def test_a_read_leaves_no_row(client: TestClient, config_dir: Path) -> None:
    assert _admin(client).get("/api/v1/catalogs").status_code == 200
    assert rows(config_dir) == []


def test_an_oversized_correlation_id_is_replaced_not_shortened(
    client: TestClient, config_dir: Path
) -> None:
    sent = "a" * 5000
    response = _create_catalog(client, "long", **{"X-Papaia-Correlation-Id": sent})
    assert response.status_code == 201
    [row] = rows(config_dir)
    assert is_valid_correlation_id(row["correlation_id"])
    assert not sent.startswith(row["correlation_id"])
    assert response.headers["X-Papaia-Correlation-Id"] == row["correlation_id"]


def test_a_token_shaped_correlation_id_is_replaced(client: TestClient, config_dir: Path) -> None:
    sent = "ghp_" + "x" * 36
    assert _create_catalog(client, "tok", **{"X-Papaia-Correlation-Id": sent}).status_code == 201
    [row] = rows(config_dir)
    assert row["correlation_id"] != sent
    assert "ghp_" not in json.dumps(row)


# -- outside a request: the manager's own identity ---------------------------------------


def test_a_write_outside_a_request_is_the_service_identity(config_dir: Path) -> None:
    write_audit_entry(
        str(config_dir),
        user="scheduler",
        action="backup.schedule.skip",
        target="/backups",
        result="skipped",
        params={"reason": "busy"},
    )
    [row] = rows(config_dir)
    validate_event(row)
    assert row["identity_kind"] == "service"
    assert row["service_account"] == "papaia-manager"
    assert row["sub"] == "papaia-manager"
    assert row["operation"] == "backup.schedule.skip"
    assert row["result"] == "error"
    assert row["error_code"] == "skipped"
    assert "busy" not in json.dumps(row)


@pytest.mark.parametrize(
    ("action", "operation", "effect"),
    [
        ("catalog-create", "catalog_create", "write"),
        ("catalog:refresh", "catalog.refresh", "write"),
        ("/account/language", "account.language", "write"),
        ("rag.collection.delete", "rag.collection.delete", "delete"),
        ("images-prune", "images_prune", "delete"),
        ("uninstall", "uninstall", "delete"),
        ("mail.test-message", "mail.test_message", "send"),
        ("notify.", "notify", "send"),
        ("notify.sent", "notify.sent", "send"),
        ("Bad Action!", "manager.write", "write"),
        ("", "manager.write", "write"),
    ],
)
def test_actions_map_to_operation_names_and_effects(
    action: str, operation: str, effect: str
) -> None:
    assert kit_audit.operation_name(action) == operation
    assert kit_audit.effect_of(action) == effect


@pytest.mark.parametrize(
    ("action", "result", "expected", "code"),
    [
        ("install", "ok", "allowed", None),
        ("upgrade", "started", "allowed", None),
        ("restore-scoped", "partial", "error", "partial"),
        ("install", "failed", "error", "failed"),
        ("install", "error", "error", "error"),
        ("step-up.refused", "failed", "denied", "step_up_required"),
        ("install", "denied", "denied", "denied"),
    ],
)
def test_results_map_to_the_three_audit_results(
    config_dir: Path, action: str, result: str, expected: str, code: str | None
) -> None:
    write_audit_entry(str(config_dir), user="x", action=action, target="t", result=result)
    [row] = rows(config_dir)
    validate_event(row)
    assert (row["result"], row.get("error_code")) == (expected, code)


def test_an_outbox_failure_does_not_fail_the_write(
    config_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Availability before completeness of delivery (GV-01): the effect has happened."""
    blocker = config_dir / "manager" / "audit"
    blocker.write_text("not a directory", encoding="utf-8")
    write_audit_entry(str(config_dir), user="x", action="install", target="secret-target")
    entries = (config_dir / "manager" / "audit.log").read_text(encoding="utf-8")
    assert '"install"' in entries
    assert "papaia.audit/1 row not written" in caplog.text
    assert "secret-target" not in caplog.text


# -- jobs keep the identity and correlation of the request that queued them --------------


def test_a_job_row_carries_the_person_who_queued_it(config_dir: Path) -> None:
    async def scenario() -> list[str]:
        queue = JobQueue(str(config_dir))
        queue.start()

        async def succeeds(ctx: JobContext) -> None:
            write_audit_entry(str(config_dir), user="dora", action="install", target="flow")

        async def fails(ctx: JobContext) -> None:
            raise RuntimeError("boom")

        with kit_audit.bound(kit_audit.person(_SUB, azp="papaia-manager"), "corr-job-0001"):
            jobs = [
                await queue.enqueue(action="install", target="flow", user="dora", callback=cb)
                for cb in (succeeds, fails)
            ]
        for _ in range(200):
            if all(j.status in (JobStatus.SUCCEEDED, JobStatus.FAILED) for j in jobs):
                break
            await asyncio.sleep(0.01)
        statuses = [j.status.value for j in jobs]
        queue.stop()
        return statuses

    assert asyncio.run(scenario()) == ["succeeded", "failed"]
    found = rows(config_dir)
    assert [r["result"] for r in found] == ["allowed", "error"]
    assert {r["sub"] for r in found} == {_SUB}
    assert {r["identity_kind"] for r in found} == {"person"}
    assert {r["correlation_id"] for r in found} == {"corr-job-0001"}


# -- delivery ----------------------------------------------------------------------------


class _Ingest:
    """Acknowledges every line it receives, like papaia-audit for a new source."""

    def __init__(self) -> None:
        self.received: list[dict[str, Any]] = []

    def send(self, body: bytes) -> bytes:
        lines = [json.loads(line) for line in body.decode().splitlines() if line]
        self.received.extend(lines)
        acks = [{"source_seq": r["source_seq"], "seq": r["source_seq"]} for r in lines]
        return json.dumps({"acknowledged": acks}).encode()


def test_delivery_sends_the_outbox_and_empties_it(config_dir: Path) -> None:
    write_audit_entry(str(config_dir), user="x", action="install", target="t")
    write_audit_entry(str(config_dir), user="x", action="remove", target="t")
    ingest = _Ingest()
    delivery = kit_audit.start_delivery(str(config_dir), ingest, idle_interval=0.01)
    try:
        for _ in range(300):
            if not rows(config_dir) and len(ingest.received) == 2:
                break
            time.sleep(0.01)
    finally:
        delivery.stop()
    assert [r["operation"] for r in ingest.received] == ["install", "remove"]
    assert rows(config_dir) == []
    assert not any(t.name == "papaia-audit-delivery" for t in threading.enumerate())


def test_without_client_certificates_delivery_is_not_configured(tmp_path: Path) -> None:
    get_settings.cache_clear()
    settings = get_settings().model_copy(
        update={
            "audit_tls_ca_file": str(tmp_path / "missing-ca.pem"),
            "audit_tls_cert_file": str(tmp_path / "missing-client.pem"),
            "audit_tls_key_file": str(tmp_path / "missing-client.key"),
        }
    )
    assert kit_audit.ingest_transport(settings) is None
    get_settings.cache_clear()


def test_the_ingest_url_must_be_https() -> None:
    get_settings.cache_clear()
    with pytest.raises(ValueError):
        get_settings().model_copy(update={}).model_validate(
            {**get_settings().model_dump(), "audit_ingest_url": "http://papaia-audit:8443/v1/ingest"}
        )
    get_settings.cache_clear()
