"""The ways a change to the connection store is refused.

Each class carries the HTTP status the API answers with, so a route translates any of
them in one place. They are all answers to a request, not failures of the manager.
"""
from __future__ import annotations


class ConnectionStoreError(Exception):
    """A change to the connections was refused. `status` is the HTTP answer."""

    status = 400


class InvalidConnectionError(ConnectionStoreError):
    """The request names something that cannot be stored."""

    status = 422


class UnknownConnectionError(ConnectionStoreError):
    status = 404


class ConnectionExistsError(ConnectionStoreError):
    status = 409


class StaleConnectionError(ConnectionStoreError):
    """The entry changed after the page loaded it (somebody else wrote the file)."""

    status = 409


class ProtectedConnectionError(ConnectionStoreError):
    """The default connection cannot be removed or renamed."""

    status = 409


class SecretMissingError(ConnectionStoreError):
    """`QI_CONNECTIONS_SECRET` is not set, so no api-key can be stored or read."""

    status = 409


class ConnectionInUseError(ConnectionStoreError):
    """Ingest jobs write to this connection."""

    status = 409

    def __init__(self, message: str, jobs: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.jobs = jobs


class ConnectionFileError(ConnectionStoreError):
    """The connections file cannot be changed as it is: broken, or changed under us."""

    status = 409


class ConnectionsReadOnlyError(ConnectionStoreError):
    """The catalog directory does not accept writes from the manager."""

    status = 503
