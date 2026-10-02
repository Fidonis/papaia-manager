"""Host health: disk space and certificate expiry, as the core's `doctor` reports them.

The Services page answers whether the stack is up. This module answers whether
the machine under it is in shape, and it does so without measuring anything
itself: `papaia-ctl doctor` already knows which filesystems matter, where the
certificates live and what counts as too little room, so the manager runs it and
reads the verdict. A shell on the host and this panel therefore cannot disagree
about the same installation, and no threshold is declared twice.

Four decisions here are load-bearing:

* Only `disk_space` and `certs` are used. `doctor` has five more checks, and
  they fork `docker`, resolve names and probe ports -- fine once, wrong on a
  poll. The rest are skipped by name, and the answer is filtered to the two that
  were asked for, so a check the core adds later is ignored rather than shown
  unreviewed.
* The core's verdict is taken as is. Its disk thresholds are free bytes, not a
  percentage, and the percentage shown here is arithmetic for the eye only: it
  never decides a colour.
* A location the container cannot see is reported as not measured, never as
  empty. The Docker data root is the usual one -- the manager mounts the config
  and backup directories, not `/var/lib/docker`, and `doctor` says so itself.
* Not knowing is not the same as knowing it is bad. A core without `doctor`, a
  run that timed out and an answer that does not parse all come back as
  `available=False` with a reason, and every consumer leaves them out of the
  verdict rather than painting the chip red over a missing probe.

Reading is cheap and forking is not, so the two are separate. Pages that render
on every navigation (the status row, the sidebar dot) only look at the cache and
ask for a refresh in the background; the Host page itself awaits one. At most one
`doctor` runs at a time, and a finished run is reused for a minute, however many
people have a tab open.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any

from app.core.ctl import CtlError, run_py_cli
from app.core.services import ServiceHealth

logger = logging.getLogger(__name__)

# The doctor checks this module reads, and the ones it asks the core not to run.
# Together they are the core's whole registry at 1.4.0. `--skip` refuses a name it
# does not know, so the list is also what fails loudly if a check is renamed.
_WANTED_CHECKS = ("disk_space", "certs")
_SKIPPED_CHECKS = ("docker_version", "ports", "dns", "addon_compat", "container_health")

# Version of the JSON document `doctor --json` emits. A different one is
# somebody else's contract, not an older spelling of this one.
_SUPPORTED_SCHEMA = 1

# A finished run is reused this long. `doctor` is documented as not meant for
# tight polling, and the status row asks every 30 s from every open tab.
CACHE_TTL_SECONDS = 60.0

# A failed run is remembered for less, so a transient error clears itself soon
# without turning every poll into a retry.
FAILURE_TTL_SECONDS = 15.0

# "Re-check" bypasses the TTL but not this: a double click, or two administrators
# pressing it together, share one run.
RECHECK_MIN_INTERVAL_SECONDS = 5.0

# Generous against the core's own budget (`docker info` 10 s, `openssl` 10 s per
# certificate), short enough that a hung child is not left holding the lock.
RUN_TIMEOUT_SECONDS = 30.0

# Past this, a cached reading is no longer shown by the cache-only consumers. The
# chip refreshes it in the background on every poll, so this only matters when
# nobody has had a page open for a while.
STALE_AFTER_SECONDS = 180.0

_TOO_OLD = "This papAIa core does not provide doctor yet. Upgrade the core to 1.4.0 or newer."

_DISK_LABELS = {
    "config_dir": "Config disk",
    "backup_dir": "Backup disk",
    "docker_root": "Docker data",
}

# Where the core keeps the certificates it reads, relative to the config
# directory: the bundled ones, and Nginx Proxy Manager's Let's Encrypt live tree.
_BUNDLED_DIR = PurePosixPath("certs")
_LETSENCRYPT_LIVE = PurePosixPath("infra/nginx/nginx-letsencrypt/live")


class HostState(StrEnum):
    """What the core said about one check, in this panel's words."""

    OK = "ok"
    WARN = "warn"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


# Lower is worse. `UNKNOWN` ranks below `OK` on purpose: a certificate that could
# not be read is not a reason to call the host healthy, but it is not a warning
# either, and `HostHealth.overall` ignores it when anything else has a verdict.
_SEVERITY: dict[HostState, int] = {
    HostState.CRITICAL: 0,
    HostState.WARN: 1,
    HostState.UNKNOWN: 2,
    HostState.OK: 3,
}

# doctor's vocabulary is pass / warn / fail / skip.
_STATE_BY_DOCTOR: dict[str, HostState] = {
    "pass": HostState.OK,
    "warn": HostState.WARN,
    "fail": HostState.CRITICAL,
    "skip": HostState.UNKNOWN,
}


def format_bytes(value: int) -> str:
    """`189 GiB`, `9.0 GiB`, `512 MiB`: binary units, three significant digits."""
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024:
            return f"{size:.0f} {unit}" if unit == "B" or size >= 100 else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.0f} TiB" if size >= 100 else f"{size:.1f} TiB"


@dataclass(frozen=True)
class HostCheck:
    """One measured thing: a filesystem or a certificate.

    `detail` is what the row prints under its label -- the path of a filesystem,
    the issuer of a certificate. Both are shown to administrators only.
    """

    kind: str
    key: str
    label: str
    detail: str
    state: HostState
    free_bytes: int | None = None
    total_bytes: int | None = None
    days_left: int | None = None
    not_after: str = ""
    reason: str = ""

    @property
    def used_percent(self) -> float | None:
        """Share of the filesystem that is not free. For the eye, never a verdict.

        `free` is what an unprivileged process may still use, so this reads a
        little higher than `df` on a filesystem with reserved blocks.
        """
        if self.free_bytes is None or not self.total_bytes:
            return None
        used = 100.0 * (1.0 - self.free_bytes / self.total_bytes)
        return round(min(max(used, 0.0), 100.0), 1)

    @property
    def free_text(self) -> str:
        return format_bytes(self.free_bytes) if self.free_bytes is not None else ""

    @property
    def total_text(self) -> str:
        return format_bytes(self.total_bytes) if self.total_bytes is not None else ""

    @property
    def expires_on(self) -> str:
        """`14 Oct 2026`, or empty when the core gave no usable date."""
        try:
            moment = datetime.strptime(self.not_after, "%Y-%m-%dT%H:%M:%SZ")  # noqa: DTZ007
        except ValueError:
            return ""
        return f"{moment.day} {moment:%b %Y}"


@dataclass(frozen=True)
class HostHealth:
    """One reading of the host, or the reason there is none."""

    available: bool
    reason: str = ""
    disks: tuple[HostCheck, ...] = ()
    certs: tuple[HostCheck, ...] = ()
    notes: tuple[str, ...] = ()
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def checks(self) -> tuple[HostCheck, ...]:
        return self.disks + self.certs

    def count(self, state: HostState) -> int:
        return sum(1 for c in self.checks if c.state is state)

    @property
    def total(self) -> int:
        """Checks that have a verdict. An unreadable certificate has none."""
        return len(self.checks) - self.count(HostState.UNKNOWN)

    @property
    def ok_count(self) -> int:
        return self.count(HostState.OK)

    @property
    def warn_count(self) -> int:
        return self.count(HostState.WARN)

    @property
    def critical_count(self) -> int:
        return self.count(HostState.CRITICAL)

    @property
    def issue_count(self) -> int:
        return self.warn_count + self.critical_count

    @property
    def docker_root_measured(self) -> bool:
        return any(c.key == "docker_root" for c in self.disks)

    @property
    def overall(self) -> HostState:
        """The worst verdict, or `UNKNOWN` when there is none to judge by."""
        judged = [c.state for c in self.checks if c.state is not HostState.UNKNOWN]
        if not judged:
            return HostState.UNKNOWN
        return min(judged, key=_SEVERITY.__getitem__)

    @property
    def severity(self) -> ServiceHealth | None:
        """The same verdict on the scale the status row's `worst()` walks.

        Used for ranking only. `None` means "leave me out": nothing was judged,
        and an installation without `doctor` must not read as "Status unknown".
        """
        if not self.available:
            return None
        return {
            HostState.CRITICAL: ServiceHealth.STOPPED,
            HostState.WARN: ServiceHealth.UNHEALTHY,
            HostState.OK: ServiceHealth.HEALTHY,
            HostState.UNKNOWN: None,
        }[self.overall]


def unavailable(reason: str) -> HostHealth:
    return HostHealth(available=False, reason=reason)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _int(value: Any) -> int | None:
    # bool is an int subclass; a JSON `true` is not a byte count.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _state(value: Any) -> HostState:
    if isinstance(value, str):
        return _STATE_BY_DOCTOR.get(value, HostState.UNKNOWN)
    return HostState.UNKNOWN


def _parse_disk(entry: Any) -> HostCheck | None:
    if not isinstance(entry, dict):
        return None
    key = entry.get("label")
    if not isinstance(key, str) or not key:
        return None
    free, total = _int(entry.get("free_bytes")), _int(entry.get("total_bytes"))
    return HostCheck(
        kind="disk",
        key=key,
        label=_DISK_LABELS.get(key, key),
        detail=str(entry.get("path", "")),
        state=_state(entry.get("status")),
        free_bytes=free,
        total_bytes=total,
    )


def _certificate_identity(path: str) -> tuple[str, str]:
    """(label, detail) for a certificate path relative to the config directory."""
    where = PurePosixPath(path)
    try:
        return where.relative_to(_LETSENCRYPT_LIVE).parts[0], "Let's Encrypt"
    except (ValueError, IndexError):
        pass
    if where.parent == _BUNDLED_DIR:
        return where.name, "Bundled"
    return path, ""


def _parse_cert(entry: Any) -> HostCheck | None:
    if not isinstance(entry, dict):
        return None
    path = entry.get("path")
    if not isinstance(path, str) or not path:
        return None
    label, detail = _certificate_identity(path)
    not_after = entry.get("not_after")
    return HostCheck(
        kind="cert",
        key=path,
        label=label,
        detail=detail,
        state=_state(entry.get("status")),
        days_left=_int(entry.get("days_left")),
        not_after=not_after if isinstance(not_after, str) else "",
        reason=str(entry.get("reason", "")),
    )


def _rows(check: dict[str, Any] | None, field_name: str, parse: Any) -> list[HostCheck]:
    details = check.get("details") if check else None
    items = details.get(field_name) if isinstance(details, dict) else None
    if not isinstance(items, list):
        return []
    return [row for row in (parse(item) for item in items) if row is not None]


def _note(check: dict[str, Any] | None, rows: list[HostCheck]) -> str:
    """Why a check produced no rows, in the core's words.

    A check that measured nothing is either not applicable (`skip`: no
    certificates yet, nothing reachable) or broken (`fail`: it crashed). Both are
    worth one line to an administrator, and neither is a verdict.
    """
    if check is None or rows:
        return ""
    summary = check.get("summary")
    return summary if isinstance(summary, str) else ""


def _sort_certs(certs: list[HostCheck]) -> list[HostCheck]:
    # Worst first, then soonest to expire, then by name -- the same "attention
    # first" order the Services page keeps for its modules.
    return sorted(
        certs,
        key=lambda c: (
            _SEVERITY[c.state],
            c.days_left if c.days_left is not None else 10**9,
            c.label,
        ),
    )


def parse_doctor(stdout: str, *, checked_at: datetime | None = None) -> HostHealth:
    """Turn `doctor --json` output into a `HostHealth`. Never raises.

    Takes only `disk_space` and `certs` out of the document, whatever else it
    carries; the exit status plays no part, because 2 means "a check failed" and
    the document is complete in that case.
    """
    try:
        document = json.loads(stdout)
    except ValueError:
        return unavailable("doctor did not return JSON")
    if not isinstance(document, dict):
        return unavailable("doctor did not return a JSON object")
    if document.get("schema_version") != _SUPPORTED_SCHEMA:
        return unavailable(f"doctor reports schema {document.get('schema_version')!r}, not 1")

    raw = document.get("checks")
    entries = raw if isinstance(raw, list) else []
    by_name = {
        c["name"]: c for c in entries if isinstance(c, dict) and isinstance(c.get("name"), str)
    }
    disk_check, cert_check = by_name.get("disk_space"), by_name.get("certs")
    if disk_check is None and cert_check is None:
        return unavailable("doctor reported neither disk space nor certificates")

    disks = _rows(disk_check, "paths", _parse_disk)
    certs = _sort_certs(_rows(cert_check, "certificates", _parse_cert))
    notes = tuple(n for n in (_note(disk_check, disks), _note(cert_check, certs)) if n)
    return HostHealth(
        available=True,
        disks=tuple(disks),
        certs=tuple(certs),
        notes=notes,
        checked_at=checked_at or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Running and caching
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Entry:
    at: float
    ttl: float
    health: HostHealth


# One manager process serves one deployment, so a single entry is all this needs.
_cache: _Entry | None = None
# The run in flight, if any. It is also the single-flight: whoever wants a reading
# while one is under way waits for this task instead of starting another. Held at
# module level because the loop keeps only a weak reference to a task, and because
# the run belongs to the process, not to the request that happened to start it --
# a visitor who closes the tab must not kill the `doctor` the next one waits for.
_refresh: asyncio.Task[HostHealth] | None = None


def reset_cache() -> None:
    """Forget the last reading and any run under way."""
    global _cache, _refresh  # noqa: PLW0603
    _cache = None
    _refresh = None


def is_supported(workspace_dir: str) -> bool:
    """Whether the mounted core has a `doctor` to run.

    A file test, not a version comparison: it is the thing the subprocess would
    import, so it cannot disagree with it. `SUPPORTED_CORE_MIN` stays a warning.
    """
    return (Path(workspace_dir) / "papaia" / "tools" / "lib" / "doctor.py").is_file()


def _first_line(text: str, limit: int = 200) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""


async def _run(config_dir: str, workspace_dir: str) -> HostHealth:
    if not is_supported(workspace_dir):
        return unavailable(_TOO_OLD)
    try:
        code, out, err = await run_py_cli(
            command="doctor",
            workspace_dir=workspace_dir,
            config_dir=config_dir,
            extra_flags=["--json", f"--skip={','.join(_SKIPPED_CHECKS)}"],
            limit=RUN_TIMEOUT_SECONDS,
        )
    except CtlError as exc:
        logger.warning("host health: %s", exc)
        return unavailable(str(exc))
    # Exit 2 with a document is a check that failed -- a result. Exit 2 with
    # nothing on stdout is a refused argument, and stderr says which.
    if not out.strip():
        return unavailable(_first_line(err) or f"doctor exited with status {code} and no output")
    return parse_doctor(out)


async def _measure(config_dir: str, workspace_dir: str) -> HostHealth:
    """One run, stored. Never raises: a task nobody awaits would lose the error."""
    global _cache  # noqa: PLW0603
    try:
        health = await _run(config_dir, workspace_dir)
    except Exception:
        logger.exception("host health: unexpected failure")
        health = unavailable("the host check failed unexpectedly")
    _cache = _Entry(
        at=time.monotonic(),
        ttl=CACHE_TTL_SECONDS if health.available else FAILURE_TTL_SECONDS,
        health=health,
    )
    return health


def _usable(*, force: bool) -> HostHealth | None:
    if _cache is None:
        return None
    age = time.monotonic() - _cache.at
    return _cache.health if age < (RECHECK_MIN_INTERVAL_SECONDS if force else _cache.ttl) else None


def _start(config_dir: str, workspace_dir: str) -> asyncio.Task[HostHealth]:
    global _refresh  # noqa: PLW0603
    if _refresh is None or _refresh.done():
        _refresh = asyncio.get_running_loop().create_task(_measure(config_dir, workspace_dir))
    return _refresh


async def load_host_health(
    *, config_dir: str, workspace_dir: str, force: bool = False
) -> HostHealth:
    """The host reading, from cache when it is recent enough, else a run awaited.

    Concurrent callers share one `doctor`. `force` skips the TTL but not
    `RECHECK_MIN_INTERVAL_SECONDS`, so a double click or two administrators
    pressing "Re-check" together cost one run.
    """
    hit = _usable(force=force)
    if hit is not None:
        return hit
    # Shielded: cancelling this request (tab closed) must not cancel the run.
    return await asyncio.shield(_start(config_dir, workspace_dir))


def cached_host_health() -> HostHealth | None:
    """The last reading if it is not too old to show, else None. Never forks."""
    if _cache is None or time.monotonic() - _cache.at > STALE_AFTER_SECONDS:
        return None
    return _cache.health


def ensure_fresh(*, config_dir: str, workspace_dir: str) -> None:
    """Start a background run if the cache has run out and none is under way.

    For the pages that render on every navigation: they must not wait for a
    `doctor`, but somebody has to keep the cache warm, and the 30 s chip poll is
    the cheapest thing that is already running. With no page open, nothing runs.
    Must be called from a running event loop.
    """
    if _usable(force=False) is None:
        _start(config_dir, workspace_dir)
