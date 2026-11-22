"""The administration overview of delegations: counts per module and person, revoke per person.

A delegation is an offline session of a person at the client of its holder (`flow-engine` for
Flow, `assist-gate` for Assistant; sicherheit 1.10). The overview shows how many there are per
module and per person and nothing else: no session id, address, time, purpose or token
(PLT-58-A8). Revoking removes the person's consent at the holder's client, which ends all of
that person's delegations there, and leaves one entry in the local audit log and one
`delegation_revoked` row in the outbox.
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import tempfile
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
    "PAPAIA_CONFIG_DIR": tempfile.mkdtemp(prefix="papaia-delegations-config-"),
    "PAPAIA_WORKSPACE_DIR": tempfile.mkdtemp(prefix="papaia-delegations-workspace-"),
}.items():
    os.environ.setdefault(_key, _value)

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402
from papaia_kit.audit import validate_event  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core import kit_audit  # noqa: E402
from app.core.audit import audit_path  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers.users_deps import get_keycloak_transport, get_user_token  # noqa: E402
from tests.fake_keycloak import TOKEN, FakeKeycloak  # noqa: E402

_CSRF = "test-csrf-token-value"
_ME = "11111111-1111-1111-1111-111111111111"
_TOKEN_URL = "https://kc.test/realms/papaia/protocol/openid-connect/token"
_BASE = "/api/v1/delegations"


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    (directory / "manager").mkdir(parents=True)
    return directory


@pytest.fixture
def keycloak() -> FakeKeycloak:
    fake = FakeKeycloak()
    fake.add_user("dora", user_id=_ME, roles=("papaia-admin",))
    anna = fake.add_user("anna", user_id="22222222-2222-2222-2222-222222222222")
    ben = fake.add_user("ben", user_id="33333333-3333-3333-3333-333333333333")
    fake.add_offline_session(anna, "flow-engine")
    fake.add_offline_session(anna, "flow-engine")
    fake.add_offline_session(ben, "flow-engine")
    fake.add_offline_session(anna, "assist-gate")
    return fake


@pytest.fixture
def client(config_dir: Path, keycloak: FakeKeycloak) -> Iterator[TestClient]:
    get_settings.cache_clear()
    settings = get_settings().model_copy(
        update={
            "papaia_config_dir": str(config_dir),
            "oidc_issuer_kc_token": _TOKEN_URL,
            "manager_identity_admin_role": "papaia-admin",
        }
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_keycloak_transport] = lambda: keycloak.transport()
    app.dependency_overrides[get_user_token] = lambda: TOKEN
    yield TestClient(app, follow_redirects=False)
    get_settings.cache_clear()


def _as(client: TestClient, *roles: str) -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": _ME,
            "preferred_username": "dora",
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
    return _as(client, "papaia-admin", "admin")


def _outbox(config_dir: Path) -> list[dict[str, Any]]:
    path = kit_audit.outbox_path(str(config_dir))
    if not path.exists():
        return []
    with sqlite3.connect(path) as conn:
        found = conn.execute("SELECT body FROM audit_outbox ORDER BY source_seq").fetchall()
    return [json.loads(body) for (body,) in found]


def _log(config_dir: Path) -> list[dict[str, Any]]:
    path = audit_path(str(config_dir))
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# -- the overview ----------------------------------------------------------------------


def test_the_overview_counts_per_module_and_person(client: TestClient) -> None:
    response = _admin(client).get(_BASE)
    assert response.status_code == 200
    assert response.json() == {
        "modules": [
            {
                "module": "flow",
                "title": "Flow",
                "available": True,
                "total": 3,
                "truncated": False,
                "persons": [
                    {"user_id": "22222222-2222-2222-2222-222222222222", "username": "anna",
                     "count": 2},
                    {"user_id": "33333333-3333-3333-3333-333333333333", "username": "ben",
                     "count": 1},
                ],
            },
            {
                "module": "assistant",
                "title": "Assistant",
                "available": True,
                "total": 1,
                "truncated": False,
                "persons": [
                    {"user_id": "22222222-2222-2222-2222-222222222222", "username": "anna",
                     "count": 1},
                ],
            },
        ]
    }


def test_the_overview_shows_counts_only(client: TestClient, keycloak: FakeKeycloak) -> None:
    text = _admin(client).get(_BASE).text
    session_ids = [row["id"] for rows in keycloak.offline.values() for row in rows]
    for leaked in ("10.0.0.7", "1760000000000", "1760000600000", "ipAddress", "lastAccess",
                   "start", "clients", "secret", *session_ids):
        assert leaked not in text


def test_a_holder_that_is_not_installed_shows_as_unavailable(
    client: TestClient, keycloak: FakeKeycloak
) -> None:
    del keycloak.clients["assist-gate"]
    assistant = _admin(client).get(_BASE).json()["modules"][1]
    assert assistant == {
        "module": "assistant",
        "title": "Assistant",
        "available": False,
        "total": 0,
        "truncated": False,
        "persons": [],
    }


def test_the_overview_is_read_and_writes_nothing(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    assert _admin(client).get(_BASE).status_code == 200
    assert keycloak.wrote() == []
    assert _outbox(config_dir) == []


def test_the_overview_needs_the_identity_admin_role(client: TestClient) -> None:
    assert _as(client, "admin").get(_BASE).status_code == 403
    assert _as(client, "user").get(_BASE).status_code == 403


def test_keycloak_refusing_the_account_is_a_403(
    client: TestClient, keycloak: FakeKeycloak
) -> None:
    keycloak.allowed = False
    assert _admin(client).get(_BASE).status_code == 403


# -- revoking per person ---------------------------------------------------------------


def _revoke(client: TestClient, module: str, user_id: str, **headers: str) -> Any:
    return _admin(client).delete(
        f"{_BASE}/{module}/persons/{user_id}", headers={"X-CSRF-Token": _CSRF, **headers}
    )


def test_revoking_ends_the_persons_delegations_at_that_module_only(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    anna = "22222222-2222-2222-2222-222222222222"
    response = _revoke(client, "flow", anna, **{"X-Papaia-Correlation-Id": "corr-revoke-01"})
    assert response.status_code == 204
    assert keycloak.wrote() == [("DELETE", f"/users/{anna}/consents/flow-engine")]
    assert [r["userId"] for r in keycloak.offline["flow-engine"]] == [
        "33333333-3333-3333-3333-333333333333"
    ]
    assert [r["userId"] for r in keycloak.offline["assist-gate"]] == [anna]

    [entry] = _log(config_dir)
    assert (entry["action"], entry["target"], entry["result"]) == (
        "delegation.revoke", f"flow:{anna}", "ok"
    )
    [row] = _outbox(config_dir)
    validate_event(row)
    assert row["event"] == "delegation_revoked"
    assert row["result"] == "allowed"
    assert row["sub"] == _ME
    assert row["identity_kind"] == "person"
    assert row["operation"] == "delegation.revoke"
    assert row["effect"] == "delete"
    assert row["object_ref"] == {"type": "person", "id": anna}
    assert row["context"] == {"delegation": "flow-engine"}
    assert row["correlation_id"] == "corr-revoke-01"


def test_revoking_without_a_delegation_is_a_404_with_a_row(
    client: TestClient, config_dir: Path
) -> None:
    response = _revoke(client, "assistant", "33333333-3333-3333-3333-333333333333")
    assert response.status_code == 404
    [row] = _outbox(config_dir)
    assert (row["event"], row["result"], row["error_code"]) == (
        "delegation_revoked", "error", "not_found"
    )


def test_revoking_needs_the_csrf_token(client: TestClient, keycloak: FakeKeycloak) -> None:
    response = _admin(client).delete(
        f"{_BASE}/flow/persons/22222222-2222-2222-2222-222222222222"
    )
    assert response.status_code == 403
    assert keycloak.wrote() == []


def test_revoking_needs_the_identity_admin_role(
    client: TestClient, keycloak: FakeKeycloak
) -> None:
    response = _as(client, "admin").delete(
        f"{_BASE}/flow/persons/22222222-2222-2222-2222-222222222222",
        headers={"X-CSRF-Token": _CSRF},
    )
    assert response.status_code == 403
    assert keycloak.wrote() == []


@pytest.mark.parametrize(
    ("module", "user_id", "status"),
    [
        ("studio", "22222222-2222-2222-2222-222222222222", 404),
        ("flow-engine", "22222222-2222-2222-2222-222222222222", 404),
        ("flow", "not a uuid", 422),
        ("flow", "..%2F..%2Fclients", 404),
    ],
)
def test_revoking_validates_module_and_person(
    client: TestClient, keycloak: FakeKeycloak, module: str, user_id: str, status: int
) -> None:
    assert _revoke(client, module, user_id).status_code == status
    assert keycloak.wrote() == []
