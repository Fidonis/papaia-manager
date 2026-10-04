"""Qdrant collections of the RAG system, and the Keycloak roles that may use them.

The storage is the one `qdrant-mcp-rbac` defines, so the MCP server enforces what the
manager writes without any change on its side. Two system collections are involved,
named by `RagBackend` (the defaults are the services' own):

* the ACL collection (`_rbac_acl`): one point per (role, collection). The id is
  `uuid5(ACL_NAMESPACE, "<role>|<collection>")`, the vector `[0.0]` and the payload
  `{role, collection, access, doc_policy}`, with `access` one of `r`, `rw` and `m`.
  `m` is global manage: the MCP server returns a manage token for any role that holds
  one, whatever the `collection` field says, and `*` is only the convention. The
  collection carries a keyword index on `role`.
* the meta collection (`_collection_meta`): one point per data collection that the
  ingester and the MCP server read to embed a text query with the right model. The id
  is `uuid5(META_NAMESPACE, "<collection>")`, the vector `[0.0]` and the payload
  `{collection, embedding_model, vector_dimension}`. It holds no roles.

Both namespaces and both payload shapes are pinned by tests against literals taken
from `qdrant-mcp-rbac`, because nothing on that side pins them: a changed namespace
would silently orphan every point.

Things worth knowing when touching this:

* Nothing cascades in Qdrant. Deleting a collection leaves its meta point and its
  grants behind, and the ids are deterministic, so re-creating the name would revive
  the old grants. `delete` removes all three.
* A grant the MCP server cannot parse is skipped without a word, so a payload that
  does not match exactly is a grant that silently does not exist.
* `qdrant-ingest` writes the meta point of a collection at the end of its first run
  and refuses a run whose model differs from a recorded one. A collection created here
  without a model therefore stays open to any model until the ingester has run.
* The MCP server caches the ACL for `RBAC_ACL_COLLECTION`'s TTL (60 s by default), so
  a change reaches it within about a minute.
* The role of the ingest operator always has access to everything: it is not listed
  per collection and cannot be removed, and a global `m` grant is kept for it.
"""
from __future__ import annotations

import asyncio
import re
import unicodedata
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any

from app.core.qdrant import QdrantClient, QdrantError, QdrantUnavailable, collection_path
from app.core.rag import RagBackend

META_NAMESPACE = uuid.UUID("9e3a5c2f-8b7d-4f1e-a6b3-2d8c9e4f1a02")
ACL_NAMESPACE = uuid.UUID("8c9f3b0e-4a5d-4d0a-9f1e-7d6c5b2a1f00")

# The collection field of a global grant. Only a convention on the MCP server's side.
GLOBAL_COLLECTION = "*"
GLOBAL_ACCESS = "m"
# What can be set per collection. `m` is global, so it is never offered here.
ACCESS_LEVELS = ("r", "rw")

# The vector of a meta or ACL point is a placeholder that is never searched.
_PLACEHOLDER_VECTOR = [0.0]

# The payload indexes `qdrant-ingest` creates on every collection it writes
# (`store/indexes.py`): `list_documents` of the MCP server needs `source`.
PAYLOAD_INDEX_FIELDS = ("source", "ingest_job", "ingest_run", "acl_tags")

SCROLL_PAGE_SIZE = 256
MAX_VECTOR_SIZE = 65_536
MAX_NAME_LENGTH = 255
DETAIL_CONCURRENCY = 8

_NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")


def meta_point_id(collection: str) -> str:
    return str(uuid.uuid5(META_NAMESPACE, collection))


def acl_point_id(role: str, collection: str) -> str:
    return str(uuid.uuid5(ACL_NAMESPACE, f"{role}|{collection}"))


class InvalidInput(ValueError):  # noqa: N818 - reads as the answer it becomes: a 422
    """The request names something that cannot be stored."""


class CollectionExists(Exception):  # noqa: N818
    """A collection of that name already exists."""


class CollectionNotFound(Exception):  # noqa: N818
    """No collection of that name exists."""


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _has_control_character(value: str) -> bool:
    return any(unicodedata.category(char).startswith("C") for char in value)


def validate_collection_name(name: str, system: frozenset[str] = frozenset()) -> str:
    """A name for a new collection.

    No leading underscore (the services reserve it for their own collections), no
    path characters, and never one of the configured system collections.
    """
    if not _NAME_RE.fullmatch(name):
        raise InvalidInput(
            "A collection name starts with a letter or digit and contains only letters, "
            f"digits, '.', '_' and '-' (at most {MAX_NAME_LENGTH} characters)."
        )
    if name in system:
        raise InvalidInput(f"{name!r} is a system collection.")
    return name


def _existing_name(name: str, system: frozenset[str]) -> str:
    """A name that refers to a collection that is already there.

    Looser than `validate_collection_name`: the ingester and other tools create names
    this page would not, and those still have to be manageable.
    """
    if not name or len(name) > MAX_NAME_LENGTH or _has_control_character(name):
        raise InvalidInput("Not a valid collection name.")
    if name in system:
        raise InvalidInput(f"{name!r} is a system collection.")
    return name


def validate_vector_size(size: int) -> int:
    if not 1 <= size <= MAX_VECTOR_SIZE:
        raise InvalidInput(f"The vector size must be between 1 and {MAX_VECTOR_SIZE}.")
    return size


def validate_embedding_model(model: str | None) -> str | None:
    """The model name, or None for "not known yet" (the ingester records it)."""
    value = (model or "").strip()
    if not value:
        return None
    if len(value) > MAX_NAME_LENGTH or _has_control_character(value):
        raise InvalidInput("Not a valid embedding model name.")
    return value


def validate_role(role: str) -> str:
    """A role name as the MCP server will match it: exactly, and case-sensitively.

    Not checked against Keycloak. `|` is refused because it separates role and
    collection in the point id, so two different pairs could share one.
    """
    value = role.strip()
    if not value:
        raise InvalidInput("A role name must not be empty.")
    if len(value) > MAX_NAME_LENGTH:
        raise InvalidInput(f"A role name is at most {MAX_NAME_LENGTH} characters.")
    if "|" in value or _has_control_character(value):
        raise InvalidInput(f"{value!r} is not a valid role name.")
    return value


@dataclass(frozen=True)
class RoleGrant:
    role: str
    access: str  # "r" or "rw"


def normalise_grants(grants: Iterable[RoleGrant], operator_role: str) -> tuple[RoleGrant, ...]:
    """The grants of one collection, validated, without duplicates, in role order."""
    seen: set[str] = set()
    result: list[RoleGrant] = []
    for grant in grants:
        role = validate_role(grant.role)
        if grant.access not in ACCESS_LEVELS:
            raise InvalidInput(f"The access level must be one of {', '.join(ACCESS_LEVELS)}.")
        if role == operator_role:
            raise InvalidInput(
                f"{role!r} always has access to every collection and is not listed per collection."
            )
        if role in seen:
            raise InvalidInput(f"The role {role!r} is listed twice.")
        seen.add(role)
        result.append(RoleGrant(role, grant.access))
    return tuple(sorted(result, key=lambda grant: grant.role))


# ---------------------------------------------------------------------------
# What the page shows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CollectionInfo:
    name: str
    points: int | None
    vector_size: int | None
    distance: str | None
    status: str | None
    embedding_model: str | None
    has_meta: bool
    grants: tuple[RoleGrant, ...]

    @property
    def roles_payload(self) -> list[dict[str, str]]:
        """The grants as the roles dialog edits them (a template cannot build this)."""
        return [{"role": grant.role, "access": grant.access} for grant in self.grants]


@dataclass(frozen=True)
class CollectionsView:
    """Everything the Collections page renders, or why there is nothing to render."""

    available: bool
    reason: str = ""
    collections: tuple[CollectionInfo, ...] = ()
    # Roles other than the operator that hold a global `m` grant.
    global_roles: tuple[str, ...] = ()
    operator_role: str = ""
    operator_granted: bool = False
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _AclPoint:
    id: str
    role: str
    collection: str
    access: str
    doc_policy: Any


@dataclass(frozen=True)
class _Details:
    points: int | None
    vector_size: int | None
    distance: str | None
    status: str | None


_NO_DETAILS = _Details(None, None, None, None)


def _parse_details(result: Any) -> _Details:
    if not isinstance(result, dict):
        return _NO_DETAILS
    vectors = result.get("config", {}).get("params", {}).get("vectors")
    # Named vectors are a dict of dicts; the ingester and the MCP server only create
    # the unnamed default one, which is the shape worth showing.
    unnamed = isinstance(vectors, dict) and "size" in vectors
    points = result.get("points_count")
    return _Details(
        points=points if isinstance(points, int) else None,
        vector_size=vectors["size"] if unnamed and isinstance(vectors["size"], int) else None,
        distance=str(vectors.get("distance")) if unnamed and vectors.get("distance") else None,
        status=str(result["status"]) if result.get("status") else None,
    )


def _parse_acl(points: Iterable[Any]) -> list[_AclPoint]:
    """The valid grants among raw points; the rest is skipped, as the MCP server does."""
    parsed: list[_AclPoint] = []
    for point in points:
        payload = point.get("payload") if isinstance(point, dict) else None
        if not isinstance(payload, dict):
            continue
        role = payload.get("role")
        collection = payload.get("collection")
        access = payload.get("access")
        if not (isinstance(role, str) and role and isinstance(collection, str) and collection):
            continue
        if access not in (*ACCESS_LEVELS, GLOBAL_ACCESS):
            continue
        parsed.append(
            _AclPoint(str(point.get("id")), role, collection, access, payload.get("doc_policy"))
        )
    return parsed


class CollectionStore:
    """The Collections page's reads and writes against one Qdrant."""

    def __init__(self, client: QdrantClient, backend: RagBackend) -> None:
        self._client = client
        self._backend = backend

    # ── reading ─────────────────────────────────────────────────────────────

    async def snapshot(self) -> CollectionsView:
        """The list, or the reason there is none. Never raises for a Qdrant problem."""
        backend = self._backend
        unavailable = CollectionsView(
            available=False,
            operator_role=backend.operator_role,
            warnings=backend.warnings,
        )
        if not backend.api_key:
            return _with_reason(unavailable, _NO_KEY)
        try:
            return await self._snapshot()
        except QdrantUnavailable as exc:
            return _with_reason(unavailable, exc.detail)
        except QdrantError as exc:
            return _with_reason(unavailable, f"Qdrant answered with an error: {exc.detail}")

    async def _snapshot(self) -> CollectionsView:
        backend = self._backend
        result = await self._client.request("GET", "/collections")
        listed = result.get("collections", []) if isinstance(result, dict) else []
        names = sorted(
            (
                str(item["name"])
                for item in listed
                if isinstance(item, dict)
                and item.get("name")
                and item["name"] not in backend.system_collections
            ),
            key=str.casefold,
        )

        acl, meta, details = await asyncio.gather(
            self._read_acl(), self._read_meta(names), self._read_details(names)
        )

        grants: dict[str, list[RoleGrant]] = {}
        global_roles: set[str] = set()
        operator_granted = False
        for point in acl:
            if point.access == GLOBAL_ACCESS:
                if point.role == backend.operator_role:
                    operator_granted = True
                else:
                    global_roles.add(point.role)
            elif point.role != backend.operator_role:
                grants.setdefault(point.collection, []).append(RoleGrant(point.role, point.access))

        collections = []
        for name in names:
            recorded = meta.get(meta_point_id(name))
            model = recorded.get("embedding_model") if recorded else None
            detail = details[name]
            collections.append(
                CollectionInfo(
                    name=name,
                    points=detail.points,
                    vector_size=detail.vector_size,
                    distance=detail.distance,
                    status=detail.status,
                    embedding_model=model if isinstance(model, str) and model else None,
                    has_meta=recorded is not None,
                    grants=tuple(sorted(grants.get(name, []), key=lambda g: g.role)),
                )
            )
        return CollectionsView(
            available=True,
            collections=tuple(collections),
            global_roles=tuple(sorted(global_roles)),
            operator_role=backend.operator_role,
            operator_granted=operator_granted,
            warnings=backend.warnings,
        )

    async def _read_details(self, names: list[str]) -> dict[str, _Details]:
        limit = asyncio.Semaphore(DETAIL_CONCURRENCY)

        async def one(name: str) -> tuple[str, _Details]:
            async with limit:
                try:
                    result = await self._client.request("GET", collection_path(name))
                except QdrantUnavailable:
                    raise
                except QdrantError:
                    # Gone between the listing and now, or unreadable: show the name.
                    return name, _NO_DETAILS
            return name, _parse_details(result)

        return dict(await asyncio.gather(*(one(name) for name in names)))

    async def _read_meta(self, names: list[str]) -> dict[str, dict[str, Any]]:
        """Meta payloads by point id; empty when the meta collection does not exist."""
        if not names:
            return {}
        path = f"{collection_path(self._backend.meta_collection)}/points"
        try:
            result = await self._client.request(
                "POST",
                path,
                json={
                    "ids": [meta_point_id(name) for name in names],
                    "with_payload": True,
                    "with_vector": False,
                },
            )
        except QdrantUnavailable:
            raise
        except QdrantError as exc:
            if exc.status == 404:
                return {}
            raise
        return {
            str(point["id"]): point["payload"]
            for point in result or []
            if isinstance(point, dict) and isinstance(point.get("payload"), dict)
        }

    async def _read_acl(self) -> list[_AclPoint]:
        """Every grant, paged as the MCP server pages it; empty before the first write."""
        path = f"{collection_path(self._backend.acl_collection)}/points/scroll"
        points: list[_AclPoint] = []
        offset: Any = None
        while True:
            body: dict[str, Any] = {
                "limit": SCROLL_PAGE_SIZE,
                "with_payload": True,
                "with_vector": False,
            }
            if offset is not None:
                body["offset"] = offset
            try:
                result = await self._client.request("POST", path, json=body)
            except QdrantUnavailable:
                raise
            except QdrantError as exc:
                if exc.status == 404:
                    return points
                raise
            page = result if isinstance(result, dict) else {}
            points.extend(_parse_acl(page.get("points", [])))
            offset = page.get("next_page_offset")
            if offset is None:
                return points

    # ── writing ─────────────────────────────────────────────────────────────

    async def create(
        self,
        name: str,
        vector_size: int,
        embedding_model: str | None,
        grants: Iterable[RoleGrant],
    ) -> None:
        """Create a collection the way the ingester would, then record who may use it.

        A failure after the collection exists removes it again, with what was written
        for it, so a retry starts clean. Raises `CollectionExists` for a taken name.
        """
        self._require_key()
        backend = self._backend
        name = validate_collection_name(name, backend.system_collections)
        size = validate_vector_size(vector_size)
        model = validate_embedding_model(embedding_model)
        roles = normalise_grants(grants, backend.operator_role)

        if await self._exists(name):
            raise CollectionExists(name)
        try:
            await self._client.request(
                "PUT",
                collection_path(name),
                json={"vectors": {"size": size, "distance": "Cosine"}},
            )
        except QdrantUnavailable:
            raise
        except QdrantError as exc:
            if exc.status == 409:
                raise CollectionExists(name) from exc
            raise

        try:
            for field in PAYLOAD_INDEX_FIELDS:
                await self._client.request(
                    "PUT",
                    f"{collection_path(name)}/index",
                    params={"wait": "true"},
                    json={"field_name": field, "field_schema": "keyword"},
                )
            if model is not None:
                await self._write_meta(name, model, size)
            await self._ensure_acl_collection()
            acl = await self._read_acl()
            await self._apply_grants(name, roles, acl)
            await self._ensure_operator(acl)
        except Exception:
            await self._discard(name)
            raise

    async def delete(self, name: str) -> None:
        """Delete a collection together with its meta point and its grants.

        Idempotent: a collection that is already gone still has its leftovers removed,
        which is how a delete that stopped half-way is finished.
        """
        self._require_key()
        name = _existing_name(name, self._backend.system_collections)
        try:
            await self._client.request("DELETE", collection_path(name))
        except QdrantUnavailable:
            raise
        except QdrantError as exc:
            if exc.status != 404:
                raise
        await self._purge(name)

    async def set_roles(self, name: str, grants: Iterable[RoleGrant]) -> None:
        """Make `grants` the complete list of roles of a collection."""
        self._require_key()
        backend = self._backend
        name = _existing_name(name, backend.system_collections)
        roles = normalise_grants(grants, backend.operator_role)
        if not await self._exists(name):
            raise CollectionNotFound(name)
        await self._ensure_acl_collection()
        acl = await self._read_acl()
        await self._apply_grants(name, roles, acl)
        await self._ensure_operator(acl)

    async def ensure_operator_grant(self) -> bool:
        """Write the operator's global grant if it is missing; True if it was written."""
        self._require_key()
        await self._ensure_acl_collection()
        return await self._ensure_operator(await self._read_acl())

    # ── internals ───────────────────────────────────────────────────────────

    def _require_key(self) -> None:
        if not self._backend.api_key:
            raise QdrantUnavailable(0, _NO_KEY)

    async def _exists(self, name: str) -> bool:
        try:
            await self._client.request("GET", collection_path(name))
        except QdrantUnavailable:
            raise
        except QdrantError as exc:
            if exc.status == 404:
                return False
            raise
        return True

    async def _ensure_system_collection(self, name: str, index_field: str | None) -> None:
        """Create a meta or ACL collection exactly as the MCP server does, if missing."""
        if await self._exists(name):
            return
        try:
            await self._client.request(
                "PUT",
                collection_path(name),
                json={"vectors": {"size": 1, "distance": "Cosine"}},
            )
        except QdrantUnavailable:
            raise
        except QdrantError as exc:
            # Someone else created it between the check and now.
            if exc.status not in (400, 409):
                raise
        if index_field is not None:
            await self._client.request(
                "PUT",
                f"{collection_path(name)}/index",
                params={"wait": "true"},
                json={"field_name": index_field, "field_schema": "keyword"},
            )

    async def _ensure_acl_collection(self) -> None:
        await self._ensure_system_collection(self._backend.acl_collection, "role")

    async def _write_points(self, collection: str, points: list[dict[str, Any]]) -> None:
        await self._client.request(
            "PUT",
            f"{collection_path(collection)}/points",
            params={"wait": "true"},
            json={"points": points},
        )

    async def _delete_points(self, collection: str, ids: list[str]) -> None:
        await self._client.request(
            "POST",
            f"{collection_path(collection)}/points/delete",
            params={"wait": "true"},
            json={"points": ids},
        )

    async def _write_meta(self, name: str, model: str, size: int) -> None:
        meta = self._backend.meta_collection
        await self._ensure_system_collection(meta, None)
        await self._write_points(
            meta,
            [
                {
                    "id": meta_point_id(name),
                    "vector": _PLACEHOLDER_VECTOR,
                    "payload": {
                        "collection": name,
                        "embedding_model": model,
                        "vector_dimension": size,
                    },
                }
            ],
        )

    @staticmethod
    def _acl_point(
        role: str, collection: str, access: str, doc_policy: Any, point_id: str | None = None
    ) -> dict[str, Any]:
        return {
            "id": point_id or acl_point_id(role, collection),
            "vector": _PLACEHOLDER_VECTOR,
            # `doc_policy` is stored as an explicit null when absent, as `model_dump()`
            # of the MCP server's entry does.
            "payload": {
                "role": role,
                "collection": collection,
                "access": access,
                "doc_policy": doc_policy,
            },
        }

    async def _apply_grants(
        self, name: str, desired: tuple[RoleGrant, ...], acl: list[_AclPoint]
    ) -> None:
        """Change the grants of `name` to `desired`, writing only what differs.

        An existing point keeps its id and its `doc_policy`: the policy is not edited
        here, and an access change must not throw it away.
        """
        operator = self._backend.operator_role
        current = {
            point.role: point
            for point in acl
            if point.collection == name and point.access in ACCESS_LEVELS and point.role != operator
        }
        wanted = {grant.role for grant in desired}

        upserts = []
        for grant in desired:
            existing = current.get(grant.role)
            if existing is not None and existing.access == grant.access:
                continue
            upserts.append(
                self._acl_point(
                    grant.role,
                    name,
                    grant.access,
                    existing.doc_policy if existing else None,
                    existing.id if existing else None,
                )
            )
        stale = [point.id for role, point in current.items() if role not in wanted]

        acl_collection = self._backend.acl_collection
        if upserts:
            await self._write_points(acl_collection, upserts)
        if stale:
            await self._delete_points(acl_collection, stale)

    async def _ensure_operator(self, acl: list[_AclPoint]) -> bool:
        operator = self._backend.operator_role
        if any(point.role == operator and point.access == GLOBAL_ACCESS for point in acl):
            return False
        await self._write_points(
            self._backend.acl_collection,
            [self._acl_point(operator, GLOBAL_COLLECTION, GLOBAL_ACCESS, None)],
        )
        return True

    async def _purge(self, name: str) -> None:
        """Remove a collection's meta point and its per-collection grants."""
        try:
            await self._delete_points(self._backend.meta_collection, [meta_point_id(name)])
        except QdrantUnavailable:
            raise
        except QdrantError as exc:
            if exc.status != 404:
                raise

        # A global `m` grant is left alone even if its collection field names this
        # collection: the MCP server treats it as global, so it is not this one's.
        stale = [
            point.id
            for point in await self._read_acl()
            if point.collection == name and point.access in ACCESS_LEVELS
        ]
        if stale:
            await self._delete_points(self._backend.acl_collection, stale)

    async def _discard(self, name: str) -> None:
        """Best-effort removal of a half-created collection; the caller re-raises."""
        try:
            await self._client.request("DELETE", collection_path(name))
            await self._purge(name)
        except QdrantError:
            return


_NO_KEY = "QDRANT_JWT_SECRET is not set in ai/rag/.env, so the manager has no api-key for Qdrant."


def _with_reason(view: CollectionsView, reason: str) -> CollectionsView:
    return CollectionsView(
        available=False,
        reason=reason,
        operator_role=view.operator_role,
        warnings=view.warnings,
    )
