"""The Connections page and API, and the Collections page working on a chosen connection.

The rules themselves are pinned in `test_vectordb_service.py`; this is the HTTP surface
around them: who may call what, what the status codes are, that no key ever leaves the
manager (not in a response, not in the audit log, not in a log line), and that the
Collections page really follows the connection it is told to.
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
import yaml

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-connections-api-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-connections-api-workspace-")

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
from app.core.vectordb import crypto  # noqa: E402
from app.core.vectordb.ingest_file import CONNECTIONS_RELPATH  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers.rag_deps import get_http_transport  # noqa: E402
from tests.fake_qdrant import API_KEY, FakeQdrant, Fleet  # noqa: E402

_CSRF = "test-csrf-token-value"
_CSRF_HEADER = {"X-CSRF-Token": _CSRF}
_SECRET = "connections-secret"
_REACH = "http://qdrant.test:6333"
_BASE = "/api/v1/rag/connections"


def _module_env(config_dir: Path, text: str) -> None:
    directory = config_dir / "ai" / "rag"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".env").write_text(text, encoding="utf-8")


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    (directory / "manager").mkdir(parents=True)
    (directory / CONNECTIONS_RELPATH.parent).mkdir(parents=True)
    (directory / ".env").write_text("COMPOSE_PROFILES=keycloak,rag\n", encoding="utf-8")
    _module_env(directory, f"QDRANT_JWT_SECRET={API_KEY}\nQI_CONNECTIONS_SECRET={_SECRET}\n")
    return directory


@pytest.fixture
def fleet() -> Fleet:
    fleet = Fleet()
    fleet.add("qdrant.test", FakeQdrant(API_KEY))
    return fleet


@pytest.fixture
def client(config_dir: Path, fleet: Fleet) -> Iterator[TestClient]:
    get_settings.cache_clear()
    settings = get_settings().model_copy(
        update={"papaia_config_dir": str(config_dir), "qdrant_url": _REACH}
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_http_transport] = lambda: fleet.transport()
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


def _stored(config_dir: Path) -> dict[str, Any]:
    document = yaml.safe_load((config_dir / CONNECTIONS_RELPATH).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _entry(config_dir: Path, name: str) -> dict[str, Any]:
    return next(e for e in _stored(config_dir)["connections"] if e["name"] == name)


def _audit(config_dir: Path) -> list[dict[str, Any]]:
    path = audit_path(str(config_dir))
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _create(client: TestClient, name: str = "archive", **changes: Any) -> Any:
    body: dict[str, Any] = {"name": name, "fields": {"url": "http://archive.test:6333"}}
    body.update(changes)
    return _admin(client).post(_BASE, headers=_CSRF_HEADER, json=body)


def _etag(client: TestClient, name: str) -> str:
    listing = _admin(client).get(_BASE).json()
    return next(c["etag"] for c in listing["connections"] if c["name"] == name)


# ---------------------------------------------------------------------------
# Who may call what
# ---------------------------------------------------------------------------

_WRITES = [
    ("post", _BASE, {"name": "a", "fields": {"url": "http://a"}}),
    ("post", f"{_BASE}/test", {"name": "default"}),
    ("put", f"{_BASE}/default", {"fields": {"url": "http://a"}, "etag": "x"}),
    ("delete", f"{_BASE}/archive?etag=x", None),
    ("post", f"{_BASE}/default/reset", None),
]


def _call(client: TestClient, method: str, url: str, body: Any, headers: dict[str, str]) -> Any:
    return client.request(method, url, headers=headers, json=body)


@pytest.mark.parametrize(("method", "url", "body"), [("get", _BASE, None), *_WRITES])
def test_an_anonymous_caller_gets_a_401(
    client: TestClient, method: str, url: str, body: Any
) -> None:
    client.cookies.clear()

    assert _call(client, method, url, body, _CSRF_HEADER).status_code == 401


@pytest.mark.parametrize(("method", "url", "body"), [("get", _BASE, None), *_WRITES])
def test_a_user_without_the_admin_role_gets_a_403(
    client: TestClient, method: str, url: str, body: Any
) -> None:
    response = _call(_as(client, "user"), method, url, body, _CSRF_HEADER)

    assert response.status_code == 403


@pytest.mark.parametrize(("method", "url", "body"), [("get", _BASE, None), *_WRITES])
def test_an_administrator_gets_a_404_without_the_rag_profile(
    client: TestClient, config_dir: Path, method: str, url: str, body: Any
) -> None:
    (config_dir / ".env").write_text("COMPOSE_PROFILES=keycloak\n", encoding="utf-8")

    response = _call(_admin(client), method, url, body, _CSRF_HEADER)

    assert response.status_code == 404
    assert not (config_dir / CONNECTIONS_RELPATH).exists()


@pytest.mark.parametrize(("method", "url", "body"), _WRITES)
def test_a_change_without_the_csrf_token_is_refused_and_writes_nothing(
    client: TestClient, config_dir: Path, method: str, url: str, body: Any
) -> None:
    before = (config_dir / CONNECTIONS_RELPATH).exists()

    response = _call(_admin(client), method, url, body, {})

    assert response.status_code == 403
    assert (config_dir / CONNECTIONS_RELPATH).exists() == before
    assert _audit(config_dir) == []


def test_the_pages_are_for_administrators_on_a_rag_deployment(
    client: TestClient, config_dir: Path
) -> None:
    for path in ("/connections", "/partials/connections"):
        assert _admin(client).get(path).status_code == 200, path
        assert _as(client, "user").get(path).status_code == 403, path
        client.cookies.clear()
        assert client.get(path).status_code in (307, 401), path

    (config_dir / ".env").write_text("COMPOSE_PROFILES=keycloak\n", encoding="utf-8")
    assert _admin(client).get("/connections").status_code == 404
    assert _admin(client).get("/partials/connections").status_code == 404


# ---------------------------------------------------------------------------
# Listing and creating
# ---------------------------------------------------------------------------


def test_the_first_listing_has_the_default_connection(
    client: TestClient, config_dir: Path
) -> None:
    listing = _admin(client).get(_BASE).json()

    assert [c["name"] for c in listing["connections"]] == ["default"]
    default = listing["connections"][0]
    assert default["is_default"] and default["has_key"] and default["key_state"] == "ok"
    assert default["address"] == "http://qdrant:6333"
    assert default["integrated"] and not default["key_drift"]
    assert listing["secret_configured"] and listing["writable"]
    assert _entry(config_dir, "default")["url"] == "http://qdrant:6333"


def test_the_listing_describes_the_types_by_their_fields(client: TestClient) -> None:
    types = _admin(client).get(_BASE).json()["types"]

    assert [t["id"] for t in types] == ["qdrant"]
    assert [(f["name"], f["kind"], f["required"]) for f in types[0]["fields"]] == [
        ("url", "url", True),
        ("api_key", "secret", False),
    ]


def test_a_connection_is_created_and_audited_without_its_key(
    client: TestClient, config_dir: Path
) -> None:
    response = _create(client, api_key="archive-key")

    assert response.status_code == 201
    body = response.json()
    assert body["name"] == "archive" and body["has_key"] and body["key_state"] == "ok"
    assert "archive-key" not in response.text
    token = _entry(config_dir, "archive")["api_key"]
    assert crypto.decrypt(token, _SECRET) == "archive-key"
    assert token not in response.text

    audit = _audit(config_dir)
    assert [(e["action"], e["target"], e["user"]) for e in audit] == [
        ("rag.connection.create", "archive", "tester")
    ]
    assert audit[0]["params"]["key_action"] == "set"
    raw = audit_path(str(config_dir)).read_text(encoding="utf-8")
    assert "archive-key" not in raw and token not in raw


def test_the_refusals_have_their_status_codes(client: TestClient, config_dir: Path) -> None:
    assert _create(client).status_code == 201

    assert _create(client).status_code == 409  # taken
    assert _create(client, name="Bad Name").status_code == 422
    assert _create(client, name="x", fields={"url": "ftp://x"}).status_code == 422
    assert _create(client, name="x", fields={"url": "http://u:pw@x"}).status_code == 422
    assert _create(client, name="x", type="pinecone").status_code == 422
    assert [e["target"] for e in _audit(config_dir)] == ["archive"], "only the success is audited"


def test_a_key_cannot_be_stored_without_the_secret(
    client: TestClient, config_dir: Path
) -> None:
    _module_env(config_dir, f"QDRANT_JWT_SECRET={API_KEY}\n")

    response = _create(client, api_key="archive-key")

    assert response.status_code == 409
    assert "QI_CONNECTIONS_SECRET" in response.json()["detail"]
    assert _create(client, name="open").status_code == 201


def test_a_directory_that_does_not_accept_the_write_is_a_503(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_: Any, **__: Any) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", refuse)

    response = _create(client, api_key="archive-key")

    assert response.status_code == 503
    assert "read-only" in response.json()["detail"]
    assert "archive-key" not in response.text


# ---------------------------------------------------------------------------
# Changing and deleting
# ---------------------------------------------------------------------------


def _put(client: TestClient, name: str, **changes: Any) -> Any:
    body: dict[str, Any] = {"fields": {"url": "http://archive.test:6333"}}
    body.update(changes)
    if "etag" not in body:
        body["etag"] = _etag(client, name)
    return _admin(client).put(f"{_BASE}/{name}", headers=_CSRF_HEADER, json=body)


def test_a_change_keeps_a_key_that_was_not_sent(client: TestClient, config_dir: Path) -> None:
    _create(client, api_key="archive-key")
    token = _entry(config_dir, "archive")["api_key"]

    response = _put(client, "archive", fields={"url": "http://archive.test:6333/"})

    assert response.status_code == 200
    assert _entry(config_dir, "archive")["api_key"] == token
    assert _audit(config_dir)[-1]["params"]["key_action"] == "kept"


def test_the_key_goes_with_an_address_change_only_when_a_new_one_is_given(
    client: TestClient, config_dir: Path
) -> None:
    _create(client, api_key="archive-key")

    refused = _put(client, "archive", fields={"url": "http://elsewhere.test:6333"})
    assert refused.status_code == 422
    assert "never sent to a different one" in refused.json()["detail"]
    assert _entry(config_dir, "archive")["url"] == "http://archive.test:6333"

    moved = _put(
        client, "archive", fields={"url": "http://elsewhere.test:6333"}, api_key="new-key"
    )
    assert moved.status_code == 200
    assert crypto.decrypt(_entry(config_dir, "archive")["api_key"], _SECRET) == "new-key"


def test_a_stale_change_is_a_409_and_an_unknown_one_a_404(client: TestClient) -> None:
    _create(client)
    etag = _etag(client, "archive")
    assert _put(client, "archive").status_code == 200  # same content: the etag still fits

    assert _put(client, "archive", etag="0" * 16).status_code == 409
    assert _put(client, "gone", etag=etag).status_code == 404


def test_the_audit_names_what_happened_to_the_key(client: TestClient, config_dir: Path) -> None:
    _create(client, api_key="archive-key")

    _put(client, "archive", clear_api_key=True)
    _put(client, "archive", api_key="again")

    changes = [e for e in _audit(config_dir) if e["action"].startswith("rag.connection.")
               and e["action"] != "rag.connection.seed"]
    assert [e["params"]["key_action"] for e in changes] == ["set", "removed", "replaced"]


def test_a_connection_is_deleted_and_the_default_is_not(
    client: TestClient, config_dir: Path
) -> None:
    _create(client)
    default_etag = _etag(client, "default")
    etag = _etag(client, "archive")

    refused = _admin(client).delete(f"{_BASE}/default?etag={default_etag}", headers=_CSRF_HEADER)
    deleted = _admin(client).delete(f"{_BASE}/archive?etag={etag}", headers=_CSRF_HEADER)
    again = _admin(client).delete(f"{_BASE}/archive?etag={etag}", headers=_CSRF_HEADER)

    assert refused.status_code == 409 and "cannot be deleted" in refused.json()["detail"]
    assert deleted.status_code == 204
    assert again.status_code == 404
    assert [e["name"] for e in _stored(config_dir)["connections"]] == ["default"]
    assert [e["action"] for e in _audit(config_dir)][-1] == "rag.connection.delete"


def test_a_connection_that_jobs_use_is_a_409_naming_them(
    client: TestClient, config_dir: Path
) -> None:
    _create(client)
    (config_dir / "ai/rag/catalog/jobs.yaml").write_text(
        "version: 1\njobs:\n- id: nightly\n  target:\n    connection: archive\n",
        encoding="utf-8",
    )

    response = _admin(client).delete(
        f"{_BASE}/archive?etag={_etag(client, 'archive')}", headers=_CSRF_HEADER
    )

    assert response.status_code == 409
    assert "nightly" in response.json()["detail"]
    assert _entry(config_dir, "archive")


def test_the_default_is_reset_and_nothing_else(client: TestClient, config_dir: Path) -> None:
    _admin(client).get(_BASE)
    _put(client, "default", fields={"url": "http://qdrant:6333"}, api_key="stale")
    _create(client)

    reset = _admin(client).post(f"{_BASE}/default/reset", headers=_CSRF_HEADER)
    other = _admin(client).post(f"{_BASE}/archive/reset", headers=_CSRF_HEADER)

    assert reset.status_code == 200 and not reset.json()["key_drift"]
    assert crypto.decrypt(_entry(config_dir, "default")["api_key"], _SECRET) == API_KEY
    assert other.status_code == 422
    assert _audit(config_dir)[-1]["action"] == "rag.connection.reset"


# ---------------------------------------------------------------------------
# Testing
# ---------------------------------------------------------------------------


def test_a_stored_connection_is_tested_from_its_row(
    client: TestClient, fleet: Fleet
) -> None:
    fleet.add("archive.test", FakeQdrant("archive-key")).add("books")
    _create(client, api_key="archive-key")

    response = _admin(client).post(f"{_BASE}/test", headers=_CSRF_HEADER, json={"name": "archive"})

    assert response.status_code == 200
    assert response.json() == {"ok": True, "detail": "Reachable, 1 collection", "collections": 1}


def test_a_failed_test_is_a_result_not_an_error(client: TestClient) -> None:
    _create(client)  # nothing answers there

    response = _admin(client).post(f"{_BASE}/test", headers=_CSRF_HEADER, json={"name": "archive"})

    assert response.status_code == 200
    assert response.json()["ok"] is False and "not reachable" in response.json()["detail"]


def test_a_typed_address_needs_its_own_key_to_be_tested(
    client: TestClient, fleet: Fleet
) -> None:
    other = fleet.add("other.test", FakeQdrant("typed"))
    _create(client, api_key="archive-key")

    refused = _admin(client).post(
        f"{_BASE}/test",
        headers=_CSRF_HEADER,
        json={"name": "archive", "fields": {"url": "http://other.test:6333"}},
    )
    accepted = _admin(client).post(
        f"{_BASE}/test",
        headers=_CSRF_HEADER,
        json={
            "name": "archive",
            "fields": {"url": "http://other.test:6333"},
            "api_key": "typed",
        },
    )

    assert refused.status_code == 422
    assert accepted.json()["ok"] is True
    assert all(call[0] == "GET" for call in other.calls) and len(other.calls) == 1


def test_a_new_connection_can_be_tested_before_it_is_saved(
    client: TestClient, fleet: Fleet, config_dir: Path
) -> None:
    fleet.add("new.test", FakeQdrant("typed"))

    response = _admin(client).post(
        f"{_BASE}/test",
        headers=_CSRF_HEADER,
        json={"fields": {"url": "http://new.test:6333"}, "api_key": "typed"},
    )

    assert response.json()["ok"] is True
    assert not (config_dir / CONNECTIONS_RELPATH).exists(), "nothing was stored"


# ---------------------------------------------------------------------------
# No key ever leaves
# ---------------------------------------------------------------------------


def test_no_key_and_no_token_appears_anywhere_a_caller_could_see_it(
    client: TestClient,
    config_dir: Path,
    fleet: Fleet,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fleet.add("archive.test", FakeQdrant("archive-key"))
    caplog.set_level(logging.DEBUG)
    seen: list[str] = []

    seen.append(_create(client, api_key="archive-key").text)
    token = _entry(config_dir, "archive")["api_key"]
    seen.append(_admin(client).get(_BASE).text)
    seen.append(_admin(client).get("/partials/connections").text)
    seen.append(_admin(client).get("/connections").text)
    tested = _admin(client).post(f"{_BASE}/test", headers=_CSRF_HEADER, json={"name": "archive"})
    seen.append(tested.text)
    seen.append(_put(client, "archive", api_key="second-key").text)
    seen.append(_admin(client).post(f"{_BASE}/default/reset", headers=_CSRF_HEADER).text)
    seen.append(audit_path(str(config_dir)).read_text(encoding="utf-8"))
    seen.append(caplog.text)

    for key in ("archive-key", "second-key", API_KEY, _SECRET, token):
        assert all(key not in text for text in seen), key


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def test_the_page_lists_the_connections_and_marks_the_default(client: TestClient) -> None:
    _create(client, api_key="archive-key")

    body = _admin(client).get("/partials/connections").text

    assert "archive" in body and "http://archive.test:6333" in body
    assert "Default" in body and "key set" in body
    assert "archive-key" not in body
    assert "connection-modal" in body, "the dialogs are there while the file can be changed"


def test_the_page_shows_where_the_manager_connects_when_that_is_not_the_stored_address(
    client: TestClient,
) -> None:
    body = _admin(client).get("/partials/connections").text

    assert "http://qdrant:6333" in body
    assert "The manager connects to" in body and _REACH in body


def test_the_default_has_no_delete_and_the_others_do(client: TestClient) -> None:
    _create(client)

    body = _admin(client).get("/partials/connections").text

    assert body.count('"connection-delete"') == 1
    assert "Reset to integrated Qdrant" in body


def test_the_page_says_what_is_missing_instead_of_failing(
    client: TestClient, config_dir: Path
) -> None:
    _module_env(config_dir, "")

    body = _admin(client).get("/partials/connections").text

    assert "QI_CONNECTIONS_SECRET" in body and "no api-key can be stored" in body
    assert "not stored yet" in body, "the default is created once both secrets exist"


def test_a_broken_file_is_shown_and_cannot_be_edited_from_here(
    client: TestClient, config_dir: Path
) -> None:
    (config_dir / CONNECTIONS_RELPATH).write_text("connections: [", encoding="utf-8")

    response = _admin(client).get("/partials/connections")

    assert response.status_code == 200
    assert "cannot be changed" in response.text and "invalid YAML" in response.text
    assert "connection-modal" not in response.text
    refused = _create(client)
    assert refused.status_code == 409 and "cannot be changed" in refused.json()["detail"]


def test_entries_the_ingester_would_refuse_are_listed_as_problems(
    client: TestClient, config_dir: Path
) -> None:
    (config_dir / CONNECTIONS_RELPATH).write_text(
        "version: 1\nconnections:\n- name: broken\n  url: ftp://y\n", encoding="utf-8"
    )

    body = _admin(client).get("/partials/connections").text

    assert "The ingester reports problems" in body and "broken" in body


def test_markup_in_a_stored_value_is_escaped(client: TestClient, config_dir: Path) -> None:
    (config_dir / CONNECTIONS_RELPATH).write_text(
        "version: 1\nconnections:\n- name: odd\n"
        "  url: http://x/<img src=x onerror=alert(1)>\n",
        encoding="utf-8",
    )

    body = _admin(client).get("/partials/connections").text

    assert "<img src=x" not in body
    assert "&lt;img src=x" in body or "\\u003cimg src=x" in body


def test_the_responses_are_never_cached(client: TestClient) -> None:
    assert _admin(client).get("/partials/connections").headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
# Collections on the chosen connection
# ---------------------------------------------------------------------------


@pytest.fixture
def two(client: TestClient, fleet: Fleet) -> tuple[FakeQdrant, FakeQdrant]:
    """The integrated Qdrant (ledgers) and a second one (atlas), both reachable."""
    integrated = fleet.by_host["qdrant.test"]
    archive = fleet.add("archive.test", FakeQdrant("archive-key"))
    integrated.add("ledgers")
    archive.add("atlas")
    assert _create(client, api_key="archive-key").status_code == 201
    return integrated, archive


def test_the_collections_follow_the_connection_that_is_asked_for(
    client: TestClient, two: tuple[FakeQdrant, FakeQdrant]
) -> None:
    default_page = _admin(client).get("/partials/collections").text
    archive_page = _admin(client).get("/partials/collections?connection=archive").text

    assert "ledgers" in default_page and "atlas" not in default_page
    assert 'data-connection="default"' in default_page
    assert "atlas" in archive_page and "ledgers" not in archive_page
    assert 'data-connection="archive"' in archive_page


def test_a_connection_that_does_not_exist_is_a_404_everywhere(client: TestClient) -> None:
    assert _admin(client).get("/partials/collections?connection=nope").status_code == 404
    for call in (
        lambda: _admin(client).post(
            "/api/v1/rag/collections?connection=nope",
            headers=_CSRF_HEADER,
            json={"name": "x", "vector_size": 4},
        ),
        lambda: _admin(client).put(
            "/api/v1/rag/collections/x/roles?connection=nope",
            headers=_CSRF_HEADER,
            json={"roles": []},
        ),
        lambda: _admin(client).delete(
            "/api/v1/rag/collections/x?connection=nope", headers=_CSRF_HEADER
        ),
        lambda: _admin(client).post(
            "/api/v1/rag/collections/operator-grant?connection=nope", headers=_CSRF_HEADER
        ),
    ):
        assert call().status_code == 404


def test_a_change_lands_on_the_chosen_connection_only(
    client: TestClient, config_dir: Path, two: tuple[FakeQdrant, FakeQdrant]
) -> None:
    integrated, archive = two
    before = list(integrated.calls)

    response = _admin(client).post(
        "/api/v1/rag/collections?connection=archive",
        headers=_CSRF_HEADER,
        json={"name": "reports", "vector_size": 4, "roles": [{"role": "readers"}]},
    )

    assert response.status_code == 201
    assert "reports" in archive.collections
    assert ("readers", "reports", "r") in archive.grants()
    assert "reports" not in integrated.collections
    assert integrated.calls == before, "nothing reached the other Qdrant"
    assert _audit(config_dir)[-1]["params"]["connection"] == "archive"


def test_the_default_is_used_when_no_connection_is_named(
    client: TestClient, config_dir: Path, two: tuple[FakeQdrant, FakeQdrant]
) -> None:
    integrated, archive = two

    _admin(client).post(
        "/api/v1/rag/collections",
        headers=_CSRF_HEADER,
        json={"name": "reports", "vector_size": 4},
    )

    assert "reports" in integrated.collections and "reports" not in archive.collections
    assert _audit(config_dir)[-1]["params"]["connection"] == "default"


def test_roles_are_noted_as_not_enforced_away_from_the_integrated_qdrant(
    client: TestClient, two: tuple[FakeQdrant, FakeQdrant]
) -> None:
    note = "reads roles from the integrated Qdrant only"

    assert note not in _admin(client).get("/partials/collections").text
    assert note in _admin(client).get("/partials/collections?connection=archive").text


def test_a_connection_to_the_integrated_qdrant_under_another_name_enforces_roles_too(
    client: TestClient, fleet: Fleet
) -> None:
    fleet.add("qdrant.test", fleet.by_host["qdrant.test"])
    _create(client, "alias", fields={"url": "http://qdrant:6333"}, api_key=API_KEY)

    body = _admin(client).get("/partials/collections?connection=alias").text

    assert "reads roles from the integrated Qdrant only" not in body


def test_a_qdrant_without_an_api_key_can_be_managed(
    client: TestClient, fleet: Fleet
) -> None:
    keyless = fleet.add("open.test", FakeQdrant(""))
    keyless.add("public")
    _create(client, "open", fields={"url": "http://open.test:6333"})

    body = _admin(client).get("/partials/collections?connection=open").text

    assert "public" in body and "Collections are not available" not in body


def test_a_refused_key_names_the_connection_and_never_the_key(
    client: TestClient, fleet: Fleet
) -> None:
    fleet.add("archive.test", FakeQdrant("the-real-key"))
    _create(client, api_key="wrong-key")

    body = _admin(client).get("/partials/collections?connection=archive").text

    assert "Collections are not available" in body
    assert "refused the api-key" in body and "&#39;archive&#39;" in body
    assert "QDRANT_JWT_SECRET" not in body
    assert "wrong-key" not in body and "the-real-key" not in body


def test_an_unreadable_key_is_explained_on_the_page(
    client: TestClient, config_dir: Path, two: tuple[FakeQdrant, FakeQdrant]
) -> None:
    _, archive = two
    _module_env(config_dir, f"QDRANT_JWT_SECRET={API_KEY}\nQI_CONNECTIONS_SECRET=rotated\n")
    calls = list(archive.calls)

    body = _admin(client).get("/partials/collections?connection=archive").text

    assert "cannot be decrypted" in body and "Connections page" in body
    assert archive.calls == calls, "no request was made with a key that is not there"


def test_the_collections_page_keeps_working_while_the_file_is_unusable(
    client: TestClient, config_dir: Path, two: tuple[FakeQdrant, FakeQdrant]
) -> None:
    (config_dir / CONNECTIONS_RELPATH).write_text("connections: [", encoding="utf-8")

    body = _admin(client).get("/partials/collections").text

    assert "ledgers" in body, "the default is answered from the stack's own settings"


def test_the_collections_page_offers_the_connections_that_can_hold_collections(
    client: TestClient, two: tuple[FakeQdrant, FakeQdrant]
) -> None:
    page = _admin(client).get("/collections?connection=archive").text

    assert 'value="default"' in page and "default (default)" in page.replace("&nbsp;", " ")
    assert '<option value="archive" selected>' in page
    assert "/connections" in page


def test_an_unknown_connection_in_the_address_falls_back_to_the_default(
    client: TestClient, two: tuple[FakeQdrant, FakeQdrant]
) -> None:
    page = _admin(client).get("/collections?connection=nope").text

    assert '<option value="default" selected>' in page
    assert "nope" not in page


def test_the_default_is_selectable_before_it_is_stored(
    client: TestClient, config_dir: Path
) -> None:
    _module_env(config_dir, f"QDRANT_JWT_SECRET={API_KEY}\n")  # nothing can be stored

    page = _admin(client).get("/collections").text

    assert '<option value="default" selected>' in page
