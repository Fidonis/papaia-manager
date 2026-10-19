"""How the relay is doing, as the core's `doctor` reports it.

The core's `smtp` check reads the relay's container state and the number of messages
in its queue, and, on the host, also talks to the mail server behind it. Inside this
container it does not talk to the mail server (a poll would reach the provider every
time), which is why the page offers "Test connection" for that.

The host health poll skips this check (`host_health._skipped`): it runs `docker exec`
into the relay, and only this page has a use for the answer. So it is measured here, in
a `doctor` of its own with every other check skipped, behind a short cache, and shared
between concurrent callers. It never raises: an unreadable answer is a reading with a
reason.

The core may not have the check. It is looked for in the core's own registry before it
is asked for, because `doctor --skip` refuses a name it does not know.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.core.ctl import CtlError, run_py_cli
from app.core.docker_usage import core_checks

logger = logging.getLogger(__name__)

CHECK_NAME = "smtp"

STATUS_TTL_SECONDS = 10.0
STATUS_RECHECK_MIN_SECONDS = 3.0
STATUS_RUN_TIMEOUT_SECONDS = 30.0

_SUPPORTED_SCHEMA = 1


@dataclass(frozen=True)
class RelayStatus:
    """One reading of the relay. `verdict` is the core's: pass, warn, fail or skip."""

    available: bool
    reason: str = ""
    verdict: str = ""
    summary: str = ""
    relay: str = ""
    queued: int | None = None
    checked_at: datetime | None = None

    def as_json(self) -> dict[str, Any]:
        return {
            "available": self.available,
            "reason": self.reason,
            "verdict": self.verdict,
            "summary": self.summary,
            "relay": self.relay,
            "queued": self.queued,
            "checked_at": self.checked_at.isoformat() if self.checked_at else None,
        }


def unavailable(reason: str) -> RelayStatus:
    return RelayStatus(available=False, reason=reason)


def core_has_check(workspace_dir: str) -> bool:
    return CHECK_NAME in core_checks(workspace_dir)


def _first_line(text: str, limit: int = 200) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""


def parse_status(stdout: str, *, checked_at: datetime | None = None) -> RelayStatus:
    """Turn `doctor --json` output into a `RelayStatus`. Never raises. Takes the `smtp`
    check only, whatever else the document carries."""
    try:
        document = json.loads(stdout)
    except ValueError:
        return unavailable("doctor did not return JSON")
    if not isinstance(document, dict):
        return unavailable("doctor did not return a JSON object")
    if document.get("schema_version") != _SUPPORTED_SCHEMA:
        return unavailable(f"doctor reports schema {document.get('schema_version')!r}, not 1")
    checks = document.get("checks")
    check = next(
        (
            c
            for c in (checks if isinstance(checks, list) else [])
            if isinstance(c, dict) and c.get("name") == CHECK_NAME
        ),
        None,
    )
    if check is None:
        return unavailable("doctor did not report the smtp check")
    summary = check.get("summary")
    details = check.get("details")
    details = details if isinstance(details, dict) else {}
    relay = details.get("relay")
    queued = details.get("queued")
    return RelayStatus(
        available=True,
        verdict=str(check.get("status") or ""),
        summary=summary.strip() if isinstance(summary, str) else "",
        relay=relay if isinstance(relay, str) else "",
        queued=queued if isinstance(queued, int) and not isinstance(queued, bool) else None,
        checked_at=checked_at or datetime.now(UTC),
    )


async def _run(config_dir: str, workspace_dir: str) -> RelayStatus:
    names = core_checks(workspace_dir)
    if CHECK_NAME not in names:
        return unavailable("this papAIa core has no smtp check")
    try:
        code, out, err = await run_py_cli(
            command="doctor",
            workspace_dir=workspace_dir,
            config_dir=config_dir,
            # Everything but the one check, so this `doctor` does nothing else. Exit 2 is
            # a check that failed and still prints the whole document.
            extra_flags=["--json", f"--skip={','.join(n for n in names if n != CHECK_NAME)}"],
            limit=STATUS_RUN_TIMEOUT_SECONDS,
        )
    except CtlError as exc:
        logger.warning("mail relay status: %s", exc)
        return unavailable(str(exc))
    if not out.strip():
        return unavailable(_first_line(err) or f"doctor exited with status {code} and no output")
    return parse_status(out)


@dataclass(frozen=True)
class _Entry:
    at: float
    status: RelayStatus


_cache: _Entry | None = None
_refresh: asyncio.Task[RelayStatus] | None = None


def reset_cache() -> None:
    """Forget the last reading and any run under way."""
    global _cache, _refresh  # noqa: PLW0603
    _cache = None
    _refresh = None


async def _measure(config_dir: str, workspace_dir: str) -> RelayStatus:
    """One run, stored. Never raises: a task nobody awaits would lose the error."""
    global _cache  # noqa: PLW0603
    try:
        status = await _run(config_dir, workspace_dir)
    except Exception:
        logger.exception("mail relay status: unexpected failure")
        status = unavailable("the relay check failed unexpectedly")
    _cache = _Entry(at=time.monotonic(), status=status)
    return status


async def load_status(*, config_dir: str, workspace_dir: str, force: bool = False) -> RelayStatus:
    """The relay's status. A reading younger than the TTL is served as it stands; `force`
    (the "Re-check" button) shortens that to a few seconds, so it cannot be used to hammer
    `docker exec`. Concurrent callers share one run, which belongs to the process and not
    to the request that started it."""
    global _refresh  # noqa: PLW0603
    limit = STATUS_RECHECK_MIN_SECONDS if force else STATUS_TTL_SECONDS
    if _cache is not None and time.monotonic() - _cache.at < limit:
        return _cache.status
    if _refresh is None or _refresh.done():
        _refresh = asyncio.get_running_loop().create_task(_measure(config_dir, workspace_dir))
    # Shielded: a visitor who leaves must not cancel the run for the next.
    return await asyncio.shield(_refresh)
