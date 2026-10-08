"""The connections of the RAG system: what the page lists and what it may change.

Everything that is a rule rather than file handling lives here:

* **The default connection** is the entry named `default`: the integrated Qdrant, as the
  ingester reaches it, with the stack's api-key. It is created when it is missing (at
  start and on first use), never overwritten, editable, and neither renamed nor deleted.
  The name is the marker, so there is no second place that could drift from the file.
  While the store has no such entry, `resolve("default")` answers from the stack's own
  settings, so the Collections page does not depend on the file being usable.
* **A stored key goes only to the address it was stored with.** Testing a stored
  connection ignores an address that is sent along, and changing the address of a
  connection with a key needs the key again (or its removal). Otherwise an administrator
  could send the stack's api-key to a host of their choosing.
* **Names are fixed** once created, because ingest jobs refer to a connection by name and
  nothing would tell them. Deleting is refused while a job uses the connection, and
  changing its address asks for a confirmation that names the jobs.
* **The api-key is never an output.** Views carry `has_key` and a state, never a value.
  Keys are stored as the ingester stores them (see `crypto.py`); a key that is not
  replaced or removed keeps its stored token byte for byte, because a Fernet token is
  randomised and re-encrypting would rewrite the file for nothing.
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal

from app.config import Settings
from app.core.audit import write_audit_entry
from app.core.rag import INTEGRATED_URL, RagSecrets, rag_active, rag_secrets
from app.core.vectordb import crypto
from app.core.vectordb.base import (
    CAPABILITY_COLLECTIONS,
    DEFAULT_TYPE,
    Connection,
    KeyState,
    ProbeEnv,
    ProbeResult,
    get_type,
)
from app.core.vectordb.errors import (
    ConnectionExistsError,
    ConnectionInUseError,
    InvalidConnectionError,
    ProtectedConnectionError,
    SecretMissingError,
    StaleConnectionError,
    UnknownConnectionError,
)
from app.core.vectordb.ingest_file import (
    ConnectionEntry,
    FileSnapshot,
    IngestFileRepository,
    entry_etag,
    find_entry,
    is_valid_name,
    remove_entry,
)
from app.core.vectordb.jobs_usage import JobsUsage, jobs_using

logger = logging.getLogger(__name__)

DEFAULT_NAME = "default"

# A seed that failed is not tried again on every request.
_SEED_RETRY_SECONDS = 60.0
_seed_failed_at: dict[str, float] = {}
_seed_lock = threading.Lock()

_NO_CONNECTIONS_SECRET = (
    "QI_CONNECTIONS_SECRET is not set in ai/rag/.env, so no api-key can be stored or read. "
    "Connections without a key still work."
)
_NO_API_KEY = (
    "QDRANT_JWT_SECRET is not set in ai/rag/.env, so the manager has no api-key for Qdrant."
)

NAME_RULE = (
    "A connection name starts with a lowercase letter or digit and contains only lowercase "
    "letters, digits, '-' and '_' (at most 64 characters)."
)


def same_address(a: str, b: str) -> bool:
    return a.strip().rstrip("/").lower() == b.strip().rstrip("/").lower()


@dataclass(frozen=True)
class ConnectionView:
    """One connection as the page and the API show it. No secret."""

    name: str
    type: str
    type_label: str
    address: str
    # The values the edit dialog starts from (never the key).
    fields: Mapping[str, str]
    # Where the manager itself connects, when that is not the stored address.
    reach_address: str | None
    has_key: bool
    key_state: KeyState
    is_default: bool
    # The stored key is no longer the stack's current one (the secret was rotated).
    key_drift: bool
    # Points at the integrated Qdrant, the one the stack's MCP server enforces roles on.
    integrated: bool
    collections: bool
    used_by: tuple[str, ...]
    etag: str


@dataclass(frozen=True)
class ConnectionsState:
    connections: tuple[ConnectionView, ...]
    revision: str
    exists: bool
    writable: bool
    secret_configured: bool
    # Problems of the file as the ingester would report them, one line each.
    issues: tuple[str, ...]
    # The file cannot be changed at all (not YAML, unsupported version, ...).
    structural_error: str | None
    jobs_error: str | None
    # There is no stored default connection; the stack's own settings stand in for it.
    default_stored: bool


@dataclass(frozen=True)
class _Stored:
    stored_address: str
    connection: Connection


class ConnectionService:
    """Reads and changes the connection store on behalf of the manager."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._config_dir = settings.papaia_config_dir
        self._repo = IngestFileRepository(settings.papaia_config_dir)

    # ── reading ─────────────────────────────────────────────────────────────

    def state(self) -> ConnectionsState:
        snapshot = self._repo.snapshot()
        secrets = rag_secrets(self._config_dir)
        usage = jobs_using(self._config_dir)

        views: list[ConnectionView] = []
        seen: set[str] = set()
        for raw in snapshot.entries:
            view = self._view(raw, secrets, usage)
            if view is not None and view.name not in seen:
                seen.add(view.name)
                views.append(view)
        views.sort(key=lambda view: (not view.is_default, view.name))
        return ConnectionsState(
            connections=tuple(views),
            revision=snapshot.revision,
            exists=snapshot.exists,
            writable=snapshot.writable,
            secret_configured=bool(secrets.connections_secret),
            issues=tuple(str(issue) for issue in snapshot.issues),
            structural_error=snapshot.structural_error,
            jobs_error=usage.error,
            default_stored=DEFAULT_NAME in seen,
        )

    def _view(
        self, raw: Mapping[str, Any], secrets: RagSecrets, usage: JobsUsage
    ) -> ConnectionView | None:
        try:
            entry = ConnectionEntry.model_validate(raw)
        except ValueError:
            return None  # reported as an issue of the file; there is nothing to show
        ctype = get_type(DEFAULT_TYPE)
        fields = ctype.values_of(raw)
        address = ctype.address_of(fields)
        plain, key_state = self._read_key(entry.api_key, secrets)
        is_default = entry.name == DEFAULT_NAME
        integrated = same_address(address, INTEGRATED_URL)
        reach = self._reach(address)
        return ConnectionView(
            name=entry.name,
            type=ctype.id,
            type_label=ctype.label,
            address=address,
            fields=fields,
            reach_address=None if same_address(reach, address) else reach,
            has_key=entry.api_key is not None,
            key_state=key_state,
            is_default=is_default,
            key_drift=bool(
                is_default
                and integrated
                and key_state == "ok"
                and secrets.api_key
                and plain != secrets.api_key
            ),
            integrated=integrated,
            collections=CAPABILITY_COLLECTIONS in ctype.capabilities,
            used_by=usage.jobs_of(entry.name),
            etag=entry_etag(raw),
        )

    @staticmethod
    def _read_key(token: str | None, secrets: RagSecrets) -> tuple[str, KeyState]:
        if not token:
            return "", "none"
        try:
            return crypto.decrypt(token, secrets.connections_secret), "ok"
        except crypto.SecretError:
            return "", "unreadable"

    def _reach(self, address: str) -> str:
        """Where this process connects to a stored address.

        The integrated Qdrant is stored the way the ingester reaches it, and the manager
        may be on another network (a development machine, a different service name).
        Every other address is used as it is.
        """
        return self._settings.qdrant_url if same_address(address, INTEGRATED_URL) else address

    def _stored(self, name: str) -> _Stored:
        snapshot = self._repo.snapshot()
        secrets = rag_secrets(self._config_dir)
        for raw in snapshot.entries:
            if raw.get("name") != name:
                continue
            try:
                entry = ConnectionEntry.model_validate(raw)
            except ValueError:
                raise UnknownConnectionError(
                    f"The connection {name!r} is not valid in connections.yaml."
                ) from None
            plain, key_state = self._read_key(entry.api_key, secrets)
            return _Stored(
                entry.url,
                Connection(
                    name=entry.name,
                    type=DEFAULT_TYPE,
                    url=self._reach(entry.url),
                    api_key=plain,
                    key_state=key_state,
                    integrated=same_address(entry.url, INTEGRATED_URL),
                ),
            )
        if name == DEFAULT_NAME:
            # The store has no default (yet, or it cannot be read): the stack's own
            # settings are the answer, so the Collections page keeps working.
            return _Stored(
                self._settings.qdrant_url,
                Connection(
                    name=DEFAULT_NAME,
                    type=DEFAULT_TYPE,
                    url=self._settings.qdrant_url,
                    api_key=secrets.api_key,
                    key_state="ok" if secrets.api_key else "none",
                    source="env",
                    integrated=True,
                ),
            )
        reason = f" ({snapshot.structural_error})" if snapshot.structural_error else ""
        raise UnknownConnectionError(f"There is no connection named {name!r}{reason}.")

    def resolve(self, name: str) -> Connection:
        """The connection to use for `name`, with its key decrypted."""
        return self._stored(name).connection

    # ── the default connection ──────────────────────────────────────────────

    def ensure_default(self) -> Literal["seeded", "present", "skipped"]:
        """Create the default connection if the store has none. Never raises.

        Skipped without the RAG profile, without either secret, while the file cannot be
        changed, and for a minute after a failed attempt. An entry of that name is left
        alone whatever it holds: it is the operator's.
        """
        config_dir = self._config_dir
        if not rag_active(config_dir):
            return "skipped"
        secrets = rag_secrets(config_dir)
        if not secrets.api_key or not secrets.connections_secret:
            return "skipped"
        with _seed_lock:
            failed = _seed_failed_at.get(config_dir)
            if failed is not None and time.monotonic() - failed < _SEED_RETRY_SECONDS:
                return "skipped"
        try:
            snapshot = self._repo.snapshot()
            if snapshot.structural_error is not None:
                self._seed_failed(config_dir, snapshot.structural_error)
                return "skipped"
            if any(raw.get("name") == DEFAULT_NAME for raw in snapshot.entries):
                return "present"
            token = crypto.encrypt(secrets.api_key, secrets.connections_secret)
            created, _ = self._put_default(token, replace=False)
        except Exception as exc:  # the seed must never take a request or the start down
            self._seed_failed(config_dir, str(exc) or type(exc).__name__)
            return "skipped"
        if not created:
            return "present"
        write_audit_entry(
            config_dir,
            user="manager",
            action="rag.connection.seed",
            target=DEFAULT_NAME,
            params={"address": INTEGRATED_URL},
        )
        return "seeded"

    @staticmethod
    def _seed_failed(config_dir: str, reason: str) -> None:
        logger.warning("the default connection could not be created: %s", reason)
        with _seed_lock:
            _seed_failed_at[config_dir] = time.monotonic()

    def _put_default(self, token: str, *, replace: bool) -> tuple[bool, FileSnapshot]:
        """Write the default entry. True if it was written, False if it already existed."""
        ctype = get_type(DEFAULT_TYPE)
        written = False

        def mutate(document: dict[str, Any]) -> None:
            nonlocal written
            new = ctype.to_entry(DEFAULT_NAME, {"url": INTEGRATED_URL}, token)
            entry = find_entry(document, DEFAULT_NAME)
            if entry is None:
                document["connections"].append(new)
            elif replace:
                entry.clear()
                entry.update(new)
            else:
                written = False
                return
            written = True

        snapshot = self._repo.update(mutate)
        return written, snapshot

    def reset_default(self) -> ConnectionView:
        """Point the default connection at the integrated Qdrant again, with the stack's key."""
        secrets = rag_secrets(self._config_dir)
        if not secrets.connections_secret:
            raise SecretMissingError(_NO_CONNECTIONS_SECRET)
        if not secrets.api_key:
            raise SecretMissingError(_NO_API_KEY)
        token = crypto.encrypt(secrets.api_key, secrets.connections_secret)
        _, snapshot = self._put_default(token, replace=True)
        return self._view_in(snapshot, DEFAULT_NAME)

    # ── changing ────────────────────────────────────────────────────────────

    def create(
        self,
        *,
        name: str,
        type_id: str,
        values: Mapping[str, str],
        api_key: str | None,
    ) -> ConnectionView:
        name = self._valid_name(name)
        ctype = get_type(type_id)
        clean = ctype.validate(values)
        token = self._encrypt(api_key)

        def mutate(document: dict[str, Any]) -> None:
            if find_entry(document, name) is not None:
                raise ConnectionExistsError(f"A connection named {name!r} already exists.")
            document["connections"].append(ctype.to_entry(name, clean, token))

        return self._view_in(self._repo.update(mutate), name)

    def update(
        self,
        name: str,
        *,
        values: Mapping[str, str],
        api_key: str | None,
        clear_api_key: bool,
        etag: str,
        confirm_jobs: bool,
    ) -> ConnectionView:
        ctype = get_type(DEFAULT_TYPE)
        clean = ctype.validate(values)
        new_token = self._encrypt(api_key)
        if new_token is not None and clear_api_key:
            raise InvalidConnectionError("Enter a new api-key or remove the stored one, not both.")
        usage = jobs_using(self._config_dir)

        def mutate(document: dict[str, Any]) -> None:
            entry = find_entry(document, name)
            if entry is None:
                raise UnknownConnectionError(f"There is no connection named {name!r}.")
            if entry_etag(entry) != etag:
                raise StaleConnectionError(
                    f"The connection {name!r} was changed by somebody else. Reload and try again."
                )
            stored = entry.get("api_key")
            has_key = isinstance(stored, str) and bool(stored)
            moved = not same_address(
                ctype.address_of(ctype.values_of(entry)), ctype.address_of(clean)
            )
            if moved and has_key and new_token is None and not clear_api_key:
                raise InvalidConnectionError(
                    "The stored api-key belongs to the old address and is never sent to a "
                    "different one. Enter the api-key for the new address, or remove the "
                    "stored key."
                )
            if moved and not confirm_jobs:
                if usage.error is not None:
                    raise ConnectionInUseError(
                        f"{usage.error} Confirm to change the address anyway."
                    )
                jobs = usage.jobs_of(name)
                if jobs:
                    raise ConnectionInUseError(
                        f"The jobs {', '.join(jobs)} write to this connection and would write "
                        "to the new address. Confirm to change it.",
                        jobs,
                    )
            if new_token is not None:
                token: str | None = new_token
            elif clear_api_key or not has_key:
                token = None
            else:
                token = stored if isinstance(stored, str) else None  # byte for byte
            replacement = ctype.to_entry(name, clean, token)
            entry.clear()
            entry.update(replacement)

        return self._view_in(self._repo.update(mutate), name)

    def delete(self, name: str, *, etag: str) -> None:
        if name == DEFAULT_NAME:
            raise ProtectedConnectionError(
                "The default connection cannot be deleted. It can be edited, or reset to the "
                "integrated Qdrant."
            )
        usage = jobs_using(self._config_dir)
        if usage.error is not None:
            raise ConnectionInUseError(
                f"{usage.error} The connection is not deleted while that cannot be checked."
            )
        jobs = usage.jobs_of(name)
        if jobs:
            raise ConnectionInUseError(
                f"The jobs {', '.join(jobs)} write to this connection. Change their target first.",
                jobs,
            )

        def mutate(document: dict[str, Any]) -> None:
            entry = find_entry(document, name)
            if entry is None:
                raise UnknownConnectionError(f"There is no connection named {name!r}.")
            if entry_etag(entry) != etag:
                raise StaleConnectionError(
                    f"The connection {name!r} was changed by somebody else. Reload and try again."
                )
            remove_entry(document, name)

        self._repo.update(mutate)

    # ── testing ─────────────────────────────────────────────────────────────

    async def test(
        self,
        *,
        name: str | None,
        type_id: str,
        values: Mapping[str, str],
        api_key: str | None,
        env: ProbeEnv,
    ) -> ProbeResult:
        """Reach a database with a stored connection or with what was typed.

        A stored key is only used for the stored address: with a name and no typed key,
        an address that differs from the stored one is refused instead of tested.
        """
        typed = (api_key or "").strip()
        if name is None:
            ctype = get_type(type_id)
            clean = ctype.validate(values)
            return await ctype.probe(
                Connection("new", ctype.id, clean["url"], typed, "ok" if typed else "none"),
                env,
            )

        stored = self._stored(name)
        ctype = get_type(stored.connection.type)
        supplied = values.get("url", "").strip()
        if typed:
            clean = ctype.validate({"url": supplied or stored.stored_address})
            connection = Connection(
                name,
                ctype.id,
                self._reach(clean["url"]),
                typed,
                "ok",
                stored.connection.source,
                same_address(clean["url"], INTEGRATED_URL),
            )
        else:
            if supplied and not same_address(supplied, stored.stored_address):
                raise InvalidConnectionError(
                    "Enter the api-key to test a different address. A stored key is only "
                    "used for the address it was stored with."
                )
            if stored.connection.key_state == "unreadable":
                return ProbeResult(
                    False,
                    "The stored api-key cannot be decrypted with the current "
                    "QI_CONNECTIONS_SECRET. Enter the key again.",
                )
            connection = stored.connection
        return await ctype.probe(connection, env)

    # ── helpers ─────────────────────────────────────────────────────────────

    @staticmethod
    def _valid_name(name: str) -> str:
        if not isinstance(name, str) or not is_valid_name(name):
            raise InvalidConnectionError(NAME_RULE)
        return name

    def _encrypt(self, api_key: str | None) -> str | None:
        value = (api_key or "").strip()
        if not value:
            return None
        secret = rag_secrets(self._config_dir).connections_secret
        if not secret:
            raise SecretMissingError(_NO_CONNECTIONS_SECRET)
        return crypto.encrypt(value, secret)

    def _view_in(self, snapshot: FileSnapshot, name: str) -> ConnectionView:
        """The view of an entry in the file as it was right after a write.

        Taken from that snapshot rather than from a new read: the ingester's interface
        writes the same file without comparing first, and a save of its own landing in
        between would otherwise answer a successful change with "no such connection".
        """
        secrets = rag_secrets(self._config_dir)
        usage = jobs_using(self._config_dir)
        for raw in snapshot.entries:
            if raw.get("name") == name:
                view = self._view(raw, secrets, usage)
                if view is not None:
                    return view
        raise UnknownConnectionError(f"There is no connection named {name!r}.")


def audit_key_action(api_key: str | None, clear: bool) -> str:
    """How a change treated the key, for the audit log: never the key, never a token."""
    if clear:
        return "removed"
    return "replaced" if (api_key or "").strip() else "kept"


__all__ = [
    "DEFAULT_NAME",
    "ConnectionService",
    "ConnectionView",
    "ConnectionsState",
    "audit_key_action",
    "same_address",
]
