"""The Collections page: collections of the RAG system and their access roles.

The storage is the one `qdrant-mcp-rbac` defines, and nothing on that side pins it, so
the ids and payload shapes are pinned here against literals taken from its code. Qdrant
itself is a small in-memory fake behind `httpx.MockTransport`, which speaks only the
calls the page makes and answers in Qdrant's own error shape.
"""
from __future__ import annotations

import base64
import json
import os
import re
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any
from urllib.parse import unquote

import httpx
import pytest

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-collections-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-collections-workspace-")

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
from app.core import rag_collections  # noqa: E402
from app.core.audit import audit_path  # noqa: E402
from app.core.qdrant import QdrantClient, QdrantError, QdrantUnavailable  # noqa: E402
from app.core.rag import RagBackend, rag_active, rag_backend  # noqa: E402
from app.core.rag_collections import (  # noqa: E402
    CollectionExists,
    CollectionNotFound,
    CollectionStore,
    InvalidInput,
    RoleGrant,
    acl_point_id,
    meta_point_id,
)
from app.main import create_app  # noqa: E402
from app.routers.rag_deps import get_store  # noqa: E402

_CSRF = "test-csrf-token-value"
_KEY = "test-api-key"
_URL = "http://qdrant.test:6333"
_OPERATOR = "qdrant-ingest-operator"
_META = "_collection_meta"
_ACL = "_rbac_acl"

_RAG_ENV = (
    "PAPAIA_HOST=https://papaia.test\n"
    "COMPOSE_PROFILES=keycloak,librechat,rag\n"
    "QDRANT_PUBLIC_URL=https://qdrant.test\n"
    "QDRANT_INGEST_PUBLIC_URL=https://ingest.test\n"
)
_OFF_ENV = "PAPAIA_HOST=https://papaia.test\nCOMPOSE_PROFILES=keycloak,librechat\n"


# ---------------------------------------------------------------------------
# A Qdrant that speaks just enough REST
# ---------------------------------------------------------------------------


def _ok(result: Any) -> httpx.Response:
    return httpx.Response(200, json={"result": result, "status": "ok", "time": 0.0})


def _err(status: int, message: str) -> httpx.Response:
    return httpx.Response(status, json={"status": {"error": message}, "time": 0.0})


def _missing(name: str) -> httpx.Response:
    return _err(404, f"Not found: Collection `{name}` doesn't exist!")


class FakeQdrant:
    def __init__(self, api_key: str = _KEY) -> None:
        self.api_key = api_key
        self.collections: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.down = False
        # (method, path) pairs that answer 500, to make a step in the middle fail.
        self.fail: set[tuple[str, str]] = set()

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    # -- seeding and inspection ------------------------------------------------

    def add(self, name: str, size: int = 4, extra_points: int = 0) -> None:
        self.collections[name] = {
            "vectors": {"size": size, "distance": "Cosine"},
            "points": {},
            "indexes": [],
            "extra": extra_points,
        }

    def put(self, collection: str, point_id: str, payload: dict[str, Any]) -> None:
        self.collections[collection]["points"][point_id] = {"vector": [0.0], "payload": payload}

    def grant(
        self,
        role: str,
        collection: str,
        access: str,
        doc_policy: Any = None,
        pid: str | None = None,
    ) -> str:
        if _ACL not in self.collections:
            self.add(_ACL, size=1)
        point_id = pid or acl_point_id(role, collection)
        self.put(
            _ACL,
            point_id,
            {"role": role, "collection": collection, "access": access, "doc_policy": doc_policy},
        )
        return point_id

    def payloads(self, collection: str) -> dict[str, dict[str, Any]]:
        return {
            pid: point["payload"]
            for pid, point in self.collections.get(collection, {"points": {}})["points"].items()
        }

    def grants(self) -> set[tuple[str, str, str]]:
        return {
            (p["role"], p["collection"], p["access"]) for p in self.payloads(_ACL).values()
        }

    def wrote(self) -> list[tuple[str, str]]:
        """The calls that changed something; a scroll or a retrieve is a POST that reads."""
        return [
            call
            for call in self.calls
            if call[0] in ("PUT", "DELETE") or call[1].endswith("/points/delete")
        ]

    # -- the wire ---------------------------------------------------------------

    def _handle(self, request: httpx.Request) -> httpx.Response:
        if self.down:
            raise httpx.ConnectError("connection refused", request=request)
        path = request.url.path
        self.calls.append((request.method, unquote(path)))
        if request.headers.get("api-key") != self.api_key:
            return _err(403, "Invalid api-key")
        if (request.method, unquote(path)) in self.fail:
            return _err(500, "Service internal error: injected failure")

        parts = [unquote(part) for part in path.split("/")[1:]]
        body = json.loads(request.content) if request.content else {}
        method = request.method

        if parts == ["collections"] and method == "GET":
            return _ok({"collections": [{"name": name} for name in self.collections]})
        if parts[0] != "collections" or len(parts) < 2:
            return _err(404, "Not found")

        name, rest = parts[1], parts[2:]
        col = self.collections.get(name)

        if not rest:
            if method == "GET":
                if col is None:
                    return _missing(name)
                return _ok(
                    {
                        "status": "green",
                        "points_count": len(col["points"]) + col["extra"],
                        "config": {"params": {"vectors": col["vectors"]}},
                    }
                )
            if method == "PUT":
                if col is not None:
                    return _err(409, f"Wrong input: Collection `{name}` already exists!")
                self.add(name, size=body["vectors"]["size"])
                self.collections[name]["vectors"] = body["vectors"]
                return _ok(True)
            if method == "DELETE":
                if col is None:
                    return _missing(name)
                del self.collections[name]
                return _ok(True)

        if col is None:
            return _missing(name)

        if rest == ["index"] and method == "PUT":
            col["indexes"].append((body["field_name"], body["field_schema"]))
            return _ok({"status": "completed"})
        if rest == ["points"] and method == "PUT":
            for point in body["points"]:
                col["points"][point["id"]] = {
                    "vector": point["vector"],
                    "payload": point["payload"],
                }
            return _ok({"status": "completed"})
        if rest == ["points"] and method == "POST":
            return _ok(
                [
                    {"id": pid, "payload": col["points"][pid]["payload"]}
                    for pid in body["ids"]
                    if pid in col["points"]
                ]
            )
        if rest == ["points", "scroll"] and method == "POST":
            ids = list(col["points"])
            start = ids.index(body["offset"]) if body.get("offset") in ids else 0
            limit = body.get("limit", 10)
            page = ids[start : start + limit]
            following = ids[start + limit] if start + limit < len(ids) else None
            points = [{"id": pid, "payload": col["points"][pid]["payload"]} for pid in page]
            return _ok({"points": points, "next_page_offset": following})
        if rest == ["points", "delete"] and method == "POST":
            for pid in body["points"]:
                col["points"].pop(pid, None)
            return _ok({"status": "completed"})
        return _err(404, "Not found")


@pytest.fixture
def qdrant() -> FakeQdrant:
    return FakeQdrant()


def _backend(**changes: Any) -> RagBackend:
    values: dict[str, Any] = {
        "api_key": _KEY,
        "meta_collection": _META,
        "acl_collection": _ACL,
        "operator_role": _OPERATOR,
    }
    values.update(changes)
    return RagBackend(**values)


@pytest.fixture
async def store(qdrant: FakeQdrant) -> AsyncIterator[CollectionStore]:
    client = QdrantClient(_URL, _KEY, transport=qdrant.transport())
    yield CollectionStore(client, _backend())
    await client.aclose()


def _grants(*pairs: tuple[str, str]) -> list[RoleGrant]:
    return [RoleGrant(role, access) for role, access in pairs]


# ---------------------------------------------------------------------------
# The storage contract of qdrant-mcp-rbac
# ---------------------------------------------------------------------------


def test_point_ids_are_the_ones_the_mcp_server_derives() -> None:
    # Computed with `qdrant-mcp-rbac` (`src/qdrant/meta.py`, `src/auth/acl.py`). A
    # changed namespace or separator would orphan every point, silently.
    assert meta_point_id("finance") == "17ce0a0d-b6c7-5fe5-b8ea-1ef7be7ad26d"
    assert acl_point_id("finance", "finance") == "7d6f9059-7f22-5694-b129-ea0626ab2251"
    assert acl_point_id(_OPERATOR, "*") == "d954f5eb-24e8-5f0f-a4cf-9e8440ef0cda"


async def test_a_grant_has_exactly_the_payload_the_mcp_server_parses(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    await store.create("finance", 4, None, _grants(("finance", "r")))

    point = qdrant.collections[_ACL]["points"]["7d6f9059-7f22-5694-b129-ea0626ab2251"]
    assert point["payload"] == {
        "role": "finance",
        "collection": "finance",
        "access": "r",
        "doc_policy": None,  # an explicit null, as the MCP server's own dump writes it
    }
    assert point["vector"] == [0.0]


async def test_the_meta_point_has_exactly_the_payload_the_ingester_writes(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    await store.create("finance", 1024, "bge-m3", [])

    point = qdrant.collections[_META]["points"]["17ce0a0d-b6c7-5fe5-b8ea-1ef7be7ad26d"]
    assert point["payload"] == {
        "collection": "finance",
        "embedding_model": "bge-m3",
        "vector_dimension": 1024,
    }
    assert point["vector"] == [0.0]


async def test_the_system_collections_are_created_the_way_the_mcp_server_creates_them(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    await store.create("finance", 4, "bge-m3", [])

    for name in (_ACL, _META):
        assert qdrant.collections[name]["vectors"] == {"size": 1, "distance": "Cosine"}
    assert qdrant.collections[_ACL]["indexes"] == [("role", "keyword")]
    assert qdrant.collections[_META]["indexes"] == []


# ---------------------------------------------------------------------------
# Creating
# ---------------------------------------------------------------------------


async def test_a_collection_is_created_like_the_ingester_would(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    await store.create("finance", 1024, None, [])

    created = qdrant.collections["finance"]
    assert created["vectors"] == {"size": 1024, "distance": "Cosine"}
    assert [field for field, _ in created["indexes"]] == [
        "source",
        "ingest_job",
        "ingest_run",
        "acl_tags",
    ]
    assert {schema for _, schema in created["indexes"]} == {"keyword"}


async def test_without_a_model_no_meta_point_is_written(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    await store.create("finance", 4, "  ", [])

    # The ingester records model and dimension on its first run.
    assert qdrant.payloads(_META) == {}
    assert _META not in qdrant.collections


async def test_the_operator_gets_a_global_manage_grant_with_every_collection(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    await store.create("finance", 4, None, [])

    assert qdrant.grants() == {(_OPERATOR, "*", "m")}


async def test_roles_are_stored_one_point_per_role_and_collection(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    await store.create("finance", 4, None, _grants(("finance", "r"), ("auditors", "rw")))

    assert qdrant.grants() == {
        ("finance", "finance", "r"),
        ("auditors", "finance", "rw"),
        (_OPERATOR, "*", "m"),
    }


@pytest.mark.parametrize(
    "name",
    ["", "_hidden", "-lead", "has space", "slash/name", "dot\n", "x" * 256, _META, _ACL],
)
async def test_a_name_that_cannot_be_used_is_refused_before_anything_is_written(
    store: CollectionStore, qdrant: FakeQdrant, name: str
) -> None:
    with pytest.raises(InvalidInput):
        await store.create(name, 4, None, [])

    assert qdrant.calls == []


@pytest.mark.parametrize("size", [0, -1, 65_537])
async def test_a_vector_size_out_of_range_is_refused(store: CollectionStore, size: int) -> None:
    with pytest.raises(InvalidInput):
        await store.create("finance", size, None, [])


@pytest.mark.parametrize(
    "roles",
    [
        [("", "r")],
        [("   ", "r")],
        [("a|b", "r")],
        [("tab\there", "r")],
        [("finance", "m")],
        [("finance", "w")],
        [("finance", "r"), ("finance", "rw")],
        [(_OPERATOR, "r")],
    ],
)
async def test_a_role_list_that_cannot_be_stored_is_refused(
    store: CollectionStore, qdrant: FakeQdrant, roles: list[tuple[str, str]]
) -> None:
    with pytest.raises(InvalidInput):
        await store.create("finance", 4, None, _grants(*roles))

    assert qdrant.calls == []


async def test_a_name_that_is_taken_is_not_touched(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    qdrant.add("finance", size=8)

    with pytest.raises(CollectionExists):
        await store.create("finance", 4, "bge-m3", _grants(("finance", "r")))

    assert qdrant.wrote() == []
    assert qdrant.collections["finance"]["vectors"]["size"] == 8


async def test_a_failure_after_the_collection_exists_removes_it_again(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    qdrant.fail.add(("PUT", f"/collections/{_ACL}/points"))

    with pytest.raises(QdrantError):
        await store.create("finance", 4, "bge-m3", _grants(("finance", "r")))

    # A retry starts clean: no collection, no meta point, no grant left behind.
    assert "finance" not in qdrant.collections
    assert qdrant.payloads(_META) == {}
    assert qdrant.grants() == set()


# ---------------------------------------------------------------------------
# Roles of an existing collection
# ---------------------------------------------------------------------------


async def test_roles_are_added_changed_and_removed(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    qdrant.add("finance")
    qdrant.grant("finance", "finance", "r")
    qdrant.grant("legacy", "finance", "rw")

    await store.set_roles("finance", _grants(("finance", "rw"), ("auditors", "r")))

    assert qdrant.grants() == {
        ("finance", "finance", "rw"),
        ("auditors", "finance", "r"),
        (_OPERATOR, "*", "m"),
    }


async def test_an_access_change_keeps_the_document_policy(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    policy = {
        "default": "deny",
        "conditions": [{"field": "acl_tags", "mode": "allow", "values": ["x"]}],
    }
    qdrant.add("finance")
    point_id = qdrant.grant("finance", "finance", "r", doc_policy=policy)

    await store.set_roles("finance", _grants(("finance", "rw")))

    assert qdrant.payloads(_ACL)[point_id]["access"] == "rw"
    assert qdrant.payloads(_ACL)[point_id]["doc_policy"] == policy


async def test_an_unchanged_role_is_not_written_again(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    qdrant.add("finance")
    qdrant.grant("finance", "finance", "r")
    qdrant.grant(_OPERATOR, "*", "m")

    await store.set_roles("finance", _grants(("finance", "r")))

    assert qdrant.wrote() == []


async def test_only_this_collections_roles_are_touched(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    qdrant.add("finance")
    qdrant.add("hr")
    qdrant.grant("hr-team", "hr", "rw")
    qdrant.grant("ops", "*", "m")

    await store.set_roles("finance", [])

    assert qdrant.grants() == {("hr-team", "hr", "rw"), ("ops", "*", "m"), (_OPERATOR, "*", "m")}


async def test_roles_of_a_collection_that_does_not_exist_are_refused(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    with pytest.raises(CollectionNotFound):
        await store.set_roles("ghost", _grants(("finance", "r")))

    assert qdrant.wrote() == []


async def test_the_system_collections_cannot_be_given_roles(store: CollectionStore) -> None:
    for name in (_META, _ACL):
        with pytest.raises(InvalidInput):
            await store.set_roles(name, [])


async def test_the_operator_grant_is_written_once(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    assert await store.ensure_operator_grant() is True
    assert await store.ensure_operator_grant() is False

    assert qdrant.grants() == {(_OPERATOR, "*", "m")}
    assert qdrant.collections[_ACL]["indexes"] == [("role", "keyword")]


async def test_a_manage_grant_the_operator_already_holds_is_enough(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    # The MCP server treats any `m` grant as global, whatever its collection says.
    qdrant.grant(_OPERATOR, "finance", "m")

    assert await store.ensure_operator_grant() is False


# ---------------------------------------------------------------------------
# Deleting
# ---------------------------------------------------------------------------


async def test_deleting_removes_the_collection_its_meta_point_and_its_roles(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    await store.create("finance", 4, "bge-m3", _grants(("finance", "r")))
    await store.create("hr", 4, "bge-m3", _grants(("hr-team", "rw")))

    await store.delete("finance")

    assert "finance" not in qdrant.collections
    assert set(qdrant.payloads(_META)) == {meta_point_id("hr")}
    assert qdrant.grants() == {("hr-team", "hr", "rw"), (_OPERATOR, "*", "m")}


async def test_a_recreated_name_does_not_revive_old_roles(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    await store.create("finance", 4, None, _grants(("finance", "rw")))
    await store.delete("finance")

    await store.create("finance", 4, None, [])

    assert ("finance", "finance", "rw") not in qdrant.grants()


async def test_a_global_grant_naming_the_collection_is_left_alone(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    qdrant.add("finance")
    qdrant.grant("ops", "finance", "m")

    await store.delete("finance")

    assert qdrant.grants() == {("ops", "finance", "m")}


async def test_deleting_what_is_already_gone_still_cleans_up(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    await store.create("finance", 4, "bge-m3", _grants(("finance", "r")))
    del qdrant.collections["finance"]  # a delete that stopped half-way

    await store.delete("finance")

    assert qdrant.payloads(_META) == {}
    assert qdrant.grants() == {(_OPERATOR, "*", "m")}


async def test_deleting_without_a_meta_or_acl_collection_is_fine(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    qdrant.add("finance")

    await store.delete("finance")

    assert "finance" not in qdrant.collections


async def test_the_system_collections_cannot_be_deleted(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    qdrant.add(_META, size=1)

    with pytest.raises(InvalidInput):
        await store.delete(_META)

    assert _META in qdrant.collections


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


async def test_the_list_groups_roles_by_collection_and_hides_the_system_collections(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    qdrant.add("finance", size=1024, extra_points=7)
    qdrant.add("hr", size=384)
    qdrant.add(_META, size=1)
    qdrant.put(
        _META,
        meta_point_id("finance"),
        {"collection": "finance", "embedding_model": "bge-m3", "vector_dimension": 1024},
    )
    qdrant.grant("finance", "finance", "r")
    qdrant.grant("auditors", "finance", "rw")
    qdrant.grant("ops", "*", "m")
    qdrant.grant(_OPERATOR, "*", "m")

    view = await store.snapshot()

    assert view.available
    assert [c.name for c in view.collections] == ["finance", "hr"]
    finance, hr = view.collections
    assert (finance.points, finance.vector_size, finance.distance) == (7, 1024, "Cosine")
    assert finance.embedding_model == "bge-m3" and finance.has_meta
    assert [(g.role, g.access) for g in finance.grants] == [("auditors", "rw"), ("finance", "r")]
    assert hr.grants == () and not hr.has_meta and hr.embedding_model is None
    assert view.global_roles == ("ops",)
    assert view.operator_granted


async def test_the_operator_is_never_listed_as_an_ordinary_role(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    qdrant.add("finance")
    qdrant.grant(_OPERATOR, "finance", "r")  # redundant, and hidden

    view = await store.snapshot()

    assert view.collections[0].grants == ()
    assert not view.operator_granted


async def test_the_list_works_before_any_grant_was_ever_written(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    qdrant.add("finance")

    view = await store.snapshot()

    assert view.available and not view.operator_granted
    assert view.collections[0].grants == ()


async def test_grants_are_read_across_pages(
    store: CollectionStore, qdrant: FakeQdrant, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rag_collections, "SCROLL_PAGE_SIZE", 2)
    qdrant.add("finance")
    for index in range(5):
        qdrant.grant(f"role-{index}", "finance", "r")

    view = await store.snapshot()

    assert [g.role for g in view.collections[0].grants] == [f"role-{i}" for i in range(5)]


async def test_a_grant_the_mcp_server_could_not_parse_is_ignored(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    qdrant.add("finance")
    qdrant.grant("finance", "finance", "r")
    qdrant.grant("odd", "finance", "w")  # not an access level
    qdrant.put(_ACL, "x1", {"collection": "finance", "access": "r"})  # no role
    qdrant.put(_ACL, "x2", {"role": "", "collection": "finance", "access": "r"})

    view = await store.snapshot()

    assert [g.role for g in view.collections[0].grants] == ["finance"]


async def test_a_collection_that_vanishes_during_the_read_is_still_listed(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    qdrant.add("finance")
    qdrant.fail.add(("GET", "/collections/finance"))

    view = await store.snapshot()

    assert view.available
    assert view.collections[0].name == "finance"
    assert view.collections[0].points is None


async def test_qdrant_being_down_is_a_reason_not_an_error(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    qdrant.down = True

    view = await store.snapshot()

    assert not view.available
    assert "not reachable" in view.reason
    assert "qdrant.test" in view.reason


async def test_a_refused_key_is_reported_without_the_key(qdrant: FakeQdrant) -> None:
    client = QdrantClient(_URL, "wrong-key", transport=qdrant.transport())
    try:
        view = await CollectionStore(client, _backend(api_key="wrong-key")).snapshot()
    finally:
        await client.aclose()

    assert not view.available
    assert "refused the api-key" in view.reason
    assert "wrong-key" not in view.reason


async def test_a_missing_key_never_reaches_qdrant(qdrant: FakeQdrant) -> None:
    client = QdrantClient(_URL, "", transport=qdrant.transport())
    store = CollectionStore(client, _backend(api_key=""))
    try:
        view = await store.snapshot()
        with pytest.raises(QdrantUnavailable):
            await store.create("finance", 4, None, [])
    finally:
        await client.aclose()

    assert not view.available
    assert "QDRANT_JWT_SECRET" in view.reason
    assert qdrant.calls == []


async def test_an_error_from_qdrant_becomes_the_reason(
    store: CollectionStore, qdrant: FakeQdrant
) -> None:
    qdrant.fail.add(("GET", "/collections"))

    view = await store.snapshot()

    assert not view.available
    assert "injected failure" in view.reason


# ---------------------------------------------------------------------------
# Where the settings come from
# ---------------------------------------------------------------------------


def _module_env(config_dir: Path, text: str) -> None:
    path = config_dir / "ai" / "rag"
    path.mkdir(parents=True, exist_ok=True)
    (path / ".env").write_text(text, encoding="utf-8")


def test_the_services_own_defaults_apply_without_any_setting(tmp_path: Path) -> None:
    backend = rag_backend(str(tmp_path))

    assert backend.api_key == ""
    assert backend.meta_collection == "_collection_meta"
    assert backend.acl_collection == "_rbac_acl"
    assert backend.operator_role == "qdrant-ingest-operator"
    assert backend.warnings == ()
    assert backend.system_collections == {"_collection_meta", "_rbac_acl"}


def test_the_settings_are_read_from_the_rag_module_env_file(tmp_path: Path) -> None:
    _module_env(
        tmp_path,
        "QDRANT_JWT_SECRET=s3cret\n"
        "EMBEDDING_META_COLLECTION=meta_v2\n"
        "RBAC_ACL_COLLECTION=acl_v2\n"
        "QI_OIDC_OPERATOR_ROLE=rag-operators\n",
    )

    backend = rag_backend(str(tmp_path))

    assert backend.api_key == "s3cret"
    assert backend.meta_collection == "meta_v2"
    assert backend.acl_collection == "acl_v2"
    assert backend.operator_role == "rag-operators"
    assert backend.warnings == ()


def test_the_ingesters_names_are_used_when_the_mcp_servers_are_not_set(tmp_path: Path) -> None:
    _module_env(tmp_path, "QI_EMBED_META_COLLECTION=meta_v2\nQI_RBAC_ACL_COLLECTION=acl_v2\n")

    backend = rag_backend(str(tmp_path))

    assert (backend.meta_collection, backend.acl_collection) == ("meta_v2", "acl_v2")


def test_two_names_that_disagree_are_flagged_and_the_mcp_servers_wins(tmp_path: Path) -> None:
    _module_env(
        tmp_path,
        "EMBEDDING_META_COLLECTION=meta_a\nQI_EMBED_META_COLLECTION=meta_b\n"
        "RBAC_ACL_COLLECTION=acl_a\nQI_RBAC_ACL_COLLECTION=acl_a\n",
    )

    backend = rag_backend(str(tmp_path))

    assert backend.meta_collection == "meta_a"
    assert len(backend.warnings) == 1
    assert "meta_a" in backend.warnings[0] and "meta_b" in backend.warnings[0]


def test_an_empty_value_means_the_default(tmp_path: Path) -> None:
    _module_env(tmp_path, "EMBEDDING_META_COLLECTION=\nQI_OIDC_OPERATOR_ROLE=  \n")

    backend = rag_backend(str(tmp_path))

    assert backend.meta_collection == "_collection_meta"
    assert backend.operator_role == "qdrant-ingest-operator"


def test_a_configured_system_collection_name_is_the_one_that_is_hidden(
    tmp_path: Path,
) -> None:
    _module_env(tmp_path, "EMBEDDING_META_COLLECTION=meta_v2\n")

    assert "meta_v2" in rag_backend(str(tmp_path)).system_collections


def test_the_profile_alone_decides_whether_rag_is_active(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("COMPOSE_PROFILES=keycloak,rag\n", encoding="utf-8")
    assert rag_active(str(tmp_path))

    (tmp_path / ".env").write_text(
        "COMPOSE_PROFILES=keycloak\nQDRANT_PUBLIC_URL=https://q.test\n", encoding="utf-8"
    )
    assert not rag_active(str(tmp_path))
    assert not rag_active(str(tmp_path / "missing"))


# ---------------------------------------------------------------------------
# Pages and API
# ---------------------------------------------------------------------------


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    (directory / "manager").mkdir(parents=True)
    (directory / ".env").write_text(_RAG_ENV, encoding="utf-8")
    _module_env(directory, f"QDRANT_JWT_SECRET={_KEY}\n")
    return directory


@pytest.fixture
def client(config_dir: Path, qdrant: FakeQdrant) -> Iterator[TestClient]:
    get_settings.cache_clear()
    settings = get_settings().model_copy(update={"papaia_config_dir": str(config_dir)})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings

    async def fake_store() -> AsyncIterator[CollectionStore]:
        # The real settings reader, so the file on disk is what is exercised.
        backend = rag_backend(str(config_dir))
        http = QdrantClient(_URL, backend.api_key, transport=qdrant.transport())
        try:
            yield CollectionStore(http, backend)
        finally:
            await http.aclose()

    app.dependency_overrides[get_store] = fake_store
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


_CSRF_HEADER = {"X-CSRF-Token": _CSRF}


def _audit(config_dir: Path) -> list[dict[str, Any]]:
    path = audit_path(str(config_dir))
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


_PAGES = ["/collections", "/partials/collections"]
_WRITES = [
    ("POST", "/api/v1/rag/collections", {"name": "x", "vector_size": 4}),
    ("PUT", "/api/v1/rag/collections/x/roles", {"roles": []}),
    ("DELETE", "/api/v1/rag/collections/x", None),
    ("POST", "/api/v1/rag/collections/operator-grant", None),
]


@pytest.mark.parametrize("path", _PAGES)
def test_the_pages_are_for_administrators(client: TestClient, path: str) -> None:
    assert _as(client, "user").get(path).status_code == 403
    client.cookies.clear()
    assert client.get(path).status_code in (307, 401)
    assert _admin(client).get(path).status_code == 200


@pytest.mark.parametrize(("method", "path", "body"), _WRITES)
def test_the_api_is_for_administrators_and_needs_the_csrf_token(
    client: TestClient, method: str, path: str, body: Any
) -> None:
    as_user = _as(client, "user").request(method, path, json=body, headers=_CSRF_HEADER)
    assert as_user.status_code == 403
    assert _admin(client).request(method, path, json=body).status_code == 403
    client.cookies.clear()
    assert client.request(method, path, json=body, headers=_CSRF_HEADER).status_code == 401


@pytest.mark.parametrize("path", [*_PAGES, "/api/v1/rag/collections/operator-grant"])
def test_without_the_profile_there_is_no_such_page(
    client: TestClient, config_dir: Path, qdrant: FakeQdrant, path: str
) -> None:
    (config_dir / ".env").write_text(_OFF_ENV, encoding="utf-8")
    admin = _admin(client)

    response = admin.post(path, headers=_CSRF_HEADER) if "api" in path else admin.get(path)

    assert response.status_code == 404
    assert qdrant.calls == []


def test_a_signed_out_browser_is_sent_to_the_login_even_without_the_profile(
    client: TestClient, config_dir: Path
) -> None:
    (config_dir / ".env").write_text(_OFF_ENV, encoding="utf-8")

    response = client.get("/collections")

    assert response.status_code == 307
    assert response.headers["location"].startswith("/auth/login")


def test_the_list_shows_collections_with_their_roles(
    client: TestClient, qdrant: FakeQdrant
) -> None:
    qdrant.add("finance", size=1024, extra_points=1500)
    qdrant.grant("auditors", "finance", "rw")
    qdrant.grant(_OPERATOR, "*", "m")

    body = _admin(client).get("/partials/collections").text

    assert "finance" in body
    assert "1,500 points" in body
    assert "1024 · Cosine" in body
    assert "auditors" in body and "read + write" in body
    assert "no embedding model" in body
    assert _OPERATOR in body
    assert "Create grant" not in body
    assert "create-collection-modal" in body


def test_a_missing_operator_grant_offers_to_create_it(
    client: TestClient, qdrant: FakeQdrant
) -> None:
    qdrant.add("finance")

    body = _admin(client).get("/partials/collections").text

    assert "Create grant" in body


def test_the_list_says_why_when_qdrant_cannot_be_used(
    client: TestClient, qdrant: FakeQdrant
) -> None:
    qdrant.down = True

    response = _admin(client).get("/partials/collections")

    assert response.status_code == 200
    assert "Collections are not available" in response.text
    assert "not reachable" in response.text
    assert "create-collection-modal" not in response.text
    assert response.headers["cache-control"] == "no-store"


def test_a_missing_api_key_is_explained_on_the_page(
    client: TestClient, config_dir: Path, qdrant: FakeQdrant
) -> None:
    (config_dir / "ai" / "rag" / ".env").unlink()

    body = _admin(client).get("/partials/collections").text

    assert "QDRANT_JWT_SECRET" in body
    assert qdrant.calls == []


def test_a_name_with_markup_is_escaped(client: TestClient, qdrant: FakeQdrant) -> None:
    # The ingester may have created a name this page would not accept.
    qdrant.add("<img src=x onerror=alert(1)>")
    qdrant.grant("<b>role</b>", "<img src=x onerror=alert(1)>", "r")

    body = _admin(client).get("/partials/collections").text

    assert "<img src=x" not in body
    assert "<b>role</b>" not in body
    assert "&lt;img src=x" in body


def test_a_collection_is_created_and_audited(
    client: TestClient, config_dir: Path, qdrant: FakeQdrant
) -> None:
    response = _admin(client).post(
        "/api/v1/rag/collections",
        headers=_CSRF_HEADER,
        json={
            "name": "finance",
            "vector_size": 1024,
            "embedding_model": "bge-m3",
            "roles": [{"role": "finance", "access": "rw"}, {"role": " auditors "}],
        },
    )

    assert response.status_code == 201, response.text
    assert qdrant.grants() == {
        ("finance", "finance", "rw"),
        ("auditors", "finance", "r"),
        (_OPERATOR, "*", "m"),
    }
    entry = _audit(config_dir)[-1]
    assert (entry["action"], entry["target"], entry["user"]) == (
        "rag.collection.create",
        "finance",
        "tester",
    )
    assert entry["params"]["vector_size"] == 1024
    assert entry["params"]["roles"] == [
        {"role": "finance", "access": "rw"},
        {"role": "auditors", "access": "r"},
    ]
    assert _KEY not in json.dumps(entry)


@pytest.mark.parametrize(
    "body",
    [
        {"name": "_hidden", "vector_size": 4},
        {"name": "finance", "vector_size": 0},
        {"name": "finance", "vector_size": 4, "roles": [{"role": "a|b"}]},
        {"name": "finance", "vector_size": 4, "roles": [{"role": _OPERATOR}]},
        {"name": "finance", "vector_size": 4, "roles": [{"role": "x", "access": "m"}]},
        {"name": "finance"},
    ],
)
def test_an_invalid_collection_is_a_422_and_writes_nothing(
    client: TestClient, config_dir: Path, qdrant: FakeQdrant, body: dict[str, Any]
) -> None:
    response = _admin(client).post("/api/v1/rag/collections", headers=_CSRF_HEADER, json=body)

    assert response.status_code == 422
    assert qdrant.wrote() == []
    assert _audit(config_dir) == []


def test_a_taken_name_is_a_409(client: TestClient, qdrant: FakeQdrant) -> None:
    qdrant.add("finance")

    response = _admin(client).post(
        "/api/v1/rag/collections", headers=_CSRF_HEADER, json={"name": "finance", "vector_size": 4}
    )

    assert response.status_code == 409
    assert "already exists" in response.json()["detail"]


def test_qdrant_being_down_is_a_503_and_an_error_from_it_a_502(
    client: TestClient, qdrant: FakeQdrant
) -> None:
    body = {"name": "finance", "vector_size": 4}
    qdrant.down = True
    down = _admin(client).post("/api/v1/rag/collections", headers=_CSRF_HEADER, json=body)
    qdrant.down = False
    qdrant.fail.add(("PUT", "/collections/finance"))
    broken = _admin(client).post("/api/v1/rag/collections", headers=_CSRF_HEADER, json=body)

    assert down.status_code == 503
    assert broken.status_code == 502
    assert "injected failure" in broken.json()["detail"]


def test_roles_are_replaced_and_audited(
    client: TestClient, config_dir: Path, qdrant: FakeQdrant
) -> None:
    qdrant.add("finance")
    qdrant.grant("old", "finance", "r")

    response = _admin(client).put(
        "/api/v1/rag/collections/finance/roles",
        headers=_CSRF_HEADER,
        json={"roles": [{"role": "new", "access": "rw"}]},
    )

    assert response.status_code == 200, response.text
    assert qdrant.grants() == {("new", "finance", "rw"), (_OPERATOR, "*", "m")}
    entry = _audit(config_dir)[-1]
    assert (entry["action"], entry["target"]) == ("rag.collection.roles.update", "finance")
    assert entry["params"] == {"roles": [{"role": "new", "access": "rw"}]}


def test_roles_of_an_unknown_collection_are_a_404(client: TestClient, qdrant: FakeQdrant) -> None:
    response = _admin(client).put(
        "/api/v1/rag/collections/ghost/roles", headers=_CSRF_HEADER, json={"roles": []}
    )

    assert response.status_code == 404


def test_the_operator_cannot_be_put_into_a_role_list(
    client: TestClient, qdrant: FakeQdrant
) -> None:
    qdrant.add("finance")

    response = _admin(client).put(
        "/api/v1/rag/collections/finance/roles",
        headers=_CSRF_HEADER,
        json={"roles": [{"role": _OPERATOR}]},
    )

    assert response.status_code == 422
    assert "always has access" in response.json()["detail"]


def test_a_collection_with_awkward_characters_in_its_name_can_be_managed(
    client: TestClient, qdrant: FakeQdrant
) -> None:
    qdrant.add("a b")

    roles = _admin(client).put(
        "/api/v1/rag/collections/a%20b/roles",
        headers=_CSRF_HEADER,
        json={"roles": [{"role": "x"}]},
    )
    deleted = _admin(client).delete("/api/v1/rag/collections/a%20b", headers=_CSRF_HEADER)

    assert roles.status_code == 200
    assert deleted.status_code == 204
    assert "a b" not in qdrant.collections


def test_a_collection_is_deleted_with_its_leftovers_and_audited(
    client: TestClient, config_dir: Path, qdrant: FakeQdrant
) -> None:
    admin = _admin(client)
    admin.post(
        "/api/v1/rag/collections",
        headers=_CSRF_HEADER,
        json={
            "name": "finance",
            "vector_size": 4,
            "embedding_model": "m",
            "roles": [{"role": "f"}],
        },
    )

    response = admin.delete("/api/v1/rag/collections/finance", headers=_CSRF_HEADER)

    assert response.status_code == 204
    assert "finance" not in qdrant.collections
    assert qdrant.payloads(_META) == {}
    assert qdrant.grants() == {(_OPERATOR, "*", "m")}
    assert _audit(config_dir)[-1]["action"] == "rag.collection.delete"


def test_the_operator_grant_is_created_on_request_and_audited_once(
    client: TestClient, config_dir: Path, qdrant: FakeQdrant
) -> None:
    first = _admin(client).post("/api/v1/rag/collections/operator-grant", headers=_CSRF_HEADER)
    second = _admin(client).post("/api/v1/rag/collections/operator-grant", headers=_CSRF_HEADER)

    assert first.json() == {"written": True}
    assert second.json() == {"written": False}
    assert [e["action"] for e in _audit(config_dir)] == ["rag.collection.operator-grant"]


def test_the_page_follows_a_changed_operator_role_without_a_restart(
    client: TestClient, config_dir: Path, qdrant: FakeQdrant
) -> None:
    qdrant.add("finance")
    _module_env(config_dir, f"QDRANT_JWT_SECRET={_KEY}\nQI_OIDC_OPERATOR_ROLE=rag-operators\n")

    body = _admin(client).get("/partials/collections").text

    assert "rag-operators" in body
    assert re.search(r"rag-operators</span>\s*· manage", body)
