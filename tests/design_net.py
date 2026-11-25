"""The network of the stand-in world: every service on one stand-in, nothing beyond 127.0.0.1.

The manager of tests/design_serve.py would otherwise ask the name service and the network of
whatever host runs it for `qdrant`, `qdrant-ingest`, `litellm` and the other services of the
core, and how fast the page's "unreachable" state arrives would depend on that host. Here:

* `stand_in()` listens on 127.0.0.1 and closes every connection at once without a word, which
  every client of the manager reports as its service being unreachable -- the state the pages
  show when the service is not there.
* `point_services_at()` sets every address of a service the manager calls (SERVICES) to that
  stand-in.
* `guard()` makes the process refuse, at once, every name lookup and every connection to
  anything but the loopback, and writes each refused one as a line of JSON to a log (the file
  named by PAPAIA_DESIGN_NET_LOG in design_serve.py). The log is created empty, so an empty
  file means: nothing tried to leave the host.

The guard works on Python's socket layer, so the server has to run on the asyncio event loop
(uvloop resolves and connects in C, past it). A test tool, never part of the image.
"""

from __future__ import annotations

import errno
import ipaddress
import json
import socket
import struct
import threading
import time
from collections.abc import Callable, MutableMapping
from pathlib import Path
from typing import Any

# The settings of the manager that name a service it calls, with the path each takes after
# the stand-in's origin ("" for a base URL), and whether the client speaks TLS.
SERVICES: dict[str, tuple[str, str]] = {
    "QDRANT_URL": ("http", ""),
    "QDRANT_INGEST_URL": ("http", ""),
    "KNOWLEDGE_MAP_URL": ("http", ""),
    "LITELLM_URL": ("http", ""),
    "PREFS_URL": ("http", ""),
    "AUDIT_INGEST_URL": ("https", "/v1/ingest"),
    "AUDIT_READ_URL": ("https", ""),
    "OIDC_ISSUER_KC_TOKEN": ("https", "/realms/papaia/protocol/openid-connect/token"),
    "OIDC_ISSUER_KC_CERTS": ("https", "/certs"),
}
# Answered in the process by the fake ingester of design_ingest.py (an httpx transport that
# tells its fakes apart by these names): no lookup and no connection, so they keep their names.
WITH_INGEST_KEEPS = ("QDRANT_URL", "QDRANT_INGEST_URL")

_LOCAL_NAMES = frozenset({"localhost", "localhost.", ""})


def is_loopback(host: object) -> bool:
    """Whether `host` (a name or an address, str or bytes) stays on this machine."""
    if host is None:
        return True  # a lookup for binding, not for reaching anyone
    text = host.decode() if isinstance(host, bytes) else str(host)
    text = text.strip("[]").split("%", 1)[0]
    if text.lower() in _LOCAL_NAMES:
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _is_address(host: object) -> bool:
    text = host.decode() if isinstance(host, bytes) else str(host)
    try:
        ipaddress.ip_address(text.strip("[]").split("%", 1)[0])
    except ValueError:
        return False
    return True


class StandIn:
    """A listener on 127.0.0.1 that closes every connection at once (a reset, no answer)."""

    def __init__(self) -> None:
        self._listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._listener.bind(("127.0.0.1", 0))
        self._listener.listen(64)
        self.port: int = self._listener.getsockname()[1]
        self.accepted = 0
        self._thread = threading.Thread(target=self._serve, name="design-stand-in", daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._listener.accept()
            except OSError:
                return
            self.accepted += 1
            # Linger 0: the close is a reset, so the client fails now instead of waiting.
            conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
            conn.close()

    def close(self) -> None:
        self._listener.close()


def stand_in() -> StandIn:
    return StandIn()


def point_services_at(
    environ: MutableMapping[str, str], port: int, *, with_ingest: bool = False
) -> None:
    """Set every service address of SERVICES in `environ` to the stand-in on `port`."""
    for name, (scheme, path) in SERVICES.items():
        if with_ingest and name in WITH_INGEST_KEEPS:
            continue
        environ[name] = f"{scheme}://127.0.0.1:{port}{path}"
    # The inventory of other modules: none to ask.
    environ["INVENTORY_SOURCES"] = ""


class Guard:
    """Refuses lookups and connections beyond the loopback and logs each one."""

    def __init__(self, log: Path) -> None:
        self.log = log
        self._lock = threading.Lock()
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text("", encoding="utf-8")

    def refuse(self, call: str, target: object) -> None:
        line = json.dumps({"time": round(time.time(), 3), "call": call, "target": str(target)})
        with self._lock, self.log.open("a", encoding="utf-8") as out:
            out.write(line + "\n")


_installed: Guard | None = None


def guard(log: Path) -> Guard:
    """Install the guard in this process (once); the log starts empty."""
    global _installed  # noqa: PLW0603 -- one guard per process, like the socket module
    if _installed is not None:
        return _installed
    keeper = Guard(log)
    real_getaddrinfo: Callable[..., Any] = socket.getaddrinfo
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        # An address needs no name service; where it may go is decided when connecting.
        if not is_loopback(host) and not _is_address(host):
            keeper.refuse("getaddrinfo", host)
            raise socket.gaierror(socket.EAI_NONAME, "refused by the stand-in world")
        return real_getaddrinfo(host, *args, **kwargs)

    def _target(sock: socket.socket, address: Any) -> str | None:
        """The host of `address` if it leaves the machine, else None."""
        if sock.family not in (socket.AF_INET, socket.AF_INET6):
            return None  # a Unix socket or another local family
        host = address[0] if isinstance(address, tuple) else address
        return None if is_loopback(host) else f"{host}:{address[1]}"

    def connect(self: socket.socket, address: Any) -> None:
        target = _target(self, address)
        if target is not None:
            keeper.refuse("connect", target)
            raise ConnectionRefusedError(errno.ECONNREFUSED, "refused by the stand-in world")
        real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        target = _target(self, address)
        if target is not None:
            keeper.refuse("connect", target)
            return errno.ECONNREFUSED
        return real_connect_ex(self, address)

    socket.getaddrinfo = getaddrinfo
    socket.socket.connect = connect  # type: ignore[method-assign]
    socket.socket.connect_ex = connect_ex  # type: ignore[method-assign]
    _installed = keeper
    return keeper


def refused(log: Path) -> list[dict[str, Any]]:
    """The refused lookups and connections of a guard's log."""
    text = log.read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.strip()]
