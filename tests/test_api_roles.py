"""The Roles page and API: who may call what, what Keycloak is asked, and what is recorded.

The rules themselves are pinned in `test_roles_service.py`; this is the HTTP surface around
them: the role that gates it, the status codes, that nothing is written without the CSRF token,
that every change leaves an audit entry, and what is said when Keycloak refuses.
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

for _key, _value in {
    "OIDC_ISSUER_KC_AUTH": "https://kc.test/auth",
    "OIDC_ISSUER_KC_TOKEN": "https://kc.test/token",
    "OIDC_ISSUER_KC_CERTS": "https://kc.test/certs",
    "MANAGER_ADMIN_ROLE": "admin",
    "MANAGER_USER_ROLE": "user",
    "MANAGER_HOST": "http://localhost:8120",
    "MANAGER_OIDC_CLIENT_SECRET": "client-secret",
    "MANAGER_SESSION_SECRET": "test-session-secret-value",
    "PAPAIA_CONFIG_DIR": tempfile.mkdtemp(prefix="papaia-roles-api-config-"),
    "PAPAIA_WORKSPACE_DIR": tempfile.mkdtemp(prefix="papaia-roles-api-workspace-"),
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
_BASE = "/api/v1/roles"


def _add_role(
    fake: FakeKeycloak, name: str, *, description: str = "", members: tuple[str, ...] = ()
) -> None:
    fake.roles[name] = {
        "id": f"id-{name}",
        "name": name,
        "description": description,
        "composite": bool(members),
        "clientRole": False,
    }
    if members:
        fake.composites[name] = list(members)


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


def _as(client: TestClient, *roles: str) -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": _ME,
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

_READS = [_BASE, f"{_BASE}/viewer"]
_PAGES = ["/users/roles", "/partials/roles"]
_WRITES = [
    ("post", _BASE, {"name": "sales"}),
    ("put", f"{_BASE}/sales", {"description": "x", "members": []}),
    ("delete", f"{_BASE}/sales", None),
]


@pytest.mark.parametrize("roles", [("admin",), ("user",), ("admin", "user")])
@pytest.mark.parametrize("path", [*_READS, *_PAGES])
def test_an_administrator_without_the_identity_role_is_denied(
    client: TestClient, keycloak: FakeKeycloak, roles: tuple[str, ...], path: str
) -> None:
    assert _as(client, *roles).get(path).status_code == 403
    assert keycloak.calls == []


@pytest.mark.parametrize("roles", [("admin",), ("user",)])
@pytest.mark.parametrize(("method", "path", "body"), _WRITES)
def test_the_writes_are_denied_without_the_identity_role(
    client: TestClient,
    keycloak: FakeKeycloak,
    roles: tuple[str, ...],
    method: str,
    path: str,
    body: Any,
) -> None:
    response = _as(client, *roles).request(method, path, json=body, headers=_CSRF_HEADER)
    assert response.status_code == 403
    assert keycloak.calls == []


@pytest.mark.parametrize(("method", "path", "body"), _WRITES)
def test_anonymous_writes_are_turned_away(
    client: TestClient, method: str, path: str, body: Any
) -> None:
    client.cookies.clear()
    assert client.request(method, path, json=body).status_code == 401


def test_the_page_does_not_exist_where_accounts_are_not_in_the_bundled_keycloak(
    config_dir: Path, keycloak: FakeKeycloak
) -> None:
    client = _make_client(config_dir, keycloak, auth_provider="external_oidc")
    for path in ("/users/roles", "/partials/roles", _BASE):
        assert _admin(client).get(path).status_code == 404
    assert keycloak.calls == []


# -- reading -------------------------------------------------------------------------


def test_the_roles_are_listed_with_what_they_contain(
    client: TestClient, keycloak: FakeKeycloak
) -> None:
    _add_role(keycloak, "sales", description="Sales team", members=("user",))
    body = _admin(client).get(_BASE).json()
    assert body["state"] == "ok"
    by_name = {r["name"]: r for r in body["roles"]}
    assert by_name["sales"]["members"] == ["user"]
    assert by_name["sales"]["built_in"] is False
    assert by_name["papaia-admin"]["built_in"] is True
    assert "offline_access" not in by_name


def test_a_role_is_read_with_the_accounts_that_hold_it(
    client: TestClient, keycloak: FakeKeycloak
) -> None:
    _add_role(keycloak, "sales")
    keycloak.add_user("anna", roles=("sales",))
    body = _admin(client).get(f"{_BASE}/sales").json()
    assert body["name"] == "sales" and body["users"] == ["anna"] and body["users_more"] is False


def test_a_missing_role_is_a_404(client: TestClient) -> None:
    assert _admin(client).get(f"{_BASE}/ghost").status_code == 404


def test_a_missing_right_to_read_is_a_403_with_the_way_out(
    client: TestClient, keycloak: FakeKeycloak
) -> None:
    keycloak.allowed = False
    response = _admin(client).get(_BASE)
    assert response.status_code == 403
    assert "manage-realm" in response.json()["detail"]


def test_an_unreachable_keycloak_is_a_503(client: TestClient, keycloak: FakeKeycloak) -> None:
    keycloak.down = True
    assert _admin(client).get(_BASE).status_code == 503


# -- creating ------------------------------------------------------------------------


def test_a_role_is_created_and_audited(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    response = _admin(client).post(
        _BASE,
        json={"name": "sales", "description": "Sales team", "members": ["viewer", "user"]},
        headers=_CSRF_HEADER,
    )
    assert response.status_code == 201
    body = response.json()
    assert (body["name"], body["members"], body["built_in"]) == ("sales", ["user", "viewer"], False)
    assert keycloak.roles["sales"]["description"] == "Sales team"
    (entry,) = _audit(config_dir)
    assert (entry["action"], entry["target"], entry["user"]) == ("role.create", "sales", "admin")
    assert entry["params"] == {"described": True, "members": ["user", "viewer"]}


def test_a_write_without_the_csrf_token_changes_nothing(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    response = _admin(client).post(_BASE, json={"name": "sales"})
    assert response.status_code == 403
    assert keycloak.wrote() == []
    assert _audit(config_dir) == []


@pytest.mark.parametrize(
    "body",
    [
        {"name": ""},
        {"name": "sales team"},
        {"name": "offline_access"},
        {"name": "sales", "description": "x" * 300},
        {"name": "sales", "members": ["ghost"]},
    ],
)
def test_a_request_that_is_not_acceptable_is_refused_before_keycloak_writes(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path, body: dict[str, Any]
) -> None:
    response = _admin(client).post(_BASE, json=body, headers=_CSRF_HEADER)
    assert response.status_code == 422
    assert keycloak.wrote() == []
    assert _audit(config_dir) == []


def test_a_role_that_exists_is_a_409(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    _add_role(keycloak, "sales")
    response = _admin(client).post(_BASE, json={"name": "sales"}, headers=_CSRF_HEADER)
    assert response.status_code == 409
    assert _audit(config_dir) == []


def test_a_missing_manage_realm_right_says_which_right_is_missing(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    keycloak.manage_realm = False
    response = _admin(client).post(_BASE, json={"name": "sales"}, headers=_CSRF_HEADER)
    assert response.status_code == 403
    detail = response.json()["detail"]
    assert "manage-realm" in detail and "papaia-admin" in detail and "Associated roles" in detail
    assert "sales" not in keycloak.roles
    assert _audit(config_dir) == []


def test_members_that_fail_to_attach_leave_a_partial_entry_and_say_what_is_missing(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    keycloak.fail_on = {"POST /roles/composites"}
    response = _admin(client).post(
        _BASE, json={"name": "sales", "members": ["user"]}, headers=_CSRF_HEADER
    )
    assert response.status_code == 502
    assert "sales was created" in response.json()["detail"]
    assert [e["result"] for e in _audit(config_dir)] == ["partial"]
    assert "sales" in keycloak.roles


# -- changing ------------------------------------------------------------------------


def test_a_role_is_changed_and_audited(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    _add_role(keycloak, "sales", description="old", members=("user",))
    response = _admin(client).put(
        f"{_BASE}/sales",
        json={"description": "new", "members": ["viewer"]},
        headers=_CSRF_HEADER,
    )
    assert response.status_code == 200
    assert response.json() == {
        "name": "sales",
        "description_changed": True,
        "added": ["viewer"],
        "removed": ["user"],
    }
    assert keycloak.roles["sales"]["description"] == "new"
    (entry,) = _audit(config_dir)
    assert (entry["action"], entry["target"]) == ("role.update", "sales")
    assert entry["params"] == {
        "description_changed": True,
        "added": ["viewer"],
        "removed": ["user"],
    }


def test_a_change_that_changes_nothing_leaves_no_entry(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    _add_role(keycloak, "sales", description="same", members=("user",))
    response = _admin(client).put(
        f"{_BASE}/sales", json={"description": "same", "members": ["user"]}, headers=_CSRF_HEADER
    )
    assert response.status_code == 200
    assert _audit(config_dir) == []


def test_a_built_in_role_is_not_changed(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    response = _admin(client).put(
        f"{_BASE}/papaia-admin", json={"description": "x", "members": []}, headers=_CSRF_HEADER
    )
    assert response.status_code == 422
    assert "built-in" in response.json()["detail"]
    assert keycloak.wrote() == []
    assert _audit(config_dir) == []


def test_a_loop_is_refused(client: TestClient, keycloak: FakeKeycloak, config_dir: Path) -> None:
    _add_role(keycloak, "a", members=("b",))
    _add_role(keycloak, "b")
    response = _admin(client).put(
        f"{_BASE}/b", json={"description": "", "members": ["a"]}, headers=_CSRF_HEADER
    )
    assert response.status_code == 422
    assert "loop" in response.json()["detail"]
    assert _audit(config_dir) == []


def test_changing_a_role_that_does_not_exist_is_a_404(client: TestClient) -> None:
    response = _admin(client).put(
        f"{_BASE}/ghost", json={"description": "", "members": []}, headers=_CSRF_HEADER
    )
    assert response.status_code == 404


def test_changing_a_role_without_manage_realm_says_so(
    client: TestClient, keycloak: FakeKeycloak
) -> None:
    _add_role(keycloak, "sales")
    keycloak.manage_realm = False
    response = _admin(client).put(
        f"{_BASE}/sales", json={"description": "x", "members": []}, headers=_CSRF_HEADER
    )
    assert response.status_code == 403
    assert "manage-realm" in response.json()["detail"]


# -- deleting ------------------------------------------------------------------------


def test_a_role_is_deleted_and_audited_with_how_many_accounts_held_it(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path
) -> None:
    _add_role(keycloak, "sales")
    keycloak.add_user("anna", roles=("sales",))
    response = _admin(client).delete(f"{_BASE}/sales", headers=_CSRF_HEADER)
    assert response.status_code == 200
    assert response.json() == {"name": "sales", "accounts": 1, "more": False}
    assert "sales" not in keycloak.roles
    (entry,) = _audit(config_dir)
    assert (entry["action"], entry["target"]) == ("role.delete", "sales")
    assert entry["params"] == {"accounts": 1, "more": False}


@pytest.mark.parametrize("name", ["papaia-admin", "manager-admin", "user", "admin"])
def test_a_built_in_role_is_not_deleted(
    client: TestClient, keycloak: FakeKeycloak, config_dir: Path, name: str
) -> None:
    # `admin` is not in the realm here, but it is the role the manager is configured with.
    keycloak.roles.setdefault(
        "admin",
        {
            "id": "id-admin",
            "name": "admin",
            "description": "",
            "composite": False,
            "clientRole": False,
        },
    )
    response = _admin(client).delete(f"{_BASE}/{name}", headers=_CSRF_HEADER)
    assert response.status_code == 422
    assert name in keycloak.roles
    assert _audit(config_dir) == []


def test_deleting_a_role_that_does_not_exist_is_a_404(client: TestClient) -> None:
    assert _admin(client).delete(f"{_BASE}/ghost", headers=_CSRF_HEADER).status_code == 404


def test_deleting_without_the_csrf_token_changes_nothing(
    client: TestClient, keycloak: FakeKeycloak
) -> None:
    _add_role(keycloak, "sales")
    assert _admin(client).delete(f"{_BASE}/sales").status_code == 403
    assert "sales" in keycloak.roles


# -- the audit trail covers every change ---------------------------------------------

_AUDITED = {
    ("POST", "/api/v1/roles"): "role.create",
    ("PUT", "/api/v1/roles/{name}"): "role.update",
    ("DELETE", "/api/v1/roles/{name}"): "role.delete",
}


def test_every_route_that_changes_something_is_one_this_module_audits(client: TestClient) -> None:
    """A new mutating route has to be added to `_AUDITED` -- and so to a test above."""
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


def test_the_page_has_its_list_its_tabs_and_its_dialogs(client: TestClient) -> None:
    response = _admin(client).get("/users/roles")
    assert response.status_code == 200
    text = response.text
    assert 'id="role-list"' in text and "New role" in text
    assert 'href="/users"' in text and 'href="/users/roles"' in text
    for dialog in ("role-modal", "role-delete-modal"):
        assert f'id="{dialog}"' in text


def test_the_users_page_links_to_the_roles_tab(client: TestClient) -> None:
    text = _admin(client).get("/users").text
    assert 'href="/users/roles"' in text


def test_the_list_partial_tells_built_in_roles_from_custom_ones(
    client: TestClient, keycloak: FakeKeycloak
) -> None:
    _add_role(keycloak, "sales", description="Sales team", members=("user",))
    response = _admin(client).get("/partials/roles")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    text = response.text
    assert "sales" in text and "Sales team" in text
    assert "Built in" in text and "Custom" in text
    assert "dashboard" in text and "identity admin" in text
    assert "View" in text and "Edit" in text and "Delete…" in text
    assert "<dialog" not in text
    assert "Keycloak admin · 3" in text, "the roles of a client inside papaia-admin are counted"


def test_a_built_in_role_has_no_delete_in_the_list(
    client: TestClient, keycloak: FakeKeycloak
) -> None:
    text = _admin(client).get("/partials/roles").text
    assert "Delete…" not in text


def test_the_partial_explains_a_missing_right(client: TestClient, keycloak: FakeKeycloak) -> None:
    keycloak.allowed = False
    response = _admin(client).get("/partials/roles")
    assert response.status_code == 200
    assert "cannot read the roles yet" in response.text
    assert "manage-realm" in response.text


def test_the_partial_explains_an_unreachable_keycloak(
    client: TestClient, keycloak: FakeKeycloak
) -> None:
    keycloak.down = True
    response = _admin(client).get("/partials/roles")
    assert response.status_code == 200
    assert "Keycloak does not answer" in response.text
