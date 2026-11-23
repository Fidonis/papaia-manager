"""The permission report of the platform for auditors (OPS-19, security 1.16).

papaia-audit builds the report (`POST /v1/access-reports/build`, format
`papaia.access-report/1`) and records every retrieval as `inventory_exported`; the manager
only shows what papaia-audit answers to the signed-in auditor:

* The person's session token is exchanged for exactly the audience `papaia-audit` with the
  manager's client (RFC 8693 through the kit's exchange client: `audience=papaia-audit`,
  `scope=aud-papaia-audit`). The exchanged token is used for the one call of this request
  and dropped from the exchange client right after it; it never reaches the session, a log
  or the page.
* The call goes over mutual TLS with the manager's platform client certificate (the files of
  `AUDIT_TLS_*`) and carries the request's correlation id. The manager holds no Keycloak
  credential and no realm right for it: no Admin API call, no Docker call.
* An answer is used only when it is what was asked for: a JSON report valid against the
  published schema (`schemas/access-report.v1.schema.json`), of the requested kind, rows
  sorted by key, each row's digest and the report's checksum as the contract computes them;
  or a CSV with the header of the requested kind. Anything else is `Unavailable` and nothing
  of it is shown: no partial data.
* A refusal of papaia-audit (403) is `Refused` with its own message and hint; any other
  failure (exchange, connection, status, answer) is `Unavailable` with a fixed reason.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import ssl
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from functools import cache
from pathlib import Path
from typing import Any, Protocol

import httpx

# jsonschema comes with the kit (pinned in uv.lock), which validates its contracts with it too.
from jsonschema import Draft202012Validator  # type: ignore[import-untyped]
from papaia_kit.exchange import ExchangedToken, ExchangeError

logger = logging.getLogger(__name__)

AUDIENCE = "papaia-audit"
BUILD_PATH = "/v1/access-reports/build"
FORMAT = "papaia.access-report/1"
KINDS = ("person", "system")
FORMATS = ("json", "csv")
MEDIA_TYPES = {"json": "application/json", "csv": "text/csv"}
TARGET_RIGHTS = "im Zielsystem geführt"
# Effects of an exchange right (architecture: r read, w write, d delete, x module interface).
EFFECTS = ("r", "w", "d", "x")
# A realm of 10 000 persons stays well below this as JSON or CSV.
MAX_ANSWER_BYTES = 32 * 1024 * 1024
MAX_PROBLEM_BYTES = 16 * 1024
MAX_PROBLEM_TEXT = 1000
# Per network operation (connect, each read) and for the whole answer: building the report
# reads every person of the realm, so it may take a while, but never forever.
TIMEOUT_SECONDS = 30.0
DEADLINE_SECONDS = 60.0

# The CSV columns of each kind, as papaia-audit writes them (docs/permission-report.md).
CSV_COLUMNS = {
    "person": (
        "key", "sub", "username", "enabled", "breakglass", "realm_roles", "module_roles",
        "groups", "service_accounts_owner", "service_accounts_deputy", "delegations",
        "separation_conflict", "digest", "last_confirmed_at", "confirmation_due",
    ),
    "system": (
        "key", "audience", "exchange_clients", "service_accounts", "target_rights", "digest",
        "last_confirmed_at", "confirmation_due",
    ),
}  # fmt: skip
_STATE_FIELDS = frozenset({"digest", "last_confirmed_at", "confirmation_due"})
_PROBLEM_CODE = re.compile(r"\A[A-Za-z0-9_.-]{1,64}\Z")

# One POST: (url, JSON body, headers) -> (status, content type, body). Raises OSError,
# ssl.SSLError or httpx.HTTPError when there is no complete answer.
Post = Callable[[str, bytes, Mapping[str, str]], tuple[int, str, bytes]]


class Exchanger(Protocol):
    """What the report needs of the kit's `ExchangeClient`."""

    def exchange(
        self, subject_token: str, audience: str, *, correlation_id: str | None = None
    ) -> ExchangedToken: ...

    def forget(self, sub: str) -> None: ...


class Refused(Exception):  # noqa: N818 - an answer of papaia-audit, not a failure here
    """papaia-audit refused the auditor (403): its own message and hint, never a token."""

    def __init__(self, message: str, hint: str, code: str | None) -> None:
        super().__init__(code or "refused")
        self.message = message
        self.hint = hint
        self.code = code


class Unavailable(Exception):  # noqa: N818 - a state of the report, not a failure here
    """No usable report. `reason` is one fixed word; `message`/`hint` are papaia-audit's
    own texts when it answered with a well-formed problem (for example 503 before the first
    realm sync), else None."""

    REASONS = ("exchange", "unreachable", "status", "answer")

    def __init__(
        self, reason: str, *, message: str | None = None, hint: str | None = None
    ) -> None:
        if reason not in self.REASONS:
            raise ValueError(reason)
        super().__init__(reason)
        self.reason = reason
        self.message = message
        self.hint = hint


@dataclass(frozen=True)
class Report:
    """One answer of papaia-audit: the bytes as received, their media type and, for JSON,
    the checked document."""

    body: bytes
    media_type: str
    document: dict[str, Any] | None

    @property
    def sha256(self) -> str:
        return "sha256:" + hashlib.sha256(self.body).hexdigest()


@cache
def schema() -> dict[str, Any]:
    """The published schema of `papaia.access-report/1` (copy of the core's
    `tools/schemas/access-report.schema.json`)."""
    path = Path(__file__).with_name("schemas") / "access-report.v1.schema.json"
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


@cache
def _validator() -> Draft202012Validator:
    Draft202012Validator.check_schema(schema())
    return Draft202012Validator(schema())


def _canonical(value: object) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(_canonical(value)).hexdigest()


def content(row: Mapping[str, Any]) -> dict[str, Any]:
    """A row without its confirmation state: what its digest covers."""
    return {k: v for k, v in row.items() if k not in _STATE_FIELDS}


def checksum(sessions: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> str:
    """The report's checksum: the session limits and the content of the sorted rows."""
    return _digest({"sessions": sessions, "rows": [content(r) for r in rows]})


def _is_time(value: object) -> bool:
    """None (never confirmed) or an ISO 8601 time with a date, as papaia-audit stores it."""
    if value is None:
        return True
    if not isinstance(value, str) or len(value) > 40:  # noqa: PLR2004
        return False
    try:
        datetime.fromisoformat(value)
    except ValueError:
        return False
    return True


def check_document(raw: bytes, by: str) -> dict[str, Any]:
    """The JSON report, or ValueError when it is not exactly a report of kind `by`."""
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        raise ValueError("not json") from None
    if not isinstance(doc, dict) or not _validator().is_valid(doc):
        raise ValueError("not a report")
    if doc["by"] != by:
        raise ValueError("other kind")
    rows: list[dict[str, Any]] = doc["rows"]
    keys = [row["key"] for row in rows]
    if keys != sorted(keys) or len(set(keys)) != len(keys):
        raise ValueError("rows not sorted")
    for row in rows:
        if not row["key"].startswith(by + ":") or row["digest"] != _digest(content(row)):
            raise ValueError("row digest")
        if not _is_time(row["last_confirmed_at"]):
            raise ValueError("confirmation time")
    if doc["checksum"] != checksum(doc["sessions"], rows):
        raise ValueError("checksum")
    return doc


def check_csv(raw: bytes, by: str) -> None:
    """ValueError unless `raw` is UTF-8 text that starts with the header of kind `by`."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError("not utf-8") from None
    header = text.split("\n", 1)[0].rstrip("\r")
    if header != ",".join(CSV_COLUMNS[by]):
        raise ValueError("other header")


def _problem(raw: bytes) -> tuple[str, str, str | None] | None:
    """Message, hint and code of the platform's error envelope, or None."""
    if len(raw) > MAX_PROBLEM_BYTES:
        return None
    try:
        doc = json.loads(raw)
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None
    if not isinstance(doc, dict):
        return None
    message, hint, code = doc.get("message"), doc.get("hint"), doc.get("code")
    texts = (message, hint)
    if not all(
        isinstance(t, str) and 0 < len(t) <= MAX_PROBLEM_TEXT and t.isprintable() for t in texts
    ):
        return None
    if not (isinstance(code, str) and _PROBLEM_CODE.match(code)):
        code = None
    return str(message), str(hint), code


def _media(content_type: str) -> str:
    return content_type.split(";", 1)[0].strip().lower()


class ReportService:
    """Builds one report per call with the auditor's own exchanged token."""

    def __init__(self, *, exchange: Exchanger, post: Post, base_url: str) -> None:
        self._exchange = exchange
        self._post = post
        self._url = base_url.rstrip("/") + BUILD_PATH

    def build(self, subject_token: str, by: str, fmt: str, correlation_id: str) -> Report:
        """The report of kind `by` in format `fmt`; raises Refused or Unavailable."""
        if by not in KINDS or fmt not in FORMATS:
            raise ValueError("unknown kind or format")
        try:
            token = self._exchange.exchange(subject_token, AUDIENCE, correlation_id=correlation_id)
        except ExchangeError as exc:
            logger.warning(
                "access report: token exchange failed (%s), correlation id %s",
                exc.reason,
                correlation_id,
            )
            raise Unavailable("exchange") from None
        try:
            status, content_type, raw = self._call(token, by, fmt, correlation_id)
        finally:
            # The exchanged token serves this one call; nothing keeps it for the next one.
            self._exchange.forget(token.sub)
            del token
        return self._answer(status, content_type, raw, by, fmt, correlation_id)

    def _call(
        self, token: ExchangedToken, by: str, fmt: str, correlation_id: str
    ) -> tuple[int, str, bytes]:
        headers = {
            **token.headers(),
            "Content-Type": "application/json",
            "Accept": f"{MEDIA_TYPES[fmt]}, application/problem+json",
        }
        body = json.dumps({"by": by, "format": fmt}).encode()
        try:
            return self._post(self._url, body, headers)
        except (OSError, ssl.SSLError, httpx.HTTPError):
            logger.warning(
                "access report: papaia-audit not reachable, correlation id %s", correlation_id
            )
            raise Unavailable("unreachable") from None

    @staticmethod
    def _answer(
        status: int, content_type: str, raw: bytes, by: str, fmt: str, correlation_id: str
    ) -> Report:
        if status == 403:  # noqa: PLR2004 - the refusal of papaia-audit
            problem = _problem(raw)
            if problem is None:
                raise Unavailable("answer")
            raise Refused(*problem)
        if status != 200:  # noqa: PLR2004
            logger.warning(
                "access report: papaia-audit answered %s, correlation id %s",
                status,
                correlation_id,
            )
            problem = _problem(raw)
            message, hint = (problem[0], problem[1]) if problem else (None, None)
            raise Unavailable("status", message=message, hint=hint)
        if _media(content_type) != MEDIA_TYPES[fmt] or len(raw) > MAX_ANSWER_BYTES:
            raise Unavailable("answer")
        try:
            if fmt == "json":
                return Report(raw, content_type, check_document(raw, by))
            check_csv(raw, by)
        except ValueError as exc:
            logger.warning(
                "access report: unusable answer of papaia-audit (%s), correlation id %s",
                exc,
                correlation_id,
            )
            raise Unavailable("answer") from None
        return Report(raw, content_type, None)


def post_with(
    client: Callable[[], httpx.Client],
    *,
    deadline_seconds: float = DEADLINE_SECONDS,
    clock: Callable[[], float] = time.monotonic,
) -> Post:
    """`POST` with a client from `client`: no redirect, size-limited, with a total deadline.

    A body over the limit or past the deadline raises OSError; neither the address, the
    headers nor the answer are logged.
    """

    def post(url: str, body: bytes, headers: Mapping[str, str]) -> tuple[int, str, bytes]:
        give_up = clock() + deadline_seconds
        with (
            client() as http,
            http.stream(
                "POST", url, content=body, headers=dict(headers), follow_redirects=False
            ) as response,
        ):
            received = bytearray()
            for chunk in response.iter_bytes():
                received.extend(chunk)
                if len(received) > MAX_ANSWER_BYTES or clock() > give_up:
                    raise OSError("answer too large or too slow")
            return response.status_code, response.headers.get("content-type", ""), bytes(received)

    return post


def mtls_post(*, ca_file: str, cert_file: str, key_file: str) -> Post:
    """`post_with` over mutual TLS with the manager's platform client certificate."""

    def client() -> httpx.Client:
        context = ssl.create_default_context(cafile=ca_file)
        context.load_cert_chain(cert_file, key_file)
        return httpx.Client(
            verify=context, timeout=TIMEOUT_SECONDS, follow_redirects=False, trust_env=False
        )

    return post_with(client)
