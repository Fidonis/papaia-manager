"""Finding the conditions that call for a notification, and deciding which of them are new.

A detector reads what the manager already knows (the backup catalogue, the stack, the host
reading, the release tags) and answers with the conditions that hold *now*. It does not know
what was reported before. `decide` is the other half: it compares the conditions with what was
remembered and returns the ones that are new, and every rule about repeating or staying quiet
lives there, as a function of its arguments, so it can be tested without a clock or a stack.

Two kinds of event, and the difference is what "new" means:

* edge events (`backup_overdue`, `service_unhealthy`): a condition is new when it turns true.
  It is not reported again while it stays true, and a service that flaps is reported at most
  once per debounce window.
* marker events (`update_available`, `cert_expiring`): a condition is new when its marker
  changes -- the version on offer, the date a certificate expires. A renewed certificate or a
  newer release is a new occurrence by itself, and the same one is never reported twice.

`job_failed` is not detected here: the job queue says so when it happens (see `service`).

A detector that cannot tell (Docker unreachable, no `doctor`) returns nothing at all rather
than an empty finding, because "nothing is wrong" would clear what is remembered and report it
again when the answer comes back.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import Settings
from app.core import backups, host_health, runner, schedule, services, upgrade
from app.core.catalogs import load_registry
from app.core.jobs import JobQueue
from app.core.notifications.model import Event, EventSettings
from app.core.notifications.store import NotifyState, Seen, load_state, update_state
from app.core.resolve import outdated_addons
from app.core.snapshots import load_installed

logger = logging.getLogger(__name__)

EDGE_EVENTS = frozenset({Event.BACKUP_OVERDUE, Event.SERVICE_UNHEALTHY})

# A seen entry that is over and was last reported this long ago is forgotten.
_FORGET_AFTER = timedelta(days=30)


@dataclass(frozen=True)
class Condition:
    """Something that is true now and may call for a notification."""

    event: Event
    # What it is about: a service, a certificate, `core`, `addon:<name>`. Unique per event.
    key: str
    # What makes a repeat a new occurrence (marker events): a version, an expiry date.
    marker: str
    # The name in a log line ("litellm"), the part of the message that says which one.
    subject: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Finding:
    """The conditions of one event that hold now, within one scope.

    `scope` is the prefix of the keys this finding is complete for: a finding for `core` says
    nothing about `addon:*`, so a pass that could not read the add-ons must not clear them."""

    event: Event
    conditions: tuple[Condition, ...]
    scope: str = ""


Detector = Callable[[EventSettings], Awaitable[list[Finding]]]


def _key(event: Event, key: str) -> str:
    return f"{event.value}|{key}"


def decide(
    findings: list[Finding],
    state: NotifyState,
    *,
    now: datetime,
    settings: EventSettings,
) -> list[Condition]:
    """The conditions that are new, in order. Records them in `state`; the caller stores it."""
    new: list[Condition] = []
    for finding in findings:
        event = finding.event
        prefix = _key(event, finding.scope)
        current = {_key(event, c.key): c for c in finding.conditions}

        for stored_key, condition in current.items():
            seen = state.seen.get(stored_key)
            if event in EDGE_EVENTS:
                fire = seen is None or not seen.active
                if fire and event is Event.SERVICE_UNHEALTHY and seen is not None:
                    fire = _outside_window(seen, now, settings.debounce_minutes)
            else:
                fire = seen is None or seen.marker != condition.marker
            entry = seen if seen is not None else Seen()
            entry.active = True
            entry.marker = condition.marker
            if fire:
                entry.last_sent = now.isoformat()
                new.append(condition)
            state.seen[stored_key] = entry

        # What held at the last look and does not hold now. An edge event keeps its entry
        # (the debounce needs the time it was last reported); a marker event drops it.
        for stored_key in [k for k in state.seen if k.startswith(prefix) and k not in current]:
            entry = state.seen[stored_key]
            if event in EDGE_EVENTS:
                entry.active = False
                if _forgotten(entry, now):
                    del state.seen[stored_key]
            else:
                del state.seen[stored_key]
    return new


def _outside_window(seen: Seen, now: datetime, debounce_minutes: int) -> bool:
    if not seen.last_sent:
        return True
    try:
        last = datetime.fromisoformat(seen.last_sent)
    except ValueError:
        return True
    return now - last >= timedelta(minutes=debounce_minutes)


def _forgotten(entry: Seen, now: datetime) -> bool:
    if not entry.last_sent:
        return True
    try:
        return now - datetime.fromisoformat(entry.last_sent) > _FORGET_AFTER
    except ValueError:
        return True


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


def _span(delta: timedelta) -> str:
    """`3 days`, `5 hours`, `20 minutes`: how long, at the precision a person reads."""
    seconds = max(int(delta.total_seconds()), 0)
    for unit, size in (("day", 86_400), ("hour", 3_600), ("minute", 60)):
        if seconds >= size:
            count = seconds // size
            return f"{count} {unit}{'' if count == 1 else 's'}"
    return "less than a minute"


def backup_overdue(config_dir: str, *, now: datetime) -> list[Finding]:
    """The newest good backup is older than the schedule allows (see `schedule.evaluate`).

    Only a backup schedule can make a backup overdue: without one nobody promised a backup,
    and the status strip of the Backup page says nothing either."""
    load = schedule.load_schedule(config_dir)
    if load.error:
        return []
    if load.schedule is None:
        return [Finding(Event.BACKUP_OVERDUE, ())]
    backup_dir = backups.resolve_backup_dir(config_dir)
    if not backups.is_reachable(backup_dir):
        # An unreachable directory has no restore points to judge; that is a different
        # problem from an overdue backup and not one this can tell apart from "empty".
        return []
    status = schedule.evaluate(
        load.schedule, backups.load_restore_points(backup_dir), now=now, next_run=None
    )
    if not status.overdue:
        return [Finding(Event.BACKUP_OVERDUE, ())]
    if status.age is not None:
        message = f"The last good backup is {_span(status.age)} old."
    else:
        message = f"No backup has succeeded yet; the latest one ended {status.latest_result}."
    details: dict[str, Any] = {
        "last_good_backup": status.last_ok_at.isoformat() if status.last_ok_at else None,
        "latest_result": status.latest_result,
    }
    if status.overdue_after is not None:
        details["overdue_after_hours"] = round(status.overdue_after.total_seconds() / 3600, 1)
    return [
        Finding(
            Event.BACKUP_OVERDUE,
            (Condition(Event.BACKUP_OVERDUE, "backup", "", "backup", message, details),),
        )
    ]


def read_services(config_dir: str, workspace_dir: str) -> services.StackSnapshot | None:
    """The stack as Docker reports it, or None when it cannot be told.

    Blocking: it asks Docker. An empty snapshot is what `load_snapshot` returns when Docker did
    not answer (and for a host with nothing deployed): neither says anything about a service, so
    both are "unknown"."""
    snapshot = services.load_snapshot(config_dir, workspace_dir)
    if not snapshot.core and not snapshot.addons:
        return None
    return snapshot


def service_findings(
    snapshot: services.StackSnapshot, was_up: frozenset[str]
) -> tuple[list[Finding], frozenset[str]]:
    """The modules that are down now, and the modules that are known to have run.

    A module is down when it is `stopped` or `unhealthy`, or when it is `missing` after a pass
    saw it deployed: a stop with "Clean up" removes the containers while the profile stays
    active, and so does `docker rm`, and either is an outage as much as a stopped container.
    A module that was never seen deployed is not: a profile that was enabled and never started
    is a choice, and one mail per such module on the first pass would be noise. `starting` is
    not down: it is on its way.

    `up` is what the next pass is told. A module joins it as soon as it has containers in a
    state that says it was meant to run (`healthy`, `starting`, `unhealthy`, `stopped`), not
    only when it is `healthy`: a service that takes a minute to pass its health check and is
    stopped before that was running all the same. A one-shot that has `completed` does not
    join. A module that is down stays in it, so it keeps being known while it is gone; one that
    is no longer in the snapshot at all (its profile was switched off, the add-on removed)
    leaves it."""
    modules = [*snapshot.core, *snapshot.addons]
    labels = {_label(m): m for m in modules}
    down = (services.ServiceHealth.UNHEALTHY, services.ServiceHealth.STOPPED)
    conditions: list[Condition] = []
    for label, module in labels.items():
        health = module.health
        if health in down:
            message = f"{label} is {health.value}: {module.summary}."
        elif health is services.ServiceHealth.MISSING and label in was_up:
            if all(c.health is services.ServiceHealth.MISSING for c in module.containers):
                message = f"{label} is not running: its containers are gone."
            else:
                message = f"{label} is not running ({module.summary})."
        else:
            continue
        conditions.append(
            Condition(
                Event.SERVICE_UNHEALTHY,
                label,
                "",
                label,
                message,
                {
                    "service": module.name,
                    "add_on": module.addon or None,
                    "state": health.value,
                    "summary": module.summary,
                },
            )
        )
    deployed = {label for label, m in labels.items() if m.health in _DEPLOYED}
    up_now = frozenset(deployed | {label for label in was_up if label in labels})
    return [Finding(Event.SERVICE_UNHEALTHY, tuple(conditions))], up_now


# The states of a module that say it has been brought up: `missing` is not one, and a one-shot
# that has `completed` (or a module that is `unknown`) says nothing about a service.
_DEPLOYED = (
    services.ServiceHealth.HEALTHY,
    services.ServiceHealth.STARTING,
    services.ServiceHealth.UNHEALTHY,
    services.ServiceHealth.STOPPED,
)


def _label(module: services.ServiceModule) -> str:
    return f"{module.addon}/{module.name}" if module.addon else module.name


def certificates(health: host_health.HostHealth, *, lead_days: int) -> list[Finding]:
    """Certificates the host reading sees that expire within `lead_days`, or already have.

    Judged by the days left, not by the core's own warn and fail thresholds: the lead time is
    the administrator's. Only the certificates `doctor` scans are seen (the ones under the
    configuration directory and Let's Encrypt's `live` directory)."""
    if not health.available:
        return []
    conditions: list[Condition] = []
    for cert in health.certs:
        if cert.days_left is None or cert.days_left > lead_days:
            continue
        if cert.days_left < 0:
            message = f"{cert.label} expired {-cert.days_left} days ago."
        elif cert.days_left == 0:
            message = f"{cert.label} expires today."
        else:
            plural = "" if cert.days_left == 1 else "s"
            message = f"{cert.label} expires in {cert.days_left} day{plural}."
        conditions.append(
            Condition(
                Event.CERT_EXPIRING,
                cert.key,
                cert.not_after or "expiry unknown",
                cert.label,
                message,
                {
                    "certificate": cert.label,
                    "days_left": cert.days_left,
                    "expires": cert.not_after or None,
                },
            )
        )
    return [Finding(Event.CERT_EXPIRING, tuple(conditions))]


def addon_updates(config_dir: str, workspace_dir: str) -> list[Finding]:
    """Installed add-ons for which a catalog on disk offers another version. Blocking."""
    installed = load_installed(config_dir)
    if not installed:
        return [Finding(Event.UPDATE_AVAILABLE, (), scope="addon:")]
    outdated = outdated_addons(load_registry(config_dir), workspace_dir, installed)
    conditions = tuple(
        Condition(
            Event.UPDATE_AVAILABLE,
            f"addon:{update.name}",
            update.available,
            update.name,
            f"{update.name} {update.available} is available (installed: {update.installed}).",
            {
                "add_on": update.name,
                "installed": update.installed,
                "available": update.available,
                "catalog": update.catalog,
            },
        )
        for update in outdated
    )
    return [Finding(Event.UPDATE_AVAILABLE, conditions, scope="addon:")]


def core_update(current: str, newer: list[str] | None) -> list[Finding]:
    """A release ahead of the deployed one. `newer` is None when that could not be told."""
    if newer is None:
        return []
    if not newer:
        return [Finding(Event.UPDATE_AVAILABLE, (), scope="core")]
    latest = newer[0]
    return [
        Finding(
            Event.UPDATE_AVAILABLE,
            (
                Condition(
                    Event.UPDATE_AVAILABLE,
                    "core",
                    latest,
                    "papAIa",
                    f"papAIa {latest} is available (deployed: {current}).",
                    {"deployed": current, "available": latest, "releases_ahead": len(newer)},
                ),
            ),
            scope="core",
        )
    ]


async def stack_is_busy(queue: JobQueue | None) -> bool:
    """A job is running, or a restore, an upgrade or a stack action is.

    They stop, pause and recreate containers on purpose (a backup pauses each one while it
    archives it): what a reading of the stack shows then is the operation, not a fault."""
    if queue is not None and queue.active_job() is not None:
        return True
    for kind in (runner.RESTORE_KIND, runner.UPGRADE_KIND, runner.STACK_KIND):
        try:
            found = await runner.find_runner(kind)
        except runner.RunnerError:
            # Docker cannot be asked: the reading of the stack would not be one either.
            return True
        if found is not None and found.is_running:
            return True
    return False


def build_detectors(settings: Settings, queue: JobQueue | None) -> dict[Event, Detector]:
    """The detectors of the events that are looked for (all but `job_failed`)."""
    config_dir = settings.papaia_config_dir
    workspace_dir = settings.papaia_workspace_dir

    async def backups_late(_: EventSettings) -> list[Finding]:
        return await asyncio.to_thread(backup_overdue, config_dir, now=datetime.now(tz=UTC))

    async def services_down(_: EventSettings) -> list[Finding]:
        # While the stack is being worked on, or when Docker does not answer, nothing is
        # concluded and nothing is forgotten: the services seen running stay as they were.
        if await stack_is_busy(queue):
            return []
        snapshot = await asyncio.to_thread(read_services, config_dir, workspace_dir)
        if snapshot is None:
            return []
        was_up = frozenset(load_state(config_dir).up)
        findings, up_now = service_findings(snapshot, was_up)
        if up_now != was_up:

            def remember(state: NotifyState) -> None:
                state.up = set(up_now)

            update_state(config_dir, remember)
        return findings

    async def certs_expiring(event_settings: EventSettings) -> list[Finding]:
        health = await host_health.load_host_health(
            config_dir=config_dir, workspace_dir=workspace_dir
        )
        return certificates(health, lead_days=event_settings.cert_lead_days)

    async def updates(_: EventSettings) -> list[Finding]:
        findings: list[Finding] = []
        newer = await upgrade.newer_releases(workspace_dir=workspace_dir, config_dir=config_dir)
        current = upgrade.current_version(config_dir, workspace_dir).current
        findings += core_update(current, newer)
        findings += await asyncio.to_thread(addon_updates, config_dir, workspace_dir)
        return findings

    return {
        Event.BACKUP_OVERDUE: backups_late,
        Event.SERVICE_UNHEALTHY: services_down,
        Event.CERT_EXPIRING: certs_expiring,
        Event.UPDATE_AVAILABLE: updates,
    }
