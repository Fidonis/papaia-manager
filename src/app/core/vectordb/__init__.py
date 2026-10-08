"""Vector database connections: the types, and the ingester's store they are kept in.

Importing the package registers the connection types that ship with the manager.
"""
from __future__ import annotations

from app.core.vectordb.base import (
    CAPABILITY_COLLECTIONS,
    DEFAULT_TYPE,
    Connection,
    ConnectionType,
    FieldSpec,
    KeyState,
    ProbeEnv,
    ProbeResult,
    all_types,
    get_type,
    register_type,
)
from app.core.vectordb.errors import (
    ConnectionExistsError,
    ConnectionFileError,
    ConnectionInUseError,
    ConnectionsReadOnlyError,
    ConnectionStoreError,
    InvalidConnectionError,
    ProtectedConnectionError,
    SecretMissingError,
    StaleConnectionError,
    UnknownConnectionError,
)
from app.core.vectordb.qdrant_type import QdrantType

register_type(QdrantType())

__all__ = [
    "CAPABILITY_COLLECTIONS",
    "DEFAULT_TYPE",
    "Connection",
    "ConnectionExistsError",
    "ConnectionFileError",
    "ConnectionInUseError",
    "ConnectionStoreError",
    "ConnectionType",
    "ConnectionsReadOnlyError",
    "FieldSpec",
    "InvalidConnectionError",
    "KeyState",
    "ProbeEnv",
    "ProbeResult",
    "ProtectedConnectionError",
    "QdrantType",
    "SecretMissingError",
    "StaleConnectionError",
    "UnknownConnectionError",
    "all_types",
    "get_type",
    "register_type",
]
