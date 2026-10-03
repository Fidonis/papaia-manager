"""Host health: resources, disk space and certificate expiry, as the core's `doctor` reports them.

The Services page answers whether the stack is up. This module answers whether
the machine under it is in shape, and it does so without measuring anything
itself: `papaia-ctl doctor` already knows which filesystems matter, where the
certificates live and what counts as too little room, so the manager runs it and
reads the verdict. A shell on the host and this panel therefore cannot disagree
about the same installation, and no threshold is declared twice.

Five decisions here are load-bearing:

* Six checks are used: `memory`, `cpu`, `gpu`, `time_sync`, `disk_space` and
  `certs`. `doctor` has five more, and they fork `docker`, resolve names and
  probe ports -- fine once, wrong on a poll. Those are skipped by name, and the
  answer is filtered to the six that were asked for, so a check the core adds
  later is ignored rather than shown unreviewed. The skip list only ever lists
  names old cores already know: `--skip` refuses an unknown one, so asking for a
  new check means *not* skipping it, and an older core simply leaves it out.
* The core's verdict is taken as is. Its disk thresholds are free bytes, not a
  percentage, and the percentage shown here is arithmetic for the eye only: it
  never decides a colour. The same goes for memory, CPU and VRAM, where the
  reason for a warning is only in the core's own sentence, which is shown as is.
* A location the container cannot see is reported as not measured, never as
  empty. The Docker data root is the usual one -- the manager mounts the config
  and backup directories, not `/var/lib/docker`, and `doctor` says so itself. A
  `skip` from the core (the GPU and the clock inside this container, today) is
  the same thing: a row that says so, with no verdict.
* Not knowing is not the same as knowing it is bad. A core without `doctor`, a
  run that timed out and an answer that does not parse all come back as
  `available=False` with a reason, and every consumer leaves them out of the
  verdict rather than painting the chip red over a missing probe.
* The measuring pace is a setting. One interval, kept in `settings.yaml`, is the
  cache's time to live, the page's poll and (times three) the point where the
  cache-only consumers stop showing a reading.

Reading is cheap and forking is not, so the two are separate. Pages that render
on every navigation (the status row, the sidebar dot) only look at the cache and
ask for a refresh in the background; the Host page itself awaits one. At most one
`doctor` runs at a time, and a finished run is reused for the interval, however
many people have a tab open.
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
from app.core.settings_store import refresh_interval

logger = logging.getLogger(__name__)

# The doctor checks this module reads, and the ones it asks the core not to run.
# Together they are the core's whole registry at 1.4.0. `--skip` refuses a name it
# does not know, so the skipped list is also what fails loudly if a check is
# renamed -- and why it names only checks every core with `doctor` has.
_WANTED_CHECKS = ("memory", "cpu", "gpu", "time_sync", "disk_space", "certs")
_SKIPPED_CHECKS = ("docker_version", "ports", "dns", "addon_compat", "container_health")

# Version of the JSON document `doctor --json` emits. A different one is
# somebody else's contract, not an older spelling of this one.
_SUPPORTED_SCHEMA = 1

# A failed run is remembered for less than the interval (or for the interval, when
# that is shorter), so a transient error clears itself soon without turning every
# poll into a retry.
FAILURE_TTL_SECONDS = 15.0

# "Re-check" bypasses the interval but not this: a double click, or two
# administrators pressing it together, share one run.
RECHECK_MIN_INTERVAL_SECONDS = 5.0

# Generous against the core's own budget (`docker info` 10 s, `nvidia-smi` 10 s,
# `openssl` 10 s per certificate), short enough that a hung child is not left
# holding the lock.
RUN_TIMEOUT_SECONDS = 30.0

# Past this, a cached reading is no longer shown by the cache-only consumers. The
# chip refreshes it in the background on every poll, so this only matters when
# nobody has had a page open for a while. A long interval raises it: the cut-off
# is never less than three intervals, or a reading would expire before the next
# one was due.
STALE_AFTER_SECONDS = 180.0
STALE_AFTER_INTERVALS = 3.0

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
    """One measured thing: a resource, a filesystem or a certificate.

    `kind` is `disk`, `cert`, or one of `memory`, `cpu`, `gpu` and `clock` for the
    resources. `detail` is what the row prints under its label -- the path of a
    filesystem, the issuer of a certificate, the driver of a GPU. All of it is
    shown to administrators only.

    A resource row is drawn from `percent` (the bar) and the two texts beside it,
    `measure` and `figure`. All three are for the eye: the colour is `state`, which
    is the core's. `reason` is the core's own sentence for a row that is not OK,
    because for memory, CPU and VRAM that sentence is the only place that says
    which limit was crossed.
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
    percent: float | None = None
    measure: str = ""
    figure: str = ""

    @property
    def used_percent(self) -> float | None:
        """Share that is used. For the eye, never a verdict.

        A resource brings its own percentage. For a filesystem it is worked out
        from `free`, which is what an unprivileged process may still use, so it
        reads a little higher than `df` on a filesystem with reserved blocks.
        """
        if self.percent is not None:
            return round(min(max(self.percent, 0.0), 100.0), 1)
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
    resources: tuple[HostCheck, ...] = ()
    disks: tuple[HostCheck, ...] = ()
    certs: tuple[HostCheck, ...] = ()
    notes: tuple[str, ...] = ()
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    @property
    def checks(self) -> tuple[HostCheck, ...]:
        return self.resources + self.disks + self.certs

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
    def resources_measured(self) -> int:
        return sum(1 for c in self.resources if c.state is not HostState.UNKNOWN)

    @property
    def resources_not_measurable(self) -> int:
        return len(self.resources) - self.resources_measured

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


def _number(value: Any) -> float | None:
    return float(value) if isinstance(value, int | float) and not isinstance(value, bool) else None


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


def _details(check: dict[str, Any]) -> dict[str, Any]:
    details = check.get("details")
    return details if isinstance(details, dict) else {}


def _summary(check: dict[str, Any]) -> str:
    summary = check.get("summary")
    return summary.strip() if isinstance(summary, str) else ""


def _why(state: HostState, summary: str, shown: str) -> str:
    """The core's sentence for a row that is not OK, unless the row says it already.

    Memory, CPU and VRAM report the reason for a warning only in prose ("6.9 %
    available (warn below 10 %)"). Quoting it beats re-deriving it here, which
    would mean a threshold of this panel's own.
    """
    return summary if state is not HostState.OK and summary and summary != shown else ""


def _parse_memory(check: dict[str, Any], state: HostState) -> list[HostCheck]:
    details, summary = _details(check), _summary(check)
    total, available = _int(details.get("total_bytes")), _int(details.get("available_bytes"))
    swap_total = _int(details.get("swap_total_bytes"))
    swap_used = _int(details.get("swap_used_bytes"))
    used = _number(details.get("used_percent"))
    if total is not None and available is not None:
        measure = f"{format_bytes(available)} available of {format_bytes(total)}"
    elif total is not None:
        # No /proc (the core fell back to `docker info`): the total is all there is.
        measure = f"{format_bytes(total)} total"
    else:
        measure = summary
    if swap_total is None or swap_used is None:
        swap = ""
    elif swap_total == 0:
        swap = "no swap"
    else:
        swap = f"swap {format_bytes(swap_used)} of {format_bytes(swap_total)} used"
    return [
        HostCheck(
            kind="memory",
            key="memory",
            label="Memory",
            detail=swap,
            state=state,
            reason=_why(state, summary, measure),
            percent=used,
            measure=measure,
            figure=f"{used:.0f} % used" if used is not None else "",
        )
    ]


def _parse_cpu(check: dict[str, Any], state: HostState) -> list[HostCheck]:
    details, summary = _details(check), _summary(check)
    cores = _int(details.get("cores"))
    load1, load5, load15 = (_number(details.get(k)) for k in ("load1", "load5", "load15"))
    per_core = _number(details.get("load_per_core"))
    parts: list[str] = []
    if cores is not None:
        parts.append(f"{cores} core" if cores == 1 else f"{cores} cores")
    if load1 is not None and load5 is not None and load15 is not None:
        parts.append(f"load {load1:.2f} / {load5:.2f} / {load15:.2f}")
    measure = "load per core, 5 min average" if per_core is not None else summary
    return [
        HostCheck(
            kind="cpu",
            key="cpu",
            label="CPU",
            detail=" · ".join(parts),
            state=state,
            reason=_why(state, summary, measure),
            # One load per core is "full"; the bar stops there however high it goes.
            percent=per_core * 100.0 if per_core is not None else None,
            measure=measure,
            figure=f"{per_core:.2f} / core" if per_core is not None else "",
        )
    ]


def _parse_gpu(check: dict[str, Any], state: HostState) -> list[HostCheck]:
    details, summary = _details(check), _summary(check)
    listed = details.get("gpus")
    devices = [g for g in listed if isinstance(g, dict)] if isinstance(listed, list) else []
    if not devices:
        # A variant with no per-device figures (Intel, Vulkan, AMD without
        # `rocm-smi`): the core's sentence is the reading.
        variant = details.get("variant")
        return [
            HostCheck(
                kind="gpu",
                key="gpu",
                label="GPU",
                detail=f"variant {variant}" if isinstance(variant, str) and variant else "",
                state=state,
                measure=summary,
            )
        ]
    rows: list[HostCheck] = []
    for position, gpu in enumerate(devices):
        index = _int(gpu.get("index"))
        number = index if index is not None else position
        name, driver = gpu.get("name"), gpu.get("driver")
        used_mib = _number(gpu.get("memory_used_mib"))
        total_mib = _number(gpu.get("memory_total_mib"))
        vram = _number(gpu.get("vram_percent"))
        util = _number(gpu.get("utilization_percent"))
        temperature = _number(gpu.get("temperature_c"))
        parts: list[str] = []
        if isinstance(driver, str) and driver:
            parts.append(f"driver {driver}")
        if util is not None:
            parts.append(f"util {util:.0f} %")
        if temperature is not None:
            parts.append(f"{temperature:.0f} °C")
        measure = ""
        if used_mib is not None and total_mib is not None:
            measure = (
                f"{format_bytes(int(used_mib * 1024**2))} of "
                f"{format_bytes(int(total_mib * 1024**2))} VRAM"
            )
        rows.append(
            HostCheck(
                kind="gpu",
                key=f"gpu{number}",
                label=name if isinstance(name, str) and name else f"GPU {number}",
                detail=" · ".join(parts),
                state=state,
                # The core judges the check, not each device, so every device
                # carries its colour. The sentence that says which one tripped it
                # goes on the first row only.
                reason=_why(state, summary, "") if position == 0 else "",
                percent=vram,
                measure=measure,
                figure=f"{vram:.0f} % used" if vram is not None else "",
            )
        )
    return rows


def _parse_clock(check: dict[str, Any], state: HostState) -> list[HostCheck]:
    synchronized = _details(check).get("ntp_synchronized")
    summary = _summary(check)
    if synchronized is True:
        measure = "synchronized"
    elif synchronized is False:
        measure = "not synchronized"
    else:
        measure = summary
    return [
        HostCheck(
            kind="clock",
            key="time_sync",
            label="System clock",
            detail="",
            state=state,
            # A boolean says it all; the sentence only helps when there was none.
            reason="" if isinstance(synchronized, bool) else _why(state, summary, measure),
            measure=measure,
        )
    ]


# (check name, row kind, label when it has no reading, parser). The order is the
# order the rows are shown in, which is also the core's own.
_RESOURCE_CHECKS: tuple[tuple[str, str, str, Any], ...] = (
    ("memory", "memory", "Memory", _parse_memory),
    ("cpu", "cpu", "CPU", _parse_cpu),
    ("gpu", "gpu", "GPU", _parse_gpu),
    ("time_sync", "clock", "System clock", _parse_clock),
)


def _resource_rows(by_name: dict[str, dict[str, Any]]) -> list[HostCheck]:
    rows: list[HostCheck] = []
    for name, kind, label, parse in _RESOURCE_CHECKS:
        check = by_name.get(name)
        if check is None:
            # A core that predates the check: no row, rather than an unexplained one.
            continue
        state = _state(check.get("status"))
        if state is HostState.UNKNOWN:
            # `gpu` skips with no details at all when there is nothing to measure
            # (no LocalAI, or the CPU image). That is not a gap in the panel, so it
            # gets no row. With a variant in the details a GPU is configured and
            # could not be read, which is worth saying.
            if name == "gpu" and not _details(check).get("variant"):
                continue
            rows.append(
                HostCheck(
                    kind=kind,
                    key=name,
                    label=label,
                    detail="",
                    state=HostState.UNKNOWN,
                    reason=_summary(check),
                )
            )
            continue
        rows.extend(parse(check, state))
    return rows


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

    Takes only the checks in `_WANTED_CHECKS` out of the document, whatever else it
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
    if not any(name in by_name for name in _WANTED_CHECKS):
        return unavailable("doctor reported none of the host checks")

    disk_check, cert_check = by_name.get("disk_space"), by_name.get("certs")
    resources = _resource_rows(by_name)
    disks = _rows(disk_check, "paths", _parse_disk)
    certs = _sort_certs(_rows(cert_check, "certificates", _parse_cert))
    notes = tuple(n for n in (_note(disk_check, disks), _note(cert_check, certs)) if n)
    return HostHealth(
        available=True,
        resources=tuple(resources),
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
    # When the cache-only consumers stop showing this reading. Fixed when the
    # reading is taken; how long it counts as fresh is not (see `_usable`).
    stale_after: float
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


async def _measure(config_dir: str, workspace_dir: str, interval: int) -> HostHealth:
    """One run, stored. Never raises: a task nobody awaits would lose the error."""
    global _cache  # noqa: PLW0603
    try:
        health = await _run(config_dir, workspace_dir)
    except Exception:
        logger.exception("host health: unexpected failure")
        health = unavailable("the host check failed unexpectedly")
    _cache = _Entry(
        at=time.monotonic(),
        stale_after=max(STALE_AFTER_SECONDS, STALE_AFTER_INTERVALS * interval),
        health=health,
    )
    return health


def _usable(*, force: bool, interval: int) -> HostHealth | None:
    """The cached reading if it is still fresh, else None.

    Judged against the interval as it is *now*, not as it was when the reading was
    taken: shortening it in Settings takes effect at the next poll instead of after
    the old, longer wait.
    """
    if _cache is None:
        return None
    if force:
        limit = RECHECK_MIN_INTERVAL_SECONDS
    elif _cache.health.available:
        limit = float(interval)
    else:
        limit = min(FAILURE_TTL_SECONDS, float(interval))
    return _cache.health if time.monotonic() - _cache.at < limit else None


def _start(config_dir: str, workspace_dir: str, interval: int) -> asyncio.Task[HostHealth]:
    global _refresh  # noqa: PLW0603
    if _refresh is None or _refresh.done():
        _refresh = asyncio.get_running_loop().create_task(
            _measure(config_dir, workspace_dir, interval)
        )
    return _refresh


async def load_host_health(
    *, config_dir: str, workspace_dir: str, force: bool = False
) -> HostHealth:
    """The host reading, from cache when it is recent enough, else a run awaited.

    Concurrent callers share one `doctor`. `force` skips the interval but not
    `RECHECK_MIN_INTERVAL_SECONDS`, so a double click or two administrators
    pressing "Re-check" together cost one run.
    """
    interval = refresh_interval(config_dir)
    hit = _usable(force=force, interval=interval)
    if hit is not None:
        return hit
    # Shielded: cancelling this request (tab closed) must not cancel the run.
    return await asyncio.shield(_start(config_dir, workspace_dir, interval))


def cached_host_health() -> HostHealth | None:
    """The last reading if it is not too old to show, else None. Never forks."""
    if _cache is None or time.monotonic() - _cache.at > _cache.stale_after:
        return None
    return _cache.health


def ensure_fresh(*, config_dir: str, workspace_dir: str) -> None:
    """Start a background run if the cache has run out and none is under way.

    For the pages that render on every navigation: they must not wait for a
    `doctor`, but somebody has to keep the cache warm, and the 30 s chip poll is
    the cheapest thing that is already running. With no page open, nothing runs.
    So the chip refreshes no faster than every 30 s, however short the interval.
    Must be called from a running event loop.
    """
    interval = refresh_interval(config_dir)
    if _usable(force=False, interval=interval) is None:
        _start(config_dir, workspace_dir, interval)
