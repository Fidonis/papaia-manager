"""The manager's only way to Docker: papaia-docker-gate.

Whoever can talk to the Docker socket is root on the host, so the manager does not mount it.
The core runs `papaia-docker-gate` next to the manager, on an internal network that holds
the two of them and nothing else; the gate holds the socket and decides every request
before anything reaches it. It allows the status of the containers of the installation's
own compose project and `start` / `stop` of those containers, and refuses everything else
with 403 and an audit row of its own (exec, create, other projects, images, volumes, ...).

This module is the client side of that contract and nothing more:

* Input is checked here before it is sent -- a container reference must be a Docker name or
  id, a stop timeout must be in the gate's range -- so a bad value never leaves the manager.
  The gate checks the same again; it is the control, this is the early answer.
* Answers are checked for shape and cut to the fields the manager reads. Anything else in
  an entry is dropped, an entry of the wrong shape is skipped.
* A gate that does not answer is a state of its own (`GateUnavailableError`): running the manager
  without the gate is not supported, and the message says so rather than claiming that
  every service is down.
* The correlation id of the request (or of the job it queued) travels to the gate in
  `X-Papaia-Correlation-Id`, so the gate's audit row and the manager's share it.

Blocking (httpx's synchronous client): call it from a thread, as the routers do.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

import httpx

from app.core import kit_audit
from app.i18n import gettext

URL_ENV = "DOCKER_GATE_URL"
DEFAULT_URL = "http://papaia-docker-gate:2375"
CORRELATION_HEADER = "X-Papaia-Correlation-Id"
PROJECT_LABEL = "com.docker.compose.project"

# The gate's own limits (docs/docker-gate.md of the core): a Docker name or id, and a stop
# timeout of at most ten minutes.
MAX_STOP_SECONDS = 600
_CONTAINER_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
_CORRELATION_RE = re.compile(r"\A[A-Za-z0-9-]{1,64}\Z")
_ERROR_CODE_RE = re.compile(r"\A[a-z0-9_.]{1,64}\Z")
_MAX_URL = 256

_READ_TIMEOUT_SECONDS = 5.0
# A stop waits up to `t` seconds for the container to exit, and the gate adds its own
# round trip to Docker on top.
_STOP_MARGIN_SECONDS = 15.0


class GateError(Exception):
    """The gate gave no usable answer. `message` is what the operator reads."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class GateUnavailableError(GateError):
    """Nothing answers at the gate's address."""

    def __init__(self, url: str) -> None:
        super().__init__(
            gettext(
                "papaia-docker-gate does not answer at %(url)s. The manager reaches Docker "
                "only through papaia-docker-gate; running it without the gate is not "
                "supported."
            )
            % {"url": url}
        )


class GateRefusedError(GateError):
    """The gate answered with an error status; `code` is its error code, if it sent one."""

    def __init__(self, status: int, code: str) -> None:
        super().__init__(
            gettext("papaia-docker-gate refused the request (%(status)s %(code)s)")
            % {"status": status, "code": code or "-"}
        )
        self.status = status
        self.code = code


@dataclass(frozen=True)
class GateContainer:
    """One container of the gate's list, cut to what the manager reads."""

    id: str
    name: str
    state: str
    status: str
    labels: dict[str, str] = field(default_factory=dict)

    @property
    def project(self) -> str:
        return self.labels.get(PROJECT_LABEL, "")


def validate_url(value: str) -> str:
    """The gate's address as a plain `http://host[:port]` origin; ValueError otherwise.

    Plain HTTP on purpose: the gate listens on an internal network that holds only it and
    the manager, and has no certificate. No path, query or credentials: none of them has a
    meaning for the gate, and a path would put every call under a prefix it does not know.
    """
    text = value.strip()
    if not text or len(text) > _MAX_URL:
        raise ValueError(f"{URL_ENV} is empty or too long")
    parts = urlsplit(text)
    try:
        port = parts.port
    except ValueError as exc:
        raise ValueError(f"{URL_ENV} has an invalid port") from exc
    if parts.scheme != "http" or not parts.hostname:
        raise ValueError(f"{URL_ENV} must be an http://host[:port] address")
    if parts.username is not None or parts.password is not None:
        raise ValueError(f"{URL_ENV} must not carry credentials")
    if parts.path not in ("", "/") or parts.query or parts.fragment:
        raise ValueError(f"{URL_ENV} must not carry a path, query or fragment")
    host = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
    return f"http://{host}" + (f":{port}" if port is not None else "")


def gate_url() -> str:
    """The configured address (`DOCKER_GATE_URL`), or the gate on its internal network."""
    return validate_url(os.environ.get(URL_ENV) or DEFAULT_URL)


def _container_ref(value: str) -> str:
    if not isinstance(value, str) or not _CONTAINER_RE.match(value):
        raise ValueError("not a Docker container name or id")
    return value


def _correlation(value: str | None) -> str | None:
    """The id to send: the given one, else the one of the request or job in progress.

    One that does not fit the gate's pattern is not sent; the gate then assigns its own,
    which is better than a header the gate would have to discard.
    """
    candidate = value if value is not None else kit_audit.current_binding().correlation_id
    if candidate and _CORRELATION_RE.match(candidate):
        return candidate
    return None


def _str_labels(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {k: v for k, v in raw.items() if isinstance(k, str) and isinstance(v, str)}


def _listed(entry: object) -> GateContainer | None:
    if not isinstance(entry, dict):
        return None
    names = entry.get("Names")
    cid, state, status = entry.get("Id"), entry.get("State"), entry.get("Status")
    if not (isinstance(names, list) and names and isinstance(names[0], str)):
        return None
    if not all(isinstance(v, str) for v in (cid, state, status)):
        return None
    name = names[0].removeprefix("/")
    if not _CONTAINER_RE.match(name):
        return None
    return GateContainer(
        id=str(cid),
        name=name,
        state=str(state),
        status=str(status),
        labels=_str_labels(entry.get("Labels")),
    )


class GateClient:
    """Calls to one gate. `transport` is httpx's, for a gate reached some other way."""

    def __init__(self, url: str, *, transport: httpx.BaseTransport | None = None) -> None:
        self.url = validate_url(url)
        self._transport = transport

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        correlation_id: str | None = None,
        timeout: float = _READ_TIMEOUT_SECONDS,
    ) -> httpx.Response:
        headers = {}
        correlation = _correlation(correlation_id)
        if correlation:
            headers[CORRELATION_HEADER] = correlation
        try:
            with httpx.Client(
                base_url=self.url, transport=self._transport, timeout=timeout,
                follow_redirects=False, trust_env=False,
            ) as client:
                response = client.request(method, path, params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise GateUnavailableError(self.url) from exc
        if response.status_code >= 400:
            raise GateRefusedError(response.status_code, _error_code(response))
        return response

    def ping(self) -> None:
        self._request("GET", "/_ping")

    def list_containers(self) -> list[GateContainer]:
        """Every container the gate shows, stopped ones included (`all=1`)."""
        response = self._request("GET", "/containers/json", params={"all": "1"})
        try:
            document: Any = response.json()
        except ValueError as exc:
            raise GateError(gettext("papaia-docker-gate sent an unreadable answer")) from exc
        if not isinstance(document, list):
            raise GateError(gettext("papaia-docker-gate sent an unreadable answer"))
        return [c for c in (_listed(e) for e in document) if c is not None]

    def start_container(self, ref: str, *, correlation_id: str | None = None) -> bool:
        """Start one container; False when it was running already."""
        path = f"/containers/{_container_ref(ref)}/start"
        response = self._request("POST", path, correlation_id=correlation_id)
        return response.status_code != 304

    def stop_container(
        self, ref: str, *, timeout: int = 10, correlation_id: str | None = None
    ) -> bool:
        """Stop one container, waiting up to `timeout` seconds; False when it was stopped."""
        path = f"/containers/{_container_ref(ref)}/stop"
        if isinstance(timeout, bool) or not isinstance(timeout, int):
            raise ValueError("the stop timeout must be a whole number of seconds")
        if not 0 <= timeout <= MAX_STOP_SECONDS:
            raise ValueError(f"the stop timeout must be between 0 and {MAX_STOP_SECONDS}")
        response = self._request(
            "POST", path, params={"t": str(timeout)}, correlation_id=correlation_id,
            timeout=timeout + _STOP_MARGIN_SECONDS,
        )
        return response.status_code != 304


def _error_code(response: httpx.Response) -> str:
    """The gate's `{"error": "<code>"}`, if the answer carries a well-formed one."""
    try:
        body = response.json()
    except ValueError:
        return ""
    code = body.get("error") if isinstance(body, dict) else None
    return code if isinstance(code, str) and _ERROR_CODE_RE.match(code) else ""


def default_client() -> GateClient:
    """The client of the configured gate."""
    return GateClient(gate_url())
