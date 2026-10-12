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
from app.core.notifications.store import NotifyState, Seen
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


def service_unhealthy(config_dir: str, workspace_dir: str) -> list[Finding]:
    """Modules of the stack that are stopped or whose health check fails.

    `missing` is left out (a module that is not deployed is a choice, and normal after a
    clean-up) and so is `starting`. Blocking: it asks Docker."""
    snapshot = services.load_snapshot(config_dir, workspace_dir)
    if not snapshot.core and not snapshot.addons:
        # Docker did not answer (the snapshot is empty then) or nothing is deployed.
        return []
    bad = (services.ServiceHealth.UNHEALTHY, services.ServiceHealth.STOPPED)
    conditions: list[Condition] = []
    for module in [*snapshot.core, *snapshot.addons]:
        if module.health not in bad:
            continue
        label = f"{module.addon}/{module.name}" if module.addon else module.name
        conditions.append(
            Condition(
                Event.SERVICE_UNHEALTHY,
                label,
                "",
                label,
                f"{label} is {module.health.value}: {module.summary}.",
                {
                    "service": module.name,
                    "add_on": module.addon or None,
                    "state": module.health.value,
                    "summary": module.summary,
                },
            )
        )
    return [Finding(Event.SERVICE_UNHEALTHY, tuple(conditions))]


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
        if await stack_is_busy(queue):
            return []
        return await asyncio.to_thread(service_unhealthy, config_dir, workspace_dir)

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
