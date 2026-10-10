"""The Users page and API: who may call what, what Keycloak is asked, and what is recorded.

The rules themselves are pinned in `test_users_service.py`; this is the HTTP surface around
them: the role that gates it, the status codes, that nothing is written without the CSRF token,
that every change leaves an audit entry, and that a password is never anywhere but in the one
response that creates it.
"""

from __future__ import annotations

import base64
import json
import logging
import os
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
    "PAPAIA_CONFIG_DIR": tempfile.mkdtemp(prefix="papaia-users-api-config-"),
    "PAPAIA_WORKSPACE_DIR": tempfile.mkdtemp(prefix="papaia-users-api-workspace-"),
}.items():
    os.environ.setdefault(_key, _value)

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core.audit import audit_path  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers.users_deps import get_keycloak_transport, get_user_token  # noqa: E402
from tests.fake_keycloak import TOKEN, FakeKeycloak  # noqa: E402

_CSRF = "test-csrf-token-value"
_CSRF_HEADER = {"X-CSRF-Token": _CSRF}
_ME = "11111111-1111-1111-1111-111111111111"
_TOKEN_URL = "https://kc.test/realms/papaia/protocol/openid-connect/token"
_BASE = "/api/v1/users"


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    (directory / "manager").mkdir(parents=True)
    (directory / ".env").write_text("PAPAIA_HOST=https://papaia.test\n", encoding="utf-8")
    return directory


@pytest.fixture
def keycloak() -> FakeKeycloak:
    fake = FakeKeycloak(smtp=True)
    fake.add_user("admin", user_id=_ME, roles=("papaia-admin",))
    return fake


def _make_client(config_dir: Path, keycloak: FakeKeycloak, **overrides: Any) -> TestClient:
    get_settings.cache_clear()
    settings = get_settings().model_copy(
        update={
            "papaia_config_dir": str(config_dir),
            "oidc_issuer_kc_token": _TOKEN_URL,
            "manager_identity_admin_role": "papaia-admin",
            **overrides,
        }
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_keycloak_transport] = lambda: keycloak.transport()
    app.dependency_overrides[get_user_token] = lambda: TOKEN
    return TestClient(app, follow_redirects=False)


@pytest.fixture
def client(config_dir: Path, keycloak: FakeKeycloak) -> Iterator[TestClient]:
    yield _make_client(config_dir, keycloak)
    get_settings.cache_clear()


def _as(client: TestClient, *roles: str, sub: str = _ME) -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": sub,
            "preferred_username": "admin",
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


def _audit(config_dir: Path) -> list[dict[str, Any]]:
    path = audit_path(str(config_dir))
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


# -- who may call it -----------------------------------------------------------------

_READS = [
    "/api/v1/users",
    "/api/v1/users/roles",
    f"/api/v1/users/{_ME}/roles",
    f"/api/v1/users/{_ME}/sessions",
]
_PAGES = ["/users", "/partials/users"]
_WRITES = [
    ("post", "/api/v1/users", {"username": "jane"}),
    ("put", f"/api/v1/users/{_ME}/enabled", {"enabled": False}),
    ("put", f"/api/v1/users/{_ME}/roles", {"roles": []}),
    ("post", f"/api/v1/users/{_ME}/password/temporary", None),
    ("post", f"/api/v1/users/{_ME}/password/email", None),
    ("delete", f"/api/v1/users/{_ME}/sessions", None),
    ("delete", f"/api/v1/users/{_ME}/sessions/{_ME}", None),
]


@pytest.mark.parametrize("roles", [("admin",), ("user",), ("admin", "user")])
@pytest.mark.parametrize("path", [*_READS, *_PAGES])
def test_an_administrator_without_the_identity_role_is_denied(
    client: TestClient, roles: tuple[str, ...], path: str, keycloak: FakeKeycloak
) -> None:
    assert _as(client, *roles).get(path).status_code == 403
    assert keycloak.calls == [], "Keycloak must not be asked on behalf of a denied account"


@pytest.mark.parametrize("roles", [("admin",), ("user",)])
@pytest.mark.parametrize(("method", "path", "body"), _WRITES)
def test_the_writes_are_denied_without_the_identity_role(
    client: TestClient,
    roles: tuple[str, ...],
    method: str,
    path: str,
    body: Any,
    keycloak: FakeKeycloak,
) -> None:
    response = _as(client, *roles).request(method, path, json=body, headers=_CSRF_HEADER)
    assert response.status_code == 403
    assert keycloak.calls == []


@pytest.mark.parametrize("path", [*_READS, *_PAGES])
def test_anonymous_requests_are_turned_away(client: TestClient, path: str) -> None:
    client.cookies.clear()
    expected = 307 if not path.startswith("/api/") and not path.startswith("/partials/") else 401
    response = client.get(path, headers={"HX-Request": "true"} if "partials" in path else {})
    assert response.status_code == expected


@pytest.mark.parametrize(("method", "path", "body"), _WRITES)
def test_anonymous_writes_are_turned_away(
    client: TestClient, method: str, path: str, body: Any
) -> None:
    client.cookies.clear()
    assert client.request(method, path, json=body).status_code == 401


def test_a_session_that_cannot_produce_a_token_is_signed_out(
    config_dir: Path, keycloak: FakeKeycloak
) -> None:
    client = _make_client(config_dir, keycloak)
    client.app.dependency_overrides.pop(get_user_token)  # type: ignore[attr-defined]
    # The session has no refresh token to ask Keycloak with.
    response = _admin(client).get(_BASE)
    assert response.status_code == 401
    assert keycloak.calls == []


# -- where the accounts live ---------------------------------------------------------


def test_the_page_does_not_exist_where_accounts_are_not_in_the_bundled_keycloak(
    config_dir: Path, keycloak: FakeKeycloak
) -> None:
    client = _make_client(config_dir, keycloak, auth_provider="external_oidc")
    for path in ("/users", "/partials/users", _BASE):
        assert _admin(client).get(path).status_code == 404
    assert keycloak.calls == []
    # ... and the sidebar does not offer it.
    assert 'href="/users"' not in _admin(client).get("/audit").text


def test_an_address_that_is_not_a_keycloak_realm_is_reported(
    config_dir: Path, keycloak: FakeKeycloak
) -> None:
    client = _make_client(config_dir, keycloak, oidc_issuer_kc_token="https://kc.test/token")
    response = _admin(client).get(_BASE)
    assert response.status_code == 503
    assert "OIDC_ISSUER_KC_TOKEN" in response.json()["detail"]
    assert keycloak.calls == []


def test_the_identity_role_is_configurable(config_dir: Path, keycloak: FakeKeycloak) -> None:
    client = _make_client(config_dir, keycloak, manager_identity_admin_role="realm-boss")
    assert _as(client, "papaia-admin", "admin").get(_BASE).status_code == 403
    assert _as(client, "realm-boss").get(_BASE).status_code == 200


# -- reading -------------------------------------------------------------------------


def test_the_list_is_read_with_the_users_own_token(
    client: TestClient, keycloak: FakeKeycloak
) -> None:
    keycloak.add_user("jane", email="jane@example.com", roles=("user", "viewer"))
    response = _admin(client).get(_BASE)
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "ok"
    assert body["total"] == 2
    assert body["smtp"] is True
    jane = next(u for u in body["users"] if u["username"] == "jane")
    assert jane["roles"] == ["user", "viewer"]
    assert next(u for u in body["users"] if u["username"] == "admin")["is_self"] is True


def test_a_page_of_the_list_can_be_asked_for(client: TestClient, keycloak: FakeKeycloak) -> None:
    for number in range(12):
        keycloak.add_user(f"user{number:02d}")
    response = _admin(client).get(_BASE, params={"first": 10, "limit": 5, "search": "user"})
    body = response.json()
    assert (body["total"], body["first"], body["limit"], len(body["users"])) == (12, 10, 5, 2)


@pytest.mark.parametrize("query", [{"limit": 0}, {"limit": 101}, {"first": -1}])
def test_a_page_out_of_range_is_refused(client: TestClient, query: dict[str, int]) -> None:
    assert _admin(client).get(_BASE, params=query).status_code == 422


def test_a_missing_keycloak_right_is_a_403_with_the_way_out(
    client: TestClient, keycloak: FakeKeycloak
) -> None:
    keycloak.allowed = False
    response = _admin(client).get(_BASE)
    assert response.status_code == 403
    assert "Associated roles" in response.json()["detail"]


def test_an_unreachable_keycloak_is_a_503(client: TestClient, keycloak: FakeKeycloak) -> None:
    keycloak.down = True
    assert _admin(client).get(_BASE).status_code == 503


def test_a_token_keycloak_refuses_is_a_401(client: TestClient, keycloak: FakeKeycloak) -> None:
    keycloak.token = "another-token"
    assert _admin(client).get(_BASE).status_code == 401


@pytest.mark.parametrize("bad_id", ["not-a-uuid!", "..%2F..", "x" * 80, "ab"])
def test_an_id_that_is_not_one_never_reaches_keycloak(
    client: TestClient, bad_id: str, keycloak: FakeKeycloak
) -> None:
    assert _admin(client).get(f"{_BASE}/{bad_id}/roles").status_code in (404, 422)
    assert keycloak.calls == []


# -- creating ------------------------------------------------------------------------


def test_creating_an_account_returns_its_password_once_and_records_it_nowhere(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    response = _admin(client).post(
        _BASE,
        json={"username": "Jane", "email": "jane@example.com", "first_name": "Jane"},
        headers=_CSRF_HEADER,
    )
    assert response.status_code == 201
    assert response.headers["cache-control"] == "no-store"
    body = response.json()
    password = body["temporary_password"]
    assert password and keycloak.passwords[body["id"]] == (password, True)
    assert body["username"] == "jane"

    (entry,) = _audit(config_dir)
    assert entry["action"] == "user.create"
    assert entry["target"] == "jane"
    assert entry["user"] == "admin"
    assert entry["params"] == {"first_login": "temporary", "has_email": True, "email_sent": False}
    audit_text = audit_path(str(config_dir)).read_text(encoding="utf-8")
    assert password not in audit_text
    assert password not in caplog.text


def test_the_roles_for_a_new_account_come_with_the_dashboard_role_marked(
    client: TestClient,
) -> None:
    body = _admin(client).get(f"{_BASE}/roles").json()
    names = [r["name"] for r in body["roles"]]
    assert "offline_access" not in names and "default-roles-papaia" not in names
    assert [r["name"] for r in body["roles"] if r["default"]] == ["user"]


def test_a_new_account_with_roles_is_audited_as_created_and_as_given_the_roles(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    response = _admin(client).post(
        _BASE, json={"username": "jane", "roles": ["user", "viewer"]}, headers=_CSRF_HEADER
    )
    assert response.status_code == 201
    assert response.json()["roles"] == ["user", "viewer"]
    created_id = response.json()["id"]
    assert keycloak.direct[created_id] >= {"user", "viewer"}
    create, assign = _audit(config_dir)
    assert (create["action"], assign["action"]) == ("user.create", "user.role.assign")
    assert (assign["target"], assign["params"]) == ("jane", {"roles": ["user", "viewer"]})


def test_a_password_keycloak_refuses_leaves_a_partial_entry_and_says_what_is_missing(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    keycloak.reject_passwords = True
    response = _admin(client).post(_BASE, json={"username": "jane"}, headers=_CSRF_HEADER)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "jane was created" in detail and "setting its password" in detail
    (entry,) = _audit(config_dir)
    assert (entry["action"], entry["target"], entry["result"]) == ("user.create", "jane", "partial")
    assert any(u["username"] == "jane" for u in keycloak.users.values())


def test_a_step_that_fails_on_keycloaks_side_is_a_502_with_a_partial_entry(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    keycloak.fail_on = {"POST /role-mappings/realm"}
    response = _admin(client).post(
        _BASE, json={"username": "jane", "roles": ["user"]}, headers=_CSRF_HEADER
    )
    assert response.status_code == 502
    assert "giving it its roles" in response.json()["detail"]
    assert [e["result"] for e in _audit(config_dir)] == ["partial"]


def test_a_mistyped_role_refuses_the_whole_creation(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    response = _admin(client).post(
        _BASE, json={"username": "jane", "roles": ["ghost"]}, headers=_CSRF_HEADER
    )
    assert response.status_code == 422
    assert keycloak.wrote() == []
    assert _audit(config_dir) == []


def test_creating_with_a_mailed_link_sends_it(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    response = _admin(client).post(
        _BASE,
        json={"username": "jane", "email": "jane@example.com", "credential": "email"},
        headers=_CSRF_HEADER,
    )
    body = response.json()
    assert (response.status_code, body["email_sent"], body["temporary_password"]) == (
        201,
        True,
        None,
    )
    assert keycloak.mails == [body["id"]]
    assert _audit(config_dir)[0]["params"]["email_sent"] is True


def test_a_write_without_the_csrf_token_changes_nothing(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    response = _admin(client).post(_BASE, json={"username": "jane"})
    assert response.status_code == 403
    assert keycloak.wrote() == []
    assert _audit(config_dir) == []


@pytest.mark.parametrize(
    "body",
    [
        {"username": "x"},
        {"username": "jane doe"},
        {"username": "jane", "email": "nope"},
        {"username": "jane", "credential": "email"},
        {"username": "jane", "credential": "carrier-pigeon"},
    ],
)
def test_a_request_that_is_not_acceptable_is_refused_before_keycloak(
    client: TestClient, body: dict[str, Any], keycloak: FakeKeycloak, config_dir: Path
) -> None:
    response = _admin(client).post(_BASE, json=body, headers=_CSRF_HEADER)
    assert response.status_code == 422
    assert keycloak.wrote() == []
    assert _audit(config_dir) == []


def test_a_username_that_is_taken_is_a_409_and_leaves_no_entry(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    keycloak.add_user("jane")
    response = _admin(client).post(_BASE, json={"username": "jane"}, headers=_CSRF_HEADER)
    assert response.status_code == 409
    assert _audit(config_dir) == []


def test_a_mail_link_is_refused_where_the_realm_cannot_send(
    client: TestClient, keycloak: FakeKeycloak
) -> None:
    keycloak.smtp = False
    response = _admin(client).post(
        _BASE,
        json={"username": "jane", "email": "jane@example.com", "credential": "email"},
        headers=_CSRF_HEADER,
    )
    assert response.status_code == 422
    assert keycloak.wrote() == []


# -- enabling and disabling ----------------------------------------------------------


def test_disabling_is_audited_with_whether_the_sessions_ended(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    user_id = keycloak.add_user("jane")
    response = _admin(client).put(
        f"{_BASE}/{user_id}/enabled", json={"enabled": False}, headers=_CSRF_HEADER
    )
    assert response.status_code == 200
    assert response.json() == {"username": "jane", "enabled": False, "sessions_ended": True}
    (entry,) = _audit(config_dir)
    assert (entry["action"], entry["target"], entry["params"]) == (
        "user.disable",
        "jane",
        {"sessions_ended": True},
    )


def test_enabling_is_audited(client: TestClient, keycloak: FakeKeycloak, config_dir: Path) -> None:
    user_id = keycloak.add_user("jane", enabled=False)
    _admin(client).put(f"{_BASE}/{user_id}/enabled", json={"enabled": True}, headers=_CSRF_HEADER)
    (entry,) = _audit(config_dir)
    assert (entry["action"], entry["target"]) == ("user.enable", "jane")


def test_nobody_locks_themselves_out(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    response = _admin(client).put(
        f"{_BASE}/{_ME}/enabled", json={"enabled": False}, headers=_CSRF_HEADER
    )
    assert response.status_code == 422
    assert keycloak.wrote() == []
    assert _audit(config_dir) == []


# -- roles ---------------------------------------------------------------------------


def test_the_roles_to_choose_from_are_listed(client: TestClient, keycloak: FakeKeycloak) -> None:
    user_id = keycloak.add_user("jane", roles=("viewer",))
    body = _admin(client).get(f"{_BASE}/{user_id}/roles").json()
    assert body["username"] == "jane"
    names = [r["name"] for r in body["roles"]]
    assert "offline_access" not in names and "default-roles-papaia" not in names
    assert next(r for r in body["roles"] if r["name"] == "viewer")["direct"] is True


def test_roles_are_assigned_and_revoked_with_one_entry_each(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    user_id = keycloak.add_user("jane", roles=("user", "viewer"))
    response = _admin(client).put(
        f"{_BASE}/{user_id}/roles", json={"roles": ["user", "manager-admin"]}, headers=_CSRF_HEADER
    )
    assert response.json() == {
        "username": "jane",
        "added": ["manager-admin"],
        "removed": ["viewer"],
    }
    assign, revoke = _audit(config_dir)
    assert (assign["action"], assign["params"]) == (
        "user.role.assign",
        {"roles": ["manager-admin"]},
    )
    assert (revoke["action"], revoke["params"]) == ("user.role.revoke", {"roles": ["viewer"]})


def test_the_highest_role_can_be_assigned_here(client: TestClient, keycloak: FakeKeycloak) -> None:
    user_id = keycloak.add_user("jane", roles=("user",))
    response = _admin(client).put(
        f"{_BASE}/{user_id}/roles", json={"roles": ["user", "papaia-admin"]}, headers=_CSRF_HEADER
    )
    assert response.status_code == 200
    assert "papaia-admin" in keycloak.direct[user_id]


def test_a_choice_that_changes_nothing_leaves_no_entry(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    user_id = keycloak.add_user("jane", roles=("user",))
    _admin(client).put(f"{_BASE}/{user_id}/roles", json={"roles": ["user"]}, headers=_CSRF_HEADER)
    assert _audit(config_dir) == []


def test_an_unknown_role_is_a_422(client: TestClient, keycloak: FakeKeycloak) -> None:
    user_id = keycloak.add_user("jane")
    response = _admin(client).put(
        f"{_BASE}/{user_id}/roles", json={"roles": ["ghost"]}, headers=_CSRF_HEADER
    )
    assert response.status_code == 422
    assert "ghost" in response.json()["detail"]


def test_nobody_takes_their_own_identity_role_away_here(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    response = _admin(client).put(f"{_BASE}/{_ME}/roles", json={"roles": []}, headers=_CSRF_HEADER)
    assert response.status_code == 422
    assert keycloak.direct[_ME] == {"papaia-admin"}
    assert _audit(config_dir) == []


# -- passwords -----------------------------------------------------------------------


def test_a_temporary_password_is_returned_once_and_not_recorded(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    user_id = keycloak.add_user("jane")
    response = _admin(client).post(f"{_BASE}/{user_id}/password/temporary", headers=_CSRF_HEADER)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    password = response.json()["temporary_password"]
    assert keycloak.passwords[user_id] == (password, True)
    (entry,) = _audit(config_dir)
    assert (entry["action"], entry["target"]) == ("user.password.temporary", "jane")
    assert password not in audit_path(str(config_dir)).read_text(encoding="utf-8")
    assert password not in caplog.text


def test_a_reset_link_is_mailed_and_audited(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    user_id = keycloak.add_user("jane", email="jane@example.com")
    response = _admin(client).post(f"{_BASE}/{user_id}/password/email", headers=_CSRF_HEADER)
    assert response.json() == {"username": "jane", "email_sent": True}
    assert keycloak.mails == [user_id]
    assert _audit(config_dir)[0]["action"] == "user.password.reset-email"


def test_a_reset_link_needs_an_address(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    user_id = keycloak.add_user("jane")
    response = _admin(client).post(f"{_BASE}/{user_id}/password/email", headers=_CSRF_HEADER)
    assert response.status_code == 422
    assert _audit(config_dir) == []


# -- sessions ------------------------------------------------------------------------


def test_the_sessions_of_an_account_are_listed(client: TestClient, keycloak: FakeKeycloak) -> None:
    user_id = keycloak.add_user("jane")
    keycloak.add_session(user_id, ip="10.0.0.9", clients=("librechat",))
    body = _admin(client).get(f"{_BASE}/{user_id}/sessions").json()
    assert body["username"] == "jane"
    assert body["is_self"] is False
    assert body["sessions"][0]["ip_address"] == "10.0.0.9"
    assert body["sessions"][0]["clients"] == ["librechat"]
    assert _admin(client).get(f"{_BASE}/{_ME}/sessions").json()["is_self"] is True


def test_one_session_ends_and_is_audited(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    user_id = keycloak.add_user("jane")
    session = keycloak.add_session(user_id)
    response = _admin(client).delete(f"{_BASE}/{user_id}/sessions/{session}", headers=_CSRF_HEADER)
    assert response.status_code == 204
    assert keycloak.sessions[user_id] == []
    (entry,) = _audit(config_dir)
    assert (entry["action"], entry["target"]) == ("user.session.revoke", "jane")


def test_a_session_id_in_keycloaks_own_format_is_accepted(
    client: TestClient, keycloak: FakeKeycloak
) -> None:
    user_id = keycloak.add_user("jane")
    session = keycloak.add_session(user_id)
    assert len(session) == 24, "not a UUID, like the ones a real Keycloak hands out"
    response = _admin(client).delete(f"{_BASE}/{user_id}/sessions/{session}", headers=_CSRF_HEADER)
    assert response.status_code == 204


@pytest.mark.parametrize("bad", ["short", "has space!!", "a/b/c/d/e/f/g/h", "x" * 200])
def test_a_session_id_that_is_not_one_never_reaches_keycloak(
    client: TestClient, keycloak: FakeKeycloak, bad: str
) -> None:
    user_id = keycloak.add_user("jane")
    calls = len(keycloak.calls)
    response = _admin(client).delete(f"{_BASE}/{user_id}/sessions/{bad}", headers=_CSRF_HEADER)
    assert response.status_code in (404, 422)
    assert len(keycloak.calls) == calls


def test_a_session_that_is_not_the_accounts_is_a_404(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    jane = keycloak.add_user("jane")
    foreign = keycloak.add_session(keycloak.add_user("other"))
    response = _admin(client).delete(f"{_BASE}/{jane}/sessions/{foreign}", headers=_CSRF_HEADER)
    assert response.status_code == 404
    assert _audit(config_dir) == []


def test_all_sessions_end_and_are_counted_in_the_entry(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    user_id = keycloak.add_user("jane")
    keycloak.add_session(user_id)
    keycloak.add_session(user_id)
    response = _admin(client).delete(f"{_BASE}/{user_id}/sessions", headers=_CSRF_HEADER)
    assert response.json() == {"username": "jane", "ended": 2}
    (entry,) = _audit(config_dir)
    assert (entry["action"], entry["params"]) == ("user.session.revoke-all", {"sessions": 2})


def test_nobody_ends_all_of_their_own_sessions_here(
    client: TestClient, keycloak: FakeKeycloak
) -> None:
    response = _admin(client).delete(f"{_BASE}/{_ME}/sessions", headers=_CSRF_HEADER)
    assert response.status_code == 422


# -- the audit trail covers every change ---------------------------------------------

_AUDITED = {
    ("POST", "/api/v1/users"): "user.create",
    ("PUT", "/api/v1/users/{user_id}/enabled"): "user.enable|user.disable",
    ("PUT", "/api/v1/users/{user_id}/roles"): "user.role.assign|user.role.revoke",
    ("POST", "/api/v1/users/{user_id}/password/temporary"): "user.password.temporary",
    ("POST", "/api/v1/users/{user_id}/password/email"): "user.password.reset-email",
    ("DELETE", "/api/v1/users/{user_id}/sessions/{session_id}"): "user.session.revoke",
    ("DELETE", "/api/v1/users/{user_id}/sessions"): "user.session.revoke-all",
}


def test_every_route_that_changes_something_is_one_this_module_audits(client: TestClient) -> None:
    """A new mutating route has to be added to `_AUDITED` -- and so to a test above."""
    # The routes of an included router are not listed on the app in this FastAPI; its schema
    # is the complete list.
    paths: dict[str, dict[str, Any]] = client.app.openapi()["paths"]  # type: ignore[attr-defined]
    changing = {
        (method.upper(), path)
        for path, operations in paths.items()
        if path.startswith(_BASE)
        for method in operations
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}
    }
    assert changing == set(_AUDITED)


# -- the pages -----------------------------------------------------------------------


def test_the_page_has_its_list_and_its_dialogs(client: TestClient) -> None:
    response = _admin(client).get("/users")
    assert response.status_code == 200
    text = response.text
    assert 'id="user-list"' in text
    assert "New user" in text
    for dialog in (
        "new-user-modal",
        "user-secret-modal",
        "user-roles-modal",
        "user-sessions-modal",
        "user-password-modal",
        "user-toggle-modal",
    ):
        assert f'id="{dialog}"' in text


def test_the_sidebar_offers_the_page_to_the_identity_role_only(client: TestClient) -> None:
    assert 'href="/users"' in _admin(client).get("/audit").text
    assert 'href="/users"' not in _as(client, "admin").get("/audit").text


def test_the_list_partial_shows_the_accounts_and_no_dialogs(
    client: TestClient, keycloak: FakeKeycloak
) -> None:
    keycloak.add_user(
        "jane",
        email="jane@example.com",
        first="Jane",
        last="Doe",
        roles=("viewer",),
        actions=("UPDATE_PASSWORD",),
    )
    keycloak.add_user("gone", enabled=False)
    response = _admin(client).get("/partials/users")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    text = response.text
    assert "jane" in text and "Jane Doe" in text and "jane@example.com" in text
    assert "Must change password" in text
    assert "Disabled" in text
    assert "viewer" in text
    assert 'data-smtp="true"' in text
    assert "You" in text, "the signed-in account is marked"
    assert "<dialog" not in text, "the dialogs live on the page and survive a reload of the list"
    assert "Enable account" in text and "Disable account" in text


def test_the_signed_in_account_cannot_be_disabled_from_its_own_row(client: TestClient) -> None:
    text = _admin(client).get("/partials/users").text
    assert "Disable account" not in text


def test_the_partial_says_when_nothing_matches(client: TestClient) -> None:
    text = _admin(client).get("/partials/users", params={"search": "nobody-by-that-name"}).text
    assert "No user matches" in text


def test_the_partial_explains_a_missing_right(client: TestClient, keycloak: FakeKeycloak) -> None:
    keycloak.allowed = False
    response = _admin(client).get("/partials/users")
    assert response.status_code == 200
    assert "cannot manage users yet" in response.text
    assert "Associated roles" in response.text


def test_the_partial_explains_an_unreachable_keycloak(
    client: TestClient, keycloak: FakeKeycloak
) -> None:
    keycloak.down = True
    response = _admin(client).get("/partials/users")
    assert response.status_code == 200
    assert "Keycloak does not answer" in response.text


def test_the_partial_pages_with_prev_and_next(client: TestClient, keycloak: FakeKeycloak) -> None:
    for number in range(30):
        keycloak.add_user(f"user{number:02d}")
    text = _admin(client).get("/partials/users").text
    assert "1–25 of 31" in text
    assert '"first": 25' in text
