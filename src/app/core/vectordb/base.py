"""What a vector database connection is, and what a connection *type* has to provide.

A connection is a name, a type and the values that type needs (for Qdrant an address and
an optional api-key). The type decides how the values are validated, written to the
store, tested and what can be done with the connection afterwards. Adding another vector
database is registering another `ConnectionType`; nothing else in the page, the API or
the store has to learn about it.

The form of a type is data (`FieldSpec`), not markup: the dialog renders whatever the
registered types declare.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

import httpx

from app.core.vectordb.errors import InvalidConnectionError

# The type of every entry that carries none: an entry without a type is a Qdrant, which
# is what the ingester's file format means by it.
DEFAULT_TYPE = "qdrant"

# What a type can do with a connection beyond holding it. The Collections page lists
# only connections of a type that has "collections".
CAPABILITY_COLLECTIONS = "collections"

KeyState = Literal["ok", "none", "unreadable"]


@dataclass(frozen=True)
class FieldSpec:
    """One input of the connection dialog."""

    name: str
    label: str
    # `secret` is write-only: never prefilled, never returned, kept when left empty.
    kind: Literal["url", "text", "secret"]
    required: bool = False
    help: str = ""
    placeholder: str = ""


@dataclass(frozen=True)
class Connection:
    """A connection as the manager uses it, with the api-key decrypted.

    The key stays out of `repr`, so an exception or a log line that prints the object
    does not carry it.
    """

    name: str
    type: str
    url: str
    api_key: str = field(default="", repr=False)
    key_state: KeyState = "none"
    # "file" is an entry of the store; "env" is the default connection derived from the
    # stack's own settings while the store has none.
    source: Literal["file", "env"] = "file"
    # It points at the stack's own Qdrant, the one its MCP server enforces roles on.
    integrated: bool = False


@dataclass(frozen=True)
class ProbeEnv:
    """What a type needs to reach a database from this process."""

    cafile: str | None = None
    timeout: float = 5.0
    # Tests hand in a transport; production uses the network.
    transport: httpx.AsyncBaseTransport | None = None


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    detail: str
    # How many collections the database reported, when the type can count them.
    collections: int | None = None


class ConnectionType(Protocol):
    """One kind of vector database."""

    id: str
    label: str
    fields: tuple[FieldSpec, ...]
    capabilities: frozenset[str]

    def validate(self, values: Mapping[str, str]) -> dict[str, str]:
        """The non-secret values, checked and normalised.

        Raises `InvalidConnectionError`.
        """

    def values_of(self, entry: Mapping[str, Any]) -> dict[str, str]:
        """The non-secret values of a stored entry, for the edit dialog."""

    def address_of(self, values: Mapping[str, str]) -> str:
        """Where the database is. A stored key is only ever sent to this address."""

    def to_entry(
        self, name: str, values: Mapping[str, str], api_key_token: str | None
    ) -> dict[str, Any]:
        """The mapping written to the store: only the keys its format allows."""

    async def probe(self, connection: Connection, env: ProbeEnv) -> ProbeResult:
        """Reach the database with this connection. Never raises for a database problem."""


_REGISTRY: dict[str, ConnectionType] = {}


def register_type(connection_type: ConnectionType) -> None:
    _REGISTRY[connection_type.id] = connection_type


def get_type(type_id: str) -> ConnectionType:
    try:
        return _REGISTRY[type_id]
    except KeyError:
        known = ", ".join(sorted(_REGISTRY)) or "none"
        raise InvalidConnectionError(
            f"Unknown connection type {type_id!r} (known: {known})."
        ) from None


def all_types() -> list[ConnectionType]:
    return list(_REGISTRY.values())
