"""Requests for information and erasure of a person without an account (security 1.14 no. 2b).

The operator files a request on the host (`papaia-ctl person export|erase`); the core's
`papaia-audit` keeps it and names two persons: the one who filed it and the one who confirms
it. The page "Data subject requests" of the manager (`app.routers.ui_person_requests`) is
where the second person confirms or rejects it. This module is the manager's side of the
three routes of `papaia-audit`:

    GET  /v1/person-requests                       the requests of the last 7 days naming me
    POST /v1/person-requests/<id>/confirm          confirm (a sign-in of at most 5 minutes ago)
    POST /v1/person-requests/<id>/reject           reject

Every call carries the signed-in person's own token, exchanged for the audience
`papaia-audit` with the manager's client (RFC 8693), and goes over mutual TLS with the
manager's platform client certificate (the one of the audit rows): `papaia-audit` answers
these routes only to the caller `papaia-manager` and decides with the person's roles and
`auth_time`. The manager holds no right of its own there.

Confirming an export answers a passphrase once. It is handed to the page that shows it and
nowhere else: never stored, logged or written into an audit row.

Answers are checked field by field; an answer with another shape counts as an unavailable
service, so the page never shows what it cannot vouch for.
"""
from __future__ import annotations

import json
import re
import ssl
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

import httpx
from papaia_kit.correlation import CORRELATION_HEADER
from papaia_kit.exchange import ClientSecret, ExchangeClient, ExchangeError

if TYPE_CHECKING:
    from app.config import Settings

AUDIENCE = "papaia-audit"
LIST_PATH = "/v1/person-requests"
TIMEOUT = 10.0
# papaia-audit answers at most 200 requests; anything longer is not its answer.
LIST_MAX = 200
MAX_BODY = 512 * 1024

Decision = Literal["confirm", "reject"]
DECISIONS: tuple[Decision, ...] = ("confirm", "reject")
OPERATIONS = frozenset({"export", "erase"})
STATES = frozenset({"open", "confirmed", "rejected", "running", "done", "partial", "expired"})

# The request id papaia-audit issues: a UUID version 4, lower case.
ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}")
# A person as papaia-audit shows her to the manager: `P-` and 8 characters of base32.
PSEUDONYM = re.compile(r"P-[A-Z2-7]{8}")
# Time stamps of papaia-audit: UTC with milliseconds.
TIME = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")
# 8 groups of 5 characters of Crockford's base32 (200 bits).
PASSPHRASE = re.compile(r"[0-9A-HJKMNP-TV-Z]{5}(?:-[0-9A-HJKMNP-TV-Z]{5}){7}")
_CODE = re.compile(r"[A-Z][A-Z0-9-]{0,63}")

_FIELDS: dict[str, type | tuple[type, ...]] = {
    "id": str,
    "operation": str,
    "state": str,
    "requested_at": str,
    "decided_at": (str, type(None)),
    "person": str,
    "requested_by": str,
    "confirmed_by": str,
    "transfer": bool,
    "own": bool,
    "can_confirm": bool,
}
_DECIDED_FIELDS = frozenset(_FIELDS) | {"passphrase"}

# What a refusal of papaia-audit means for the page; the code is papaia-audit's own.
Reason = Literal[
    "not_fresh", "own_request", "no_role", "not_found", "wrong_state", "unauthenticated",
    "unavailable",
]  # fmt: skip
_REASONS: dict[str, Reason] = {
    "AUDIT-AUTH-FRESH": "not_fresh",
    "AUDIT-PERSON-REQ-SELF": "own_request",
    "AUDIT-PERSON-REQ-403": "no_role",
    "AUDIT-PERSON-REQ-409": "wrong_state",
}
_BY_STATUS: dict[int, Reason] = {401: "unauthenticated", 404: "not_found", 409: "wrong_state"}


class PersonRequestError(Exception):
    """papaia-audit refused, could not be reached or gave an answer of another shape."""

    def __init__(self, reason: Reason, code: str | None = None) -> None:
        super().__init__(reason)
        self.reason: Reason = reason
        self.code = code


@dataclass(frozen=True, slots=True)
class PersonRequest:
    """One request as the confirming or the requesting person sees it (pseudonyms only)."""

    id: str
    operation: str
    state: str
    requested_at: str
    decided_at: str | None
    person: str
    requested_by: str
    confirmed_by: str
    transfer: bool
    own: bool
    can_confirm: bool


@dataclass(frozen=True, slots=True)
class Decided:
    """The answer to a decision; `passphrase` only for a confirmed export, shown once."""

    request: PersonRequest
    passphrase: str | None


# method, url, headers -> (status, body)
Send = Callable[[str, str, Mapping[str, str]], tuple[int, bytes]]
# person's access token, correlation id -> token for the audience papaia-audit
Exchange = Callable[[str, str], str]


@dataclass(frozen=True, slots=True)
class Wiring:
    """How the manager reaches papaia-audit: the base URL, the transport and the exchange."""

    base_url: str
    send: Send
    exchange: Exchange

    @classmethod
    def from_settings(cls, settings: Settings) -> Wiring:
        client = ExchangeClient(
            token_endpoint=settings.oidc_issuer_kc_token,
            client_id=settings.manager_oidc_client_id,
            auth=ClientSecret(settings.manager_oidc_client_secret),
            timeout=TIMEOUT,
        )

        def exchange(access_token: str, cid: str) -> str:
            return client.exchange(access_token, AUDIENCE, correlation_id=cid).access_token

        return cls(
            base_url=settings.audit_read_url,
            send=mtls_send(
                ca_file=settings.audit_tls_ca_file,
                cert_file=settings.audit_tls_cert_file,
                key_file=settings.audit_tls_key_file,
            ),
            exchange=exchange,
        )


def mtls_send(*, ca_file: str, cert_file: str, key_file: str) -> Send:
    """A transport over mutual TLS with the manager's platform client certificate."""

    def send(method: str, url: str, headers: Mapping[str, str]) -> tuple[int, bytes]:
        context = ssl.create_default_context(cafile=ca_file)
        context.load_cert_chain(cert_file, key_file)
        with httpx.Client(verify=context, timeout=TIMEOUT, follow_redirects=False) as client:
            response = client.request(method, url, headers=dict(headers))
            return response.status_code, response.content[: MAX_BODY + 1]

    return send


_wiring: Wiring | None = None
_lock = threading.Lock()


def use(wiring: Wiring | None) -> None:
    """Use this wiring from now on; None builds it from the settings on the next call."""
    global _wiring  # noqa: PLW0603
    with _lock:
        _wiring = wiring


def _clients(settings: Settings) -> Wiring:
    global _wiring  # noqa: PLW0603
    with _lock:
        if _wiring is None:
            _wiring = Wiring.from_settings(settings)
        return _wiring


# -- answers ---------------------------------------------------------------------------------


def _request(raw: object, *, allowed: frozenset[str] = frozenset(_FIELDS)) -> PersonRequest:
    if not isinstance(raw, dict) or not set(_FIELDS) <= set(raw) or set(raw) - allowed:
        raise PersonRequestError("unavailable")
    for name, kind in _FIELDS.items():
        if not isinstance(raw[name], kind):
            raise PersonRequestError("unavailable")
    valid = (
        ID.fullmatch(raw["id"])
        and raw["operation"] in OPERATIONS
        and raw["state"] in STATES
        and TIME.fullmatch(raw["requested_at"])
        and (raw["decided_at"] is None or TIME.fullmatch(raw["decided_at"]))
        and all(PSEUDONYM.fullmatch(raw[k]) for k in ("person", "requested_by", "confirmed_by"))
    )
    if not valid:
        raise PersonRequestError("unavailable")
    return PersonRequest(**{name: raw[name] for name in _FIELDS})


def parse_list(body: bytes) -> list[PersonRequest]:
    """The answer of `GET /v1/person-requests`: exactly `{"requests": [...]}`."""
    document = _json(body)
    if not isinstance(document, dict) or set(document) != {"requests"}:
        raise PersonRequestError("unavailable")
    items = document["requests"]
    if not isinstance(items, list) or len(items) > LIST_MAX:
        raise PersonRequestError("unavailable")
    return [_request(item) for item in items]


def parse_decided(body: bytes, decision: Decision, request_id: str) -> Decided:
    """The answer of a decision; a passphrase only where one belongs, in its one shape."""
    document = _json(body)
    request = _request(document, allowed=_DECIDED_FIELDS)
    assert isinstance(document, dict)
    passphrase = document.get("passphrase")
    expected = "confirmed" if decision == "confirm" else "rejected"
    if request.id != request_id or request.state != expected:
        raise PersonRequestError("unavailable")
    wants_passphrase = decision == "confirm" and request.operation == "export"
    if wants_passphrase != (passphrase is not None):
        raise PersonRequestError("unavailable")
    if passphrase is not None and (
        not isinstance(passphrase, str) or not PASSPHRASE.fullmatch(passphrase)
    ):
        raise PersonRequestError("unavailable")
    return Decided(request=request, passphrase=passphrase)


def _json(body: bytes) -> Any:
    if len(body) > MAX_BODY:
        raise PersonRequestError("unavailable")
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise PersonRequestError("unavailable") from None


def _refusal(status: int, body: bytes) -> PersonRequestError:
    """papaia-audit's error envelope as a reason; only its code is kept, never its text."""
    code: str | None = None
    try:
        document = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        document = None
    if isinstance(document, dict):
        value = document.get("code")
        if isinstance(value, str) and _CODE.fullmatch(value):
            code = value
    reason = _REASONS.get(code or "") or _BY_STATUS.get(status, "unavailable")
    if status == 403 and reason not in ("own_request", "no_role"):
        reason = "no_role"
    return PersonRequestError(reason, code)


# -- calls -----------------------------------------------------------------------------------


def _call(
    settings: Settings, method: str, path: str, access_token: str, cid: str
) -> tuple[int, bytes]:
    wiring = _clients(settings)
    try:
        token = wiring.exchange(access_token, cid)
    except ExchangeError:
        raise PersonRequestError("unauthenticated") from None
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        CORRELATION_HEADER: cid,
    }
    try:
        status, body = wiring.send(method, wiring.base_url + path, headers)
    except (httpx.HTTPError, OSError, ssl.SSLError):
        raise PersonRequestError("unavailable") from None
    if status != 200:
        raise _refusal(status, body)
    return status, body


def list_requests(settings: Settings, access_token: str, cid: str) -> list[PersonRequest]:
    """The requests of the last 7 days that name the signed-in person, as listed there."""
    _, body = _call(settings, "GET", LIST_PATH, access_token, cid)
    return parse_list(body)


def decide(
    settings: Settings, request_id: str, decision: Decision, access_token: str, cid: str
) -> Decided:
    """Confirm or reject one request in the name of the signed-in person."""
    if not ID.fullmatch(request_id) or decision not in DECISIONS:
        raise PersonRequestError("not_found")
    _, body = _call(
        settings, "POST", f"{LIST_PATH}/{request_id}/{decision}", access_token, cid
    )
    return parse_decided(body, decision, request_id)
