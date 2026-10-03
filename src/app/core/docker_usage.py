"""Docker usage: what Docker's data takes, as the core's `doctor` reports it.

`doctor`'s `docker_usage` check reads `docker system df` and says how much of the
disk the images, containers, volumes and build cache hold, and how much of that
the daemon calls reclaimable. It is the answer to what is in the "Docker data" the
Host page cannot measure the free space of: the manager container does not mount
`/var/lib/docker`, but the daemon reports the same over the socket it does mount.

It is measured apart from the rest of the Host page, because it is the one check
that costs real work. The daemon sizes every volume to answer, which took a second
and a half on a stack with 75 volumes and grows with the data. So it runs in a
`doctor` of its own, at a slower pace (see `usage_interval`), behind a cache of its
own, and is shown from that cache:

* Whatever it costs or however it ends, the main reading is not held up by it and
  never loses a row to it. A slow or failing `docker system df` is a note under
  the disk space, not an unavailable page.
* A visitor waits for it only when there is nothing to show yet, or when they
  pressed "Re-check". Otherwise the last reading is shown and a fresh one is
  started behind the page.

The core may not have the check. It is looked for in the core's own check registry
before anything is asked for, because `doctor --skip` refuses a name it does not
know: an older core is then left alone and the page simply has no Docker usage.
There is no verdict in it, so it never counts towards the chip or the dot.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.core.ctl import CtlError, run_py_cli
from app.core.settings_store import refresh_interval

logger = logging.getLogger(__name__)

CHECK_NAME = "docker_usage"

# The core's check registry, as it is spelled in `lib/doctor.py`:
# `    ("docker_usage", check_docker_usage),`. The manager asks the core what it
# knows by reading the file the subprocess would import, the same test
# `host_health.is_supported` makes for `doctor` itself.
_REGISTRY_ENTRY = re.compile(r'^\s*\("([a-z_]+)",\s*check_[a-z_]+\)', re.MULTILINE)

_SUPPORTED_SCHEMA = 1

# Ten times the refresh interval, so the page's pace still sets the tone, but never
# more often than every five minutes (sizes of images and volumes do not move
# faster than that to matter) and never rarer than an hour, unless the interval
# itself is longer.
USAGE_INTERVAL_FACTOR = 10
USAGE_MIN_INTERVAL_SECONDS = 300.0
USAGE_MAX_INTERVAL_SECONDS = 3600.0

# A failed or skipped reading is tried again sooner than a good one is replaced.
USAGE_FAILURE_TTL_SECONDS = 120.0

# "Re-check" asks for a fresh reading, but this one costs the daemon work: twice in
# half a minute is one run.
USAGE_RECHECK_MIN_INTERVAL_SECONDS = 30.0

# The core bounds its own `docker system df` at 20 s; this is that plus a Python
# start-up, and it keeps a hung child from holding the single-flight.
USAGE_RUN_TIMEOUT_SECONDS = 30.0

# (key in the core's details, label). The core's order, which is Docker's.
_ROWS = (
    ("images", "Images"),
    ("containers", "Containers"),
    ("volumes", "Volumes"),
    ("build_cache", "Build cache"),
)

_UNITS = ("B", "kB", "MB", "GB")


def format_decimal_bytes(value: int) -> str:
    """`50.46 GB`, `235.9 MB`, `16.38 kB`: the decimal units and four significant
    digits `docker system df` itself prints, so the page reads like the command."""
    size = float(value)
    for unit in _UNITS:
        if size < 1000:
            return f"{size:.4g} {unit}"
        size /= 1000
    return f"{size:.4g} TB"


@dataclass(frozen=True)
class DockerUsageRow:
    """One line of `docker system df`."""

    key: str
    label: str
    count: int | None
    active: int | None
    size_bytes: int
    reclaimable_bytes: int | None
    # Share of everything Docker holds, for the bar. For the eye only.
    percent: float

    @property
    def size_text(self) -> str:
        return format_decimal_bytes(self.size_bytes)

    @property
    def reclaimable_text(self) -> str:
        if self.reclaimable_bytes is None:
            return ""
        return f"{format_decimal_bytes(self.reclaimable_bytes)} reclaimable"

    @property
    def detail(self) -> str:
        """`53 total · 31 active`, in Docker's own column names."""
        parts = []
        if self.count is not None:
            parts.append(f"{self.count} total")
        if self.active is not None:
            parts.append(f"{self.active} active")
        return " · ".join(parts)


@dataclass(frozen=True)
class DockerUsage:
    """One reading of Docker's disk use, or the reason there is none."""

    available: bool
    reason: str = ""
    rows: tuple[DockerUsageRow, ...] = ()
    total_bytes: int = 0
    reclaimable_bytes: int = 0
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def total_text(self) -> str:
        return format_decimal_bytes(self.total_bytes)

    @property
    def reclaimable_text(self) -> str:
        return format_decimal_bytes(self.reclaimable_bytes)


def unavailable(reason: str) -> DockerUsage:
    return DockerUsage(available=False, reason=reason)


# ---------------------------------------------------------------------------
# What the core knows
# ---------------------------------------------------------------------------


def core_checks(workspace_dir: str) -> tuple[str, ...]:
    """The names in the core's `doctor` registry, in order. Empty if it cannot be read.

    Read from the source because there is no way to ask `doctor` which checks it
    has, and `--skip` rejects a name it does not know. Empty is the safe answer:
    nothing then depends on a check being there."""
    path = Path(workspace_dir) / "papaia" / "tools" / "lib" / "doctor.py"
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ()
    return tuple(_REGISTRY_ENTRY.findall(text))


def core_has_usage(workspace_dir: str) -> bool:
    return CHECK_NAME in core_checks(workspace_dir)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def _int(value: Any) -> int | None:
    # bool is an int subclass; a JSON `true` is not a byte count.
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _row(key: str, label: str, entry: Any, total: int) -> DockerUsageRow | None:
    if not isinstance(entry, dict):
        return None
    size = _int(entry.get("size_bytes"))
    if size is None:
        return None
    share = round(min(max(100.0 * size / total, 0.0), 100.0), 1) if total else 0.0
    return DockerUsageRow(
        key=key,
        label=label,
        count=_int(entry.get("count")),
        active=_int(entry.get("active")),
        size_bytes=size,
        reclaimable_bytes=_int(entry.get("reclaimable_bytes")),
        percent=share,
    )


def parse_docker_usage(stdout: str, *, checked_at: datetime | None = None) -> DockerUsage:
    """Turn `doctor --json` output into a `DockerUsage`. Never raises.

    Takes only the `docker_usage` check, whatever else the document carries. A
    `skip` from the core (Docker could not be asked, it timed out, the output was
    not recognised) is a reading with a reason; anything else that is not a usable
    `pass` is the same."""
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
        return unavailable("doctor did not report docker_usage")
    summary = check.get("summary")
    reason = summary.strip() if isinstance(summary, str) else ""
    if check.get("status") != "pass":
        return unavailable(reason or "docker_usage could not be read")

    details = check.get("details")
    types = details.get("types") if isinstance(details, dict) else None
    if not isinstance(types, dict):
        return unavailable("doctor reported docker_usage without figures")
    # The bars are shares of the rows that are shown, not of a type Docker adds later.
    sizes = [
        _int(entry.get("size_bytes"))
        for key, _ in _ROWS
        if isinstance(entry := types.get(key), dict)
    ]
    total = sum(s for s in sizes if s is not None)
    rows = tuple(
        row
        for key, label in _ROWS
        if (row := _row(key, label, types.get(key), total)) is not None
    )
    if not rows:
        return unavailable("doctor reported docker_usage without figures")
    return DockerUsage(
        available=True,
        rows=rows,
        total_bytes=sum(r.size_bytes for r in rows),
        reclaimable_bytes=sum(r.reclaimable_bytes or 0 for r in rows),
        checked_at=checked_at or datetime.now(UTC),
    )


# ---------------------------------------------------------------------------
# Running and caching
# ---------------------------------------------------------------------------


def usage_interval(interval: int) -> float:
    """Seconds a reading is good for, given the Host page's refresh interval."""
    paced = min(
        max(USAGE_MIN_INTERVAL_SECONDS, USAGE_INTERVAL_FACTOR * interval),
        USAGE_MAX_INTERVAL_SECONDS,
    )
    # An interval longer than the cap is the operator asking for slow: honoured.
    return max(float(interval), paced)


@dataclass(frozen=True)
class _Entry:
    at: float
    usage: DockerUsage


# One manager process serves one deployment, so a single entry is all this needs.
_cache: _Entry | None = None
# The run in flight, if any: the single-flight, held here for the same reasons as in
# `host_health` (the loop keeps only a weak reference to a task, and the run belongs
# to the process, not to the request that started it).
_refresh: asyncio.Task[DockerUsage] | None = None


def reset_cache() -> None:
    """Forget the last reading and any run under way."""
    global _cache, _refresh  # noqa: PLW0603
    _cache = None
    _refresh = None


def _first_line(text: str, limit: int = 200) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""


async def _run(config_dir: str, workspace_dir: str) -> DockerUsage:
    names = core_checks(workspace_dir)
    if CHECK_NAME not in names:
        return unavailable("this papAIa core has no docker_usage check")
    try:
        code, out, err = await run_py_cli(
            command="doctor",
            workspace_dir=workspace_dir,
            config_dir=config_dir,
            # Everything but the one check, so this `doctor` does nothing else.
            extra_flags=["--json", f"--skip={','.join(n for n in names if n != CHECK_NAME)}"],
            limit=USAGE_RUN_TIMEOUT_SECONDS,
        )
    except CtlError as exc:
        logger.warning("docker usage: %s", exc)
        return unavailable(str(exc))
    if not out.strip():
        return unavailable(_first_line(err) or f"doctor exited with status {code} and no output")
    return parse_docker_usage(out)


async def _measure(config_dir: str, workspace_dir: str) -> DockerUsage:
    """One run, stored. Never raises: a task nobody awaits would lose the error."""
    global _cache  # noqa: PLW0603
    try:
        usage = await _run(config_dir, workspace_dir)
    except Exception:
        logger.exception("docker usage: unexpected failure")
        usage = unavailable("the Docker usage check failed unexpectedly")
    _cache = _Entry(at=time.monotonic(), usage=usage)
    return usage


def _fresh(*, force: bool, interval: int) -> DockerUsage | None:
    """The cached reading if it is still good, else None."""
    if _cache is None:
        return None
    if force:
        limit = USAGE_RECHECK_MIN_INTERVAL_SECONDS
    elif _cache.usage.available:
        limit = usage_interval(interval)
    else:
        limit = min(USAGE_FAILURE_TTL_SECONDS, usage_interval(interval))
    return _cache.usage if time.monotonic() - _cache.at < limit else None


def _start(config_dir: str, workspace_dir: str) -> asyncio.Task[DockerUsage]:
    global _refresh  # noqa: PLW0603
    if _refresh is None or _refresh.done():
        _refresh = asyncio.get_running_loop().create_task(_measure(config_dir, workspace_dir))
    return _refresh


async def load_docker_usage(
    *, config_dir: str, workspace_dir: str, force: bool = False
) -> DockerUsage | None:
    """The Docker usage to show, or None when the core has nothing to offer.

    A reading that is still good is returned as it is. With none at all, or when
    `force` is set (the "Re-check" button) and the last one is older than the
    minimum, the run is awaited. Otherwise the last reading, however stale, is
    returned at once and a fresh one is started behind it, so a visitor never waits
    on `docker system df` more than the first time. Concurrent callers share one run.
    """
    if not core_has_usage(workspace_dir):
        return None
    hit = _fresh(force=force, interval=refresh_interval(config_dir))
    if hit is not None:
        return hit
    if _cache is None or force:
        # Shielded: a visitor who leaves must not cancel the run for the next.
        return await asyncio.shield(_start(config_dir, workspace_dir))
    _start(config_dir, workspace_dir)
    return _cache.usage
