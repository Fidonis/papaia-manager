"""`papaia.audit/1` rows for every write of the manager, through the kit's outbox writer.

Every call of `app.core.audit.write_audit_entry` -- the one place the manager records a write,
a send or a delete -- also hands one row to this module (governance GV-01). The manager has no
database of its own, so the rows go to a SQLite outbox file written with the kit's
`FileAuditWriter`, each in a transaction of its own right after the effect (decision E-52). A
background thread delivers the outbox to the core's `papaia-audit` over mutual TLS and removes a
row only once it is acknowledged; while `papaia-audit` cannot be reached, the rows wait and the
manager keeps working.

A row says who acted, on which operation, with which result and correlation id -- never a
target, a parameter, a name or a token: those stay in the local audit log, which is the
operator's view. Who acted comes from the request (`AuditContextMiddleware`): the signed-in
person's `sub` and the manager's client as `azp`. Outside a request (the backup schedule, the
notifications) it is the manager's own service identity. A job keeps the identity and the
correlation id of the request that queued it (`app.core.jobs`).

Recording never fails the write it records: the effect has already happened. A row that
cannot be stored is logged with the class of the error only.
"""
from __future__ import annotations

import logging
import re
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from papaia_kit.audit import AUDIT_FORMAT
from papaia_kit.audit.transport import HttpsIngestTransport, IngestError, IngestTransport
from papaia_kit.audit.writer import FileAuditWriter
from papaia_kit.correlation import (
    CORRELATION_HEADER,
    correlation_id_from_headers,
    new_correlation_id,
)

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

    from app.config import Settings

logger = logging.getLogger(__name__)

# The source name papaia-audit knows the manager by (from its client certificate), and the
# writer, catalog id and service identity of its rows.
SOURCE = "papaia-manager"
CATALOG_ID = "manager"
FALLBACK_OPERATION = "manager.write"

Effect = Literal["write", "send", "delete"]
IdentityKind = Literal["person", "service"]

_OPERATION = re.compile(r"[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)*")
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")
_DELETE_WORDS = frozenset({"delete", "remove", "prune", "uninstall", "discard", "clear", "revoke"})
_SEND_PREFIXES = ("notify", "mail.test")
# Local results that mean the effect happened (`started`: a job was queued).
_ALLOWED = frozenset({"ok", "started"})
_DENIED = frozenset({"denied", "refused"})
# Refusals the local log records as a failure of the action.
_DENIAL_ACTIONS = {"step-up.refused": "step_up_required"}


@dataclass(frozen=True, slots=True)
class Actor:
    """Who a row names as acting."""

    sub: str
    identity_kind: IdentityKind
    azp: str
    service_account: str | None = None


SERVICE = Actor(sub=SOURCE, identity_kind="service", azp=SOURCE, service_account=SOURCE)


def person(sub: str, *, azp: str) -> Actor:
    return Actor(sub=sub, identity_kind="person", azp=azp)


_actor: ContextVar[Actor | None] = ContextVar("papaia_audit_actor", default=None)
_correlation: ContextVar[str | None] = ContextVar("papaia_audit_correlation", default=None)


@contextmanager
def bound(actor: Actor | None, correlation_id: str) -> Iterator[None]:
    """Name the actor and the correlation id of everything recorded inside the block."""
    actor_token = _actor.set(actor)
    correlation_token = _correlation.set(correlation_id)
    try:
        yield
    finally:
        _correlation.reset(correlation_token)
        _actor.reset(actor_token)


@dataclass(frozen=True, slots=True)
class Binding:
    """The actor and correlation id in effect, carried from a request into its job."""

    actor: Actor | None
    correlation_id: str | None


def current_binding() -> Binding:
    return Binding(actor=_actor.get(), correlation_id=_correlation.get())


@contextmanager
def rebound(binding: Binding) -> Iterator[None]:
    """Restore a binding; a job queued outside a request gets one correlation id of its own."""
    with bound(binding.actor, binding.correlation_id or new_correlation_id()):
        yield


def operation_name(action: str) -> str:
    """The action of the local log as an operation name of `papaia.audit/1`."""
    name = action.strip().lower().replace("-", "_").replace(":", ".").replace("/", ".")
    name = re.sub(r"\.{2,}", ".", name).strip(".")
    if len(name) <= 64 and _OPERATION.fullmatch(name):
        return name
    return FALLBACK_OPERATION


def effect_of(action: str) -> Effect:
    name = operation_name(action)
    if name.startswith(_SEND_PREFIXES):
        return "send"
    if _DELETE_WORDS.intersection(re.split(r"[._]", name)):
        return "delete"
    return "write"


def _result(action: str, result: str) -> tuple[str, str | None]:
    if action in _DENIAL_ACTIONS:
        return "denied", _DENIAL_ACTIONS[action]
    value = result.strip().lower()
    if value in _ALLOWED:
        return "allowed", None
    code = value if _ERROR_CODE.fullmatch(value) else "error"
    if value in _DENIED:
        return "denied", code
    return "error", code


def _timestamp(moment: datetime) -> str:
    moment = moment.astimezone(UTC)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def build_row(
    *,
    action: str,
    result: str,
    actor: Actor,
    correlation_id: str,
    event: str = "operation",
    object_ref: Mapping[str, str] | None = None,
    context: Mapping[str, str] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    status, code = _result(action, result)
    row: dict[str, Any] = {
        "format": AUDIT_FORMAT,
        "time": _timestamp(now or datetime.now(tz=UTC)),
        "correlation_id": correlation_id,
        "writer": SOURCE,
        "result": status,
        "event": event,
        "role": "caller",
        "sub": actor.sub,
        "identity_kind": actor.identity_kind,
        "service_account": actor.service_account,
        "azp": actor.azp,
        "catalog_id": CATALOG_ID,
        "operation": operation_name(action),
        "effect": effect_of(action),
        "error_code": code,
    }
    if object_ref is not None:
        row["object_ref"] = dict(object_ref)
    if context is not None:
        row["context"] = dict(context)
    return row


# -- the outbox --------------------------------------------------------------------------

_outbox_override: str | None = None
_writers: dict[Path, FileAuditWriter] = {}
_writers_lock = threading.Lock()


class _RecordOnly:
    """Transport of the recording writer: it records, the delivery thread sends."""

    def send(self, body: bytes) -> bytes:
        raise IngestError("unreachable", "record_only")


def configure(settings: Settings) -> None:
    """Use `AUDIT_OUTBOX_PATH` instead of the outbox in the config directory."""
    global _outbox_override  # noqa: PLW0603
    _outbox_override = settings.audit_outbox_path


def outbox_path(config_dir: str) -> Path:
    if _outbox_override:
        return Path(_outbox_override)
    return Path(config_dir) / "manager" / "audit" / "outbox.sqlite3"


def _writer(path: Path) -> FileAuditWriter:
    with _writers_lock:
        writer = _writers.get(path)
        if writer is None:
            # The writer creates the last directory itself (mode 0700), not its parents.
            path.parent.parent.mkdir(parents=True, exist_ok=True)
            writer = FileAuditWriter(path, _RecordOnly())
            _writers[path] = writer
        return writer


def record(
    config_dir: str,
    *,
    action: str,
    result: str,
    event: str = "operation",
    object_ref: Mapping[str, str] | None = None,
    context: Mapping[str, str] | None = None,
) -> None:
    """Store one row for a write that has happened; never raises."""
    try:
        row = build_row(
            action=action,
            result=result,
            actor=_actor.get() or SERVICE,
            correlation_id=_correlation.get() or new_correlation_id(),
            event=event,
            object_ref=object_ref,
            context=context,
        )
        _writer(outbox_path(config_dir)).record(row)
    except Exception as exc:  # the write it records has happened; see the module docstring
        logger.error("papaia.audit/1 row not written (%s)", type(exc).__name__)


def record_row(config_dir: str, row: Mapping[str, Any]) -> None:
    """Store a row another component built (the kit's preference write); never raises."""
    try:
        _writer(outbox_path(config_dir)).record(dict(row))
    except Exception as exc:  # the write it records has happened; see the module docstring
        logger.error("papaia.audit/1 row not written (%s)", type(exc).__name__)


# -- delivery ----------------------------------------------------------------------------


@dataclass
class Delivery:
    """The background thread that delivers one outbox file."""

    thread: threading.Thread
    stop_event: threading.Event

    def stop(self, timeout: float = 15.0) -> None:
        self.stop_event.set()
        self.thread.join(timeout)


def ingest_transport(settings: Settings) -> IngestTransport | None:
    """The mTLS transport to `papaia-audit`, or None while the client certificate is missing."""
    files = (settings.audit_tls_ca_file, settings.audit_tls_cert_file, settings.audit_tls_key_file)
    if not all(Path(name).is_file() for name in files):
        return None
    try:
        return HttpsIngestTransport(
            ca_file=settings.audit_tls_ca_file,
            cert_file=settings.audit_tls_cert_file,
            key_file=settings.audit_tls_key_file,
            url=settings.audit_ingest_url,
        )
    except (ValueError, OSError) as exc:
        logger.error("papaia-audit transport not usable (%s)", type(exc).__name__)
        return None


def start_delivery(
    config_dir: str, transport: IngestTransport, *, idle_interval: float = 1.0
) -> Delivery:
    path = outbox_path(config_dir)
    path.parent.parent.mkdir(parents=True, exist_ok=True)
    writer = FileAuditWriter(path, transport, idle_interval=idle_interval)
    stop_event = threading.Event()
    thread = threading.Thread(
        target=writer.run, args=(stop_event,), name="papaia-audit-delivery", daemon=True
    )
    thread.start()
    return Delivery(thread=thread, stop_event=stop_event)


# -- the request -------------------------------------------------------------------------


_REFUSED = 400
_FAILED = 500


def _from_page(scope: Scope) -> bool:
    path = str(scope.get("path", ""))
    if path.startswith("/api/"):
        return True
    return any(name.lower() == b"hx-request" for name, _ in scope.get("headers", []))


def log_refusal(scope: Scope, status: int, correlation_id: str) -> None:
    """Log a refused request of a page with its correlation id (AuditContextMiddleware)."""
    if status < _REFUSED or not _from_page(scope):
        return
    level = logging.WARNING if status >= _FAILED else logging.INFO
    logger.log(
        level,
        "request refused: %s %s answered %d, correlation id %s",
        scope.get("method", "GET"),
        scope.get("path", ""),
        status,
        correlation_id,
    )


class AuditContextMiddleware:
    """Binds the session's person and the request's correlation id for every row it writes.

    It runs inside the session middleware, so the session is decoded. The correlation id is
    the caller's `X-Papaia-Correlation-Id` when it is valid, otherwise a new one (a value too
    long or shaped like a token is replaced, never shortened); the response carries it back.

    A refused request of a page (an API call or an htmx request answered 4xx or 5xx) is logged
    with its correlation id: the page tells the person that id as the reference of its error
    message (static/js/core/errors.js), and this line is where it leads. The line names
    method, path and status, never the query, a header value or the body.
    """

    def __init__(self, app: ASGIApp, *, azp: Callable[[], str]) -> None:
        self.app = app
        self._azp = azp

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in scope["headers"]]
        correlation_id = correlation_id_from_headers(headers)
        actor = self._actor(scope)

        async def send_with_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                message["headers"] = [
                    *message.get("headers", []),
                    (CORRELATION_HEADER.lower().encode(), correlation_id.encode()),
                ]
                log_refusal(scope, int(message.get("status", 200)), correlation_id)
            await send(message)

        with bound(actor, correlation_id):
            await self.app(scope, receive, send_with_id)

    def _actor(self, scope: Scope) -> Actor | None:
        session = scope.get("session")
        user = session.get("user") if isinstance(session, dict) else None
        sub = user.get("sub") if isinstance(user, dict) else None
        if isinstance(sub, str) and sub:
            return person(sub, azp=self._azp())
        return None
