"""The widgets that ship with the manager.

Each one reads what the manager already knows -- the backup catalogue, the host check, the
container snapshot, the last upgrade check -- and says it in the widget contract. None
starts a measurement of its own on a poll that the rest of the manager does not already
make: the host check and the container snapshot have caches and a single flight of their
own, and these providers sit behind the same ones the status row uses.

A provider returns a *document* (a dict), not a typed payload, on purpose. The runtime
parses it with the same function it would use for a widget from outside, so a built-in
that drifts from the contract fails here, in the tests, instead of being the one widget
nobody checks.

Verdicts are taken as they are. The host check's thresholds are the core's, the backup
schedule's overdue margin is the Backup page's, and the service states are the status
row's: nothing here decides what counts as bad.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from typing import Any

from app.config import Settings
from app.core import backups, host_health, schedule, services, upgrade
from app.core.host_health import HostState
from app.core.notifications import detect
from app.core.notifications.model import Event
from app.core.notifications.store import load_config, load_state
from app.core.services import ServiceHealth
from app.core.widgets.model import WIDGET_API, State
from app.core.widgets.registry import WidgetContext, WidgetType, WidgetUnavailableError, register
from app.i18n import N_, gettext, ngettext, npgettext

_HOST_STATE: dict[HostState, State] = {
    HostState.OK: State.OK,
    HostState.WARN: State.WARN,
    HostState.CRITICAL: State.CRITICAL,
    HostState.UNKNOWN: State.UNKNOWN,
}

# Lower is worse, for picking the reading that matters most.
_HOST_RANK: dict[HostState, int] = {
    HostState.CRITICAL: 0,
    HostState.WARN: 1,
    HostState.UNKNOWN: 2,
    HostState.OK: 3,
}

# The status row's reading of a module, in this contract's words. A module that is `starting`
# is on its way and a one-shot that `completed` did its job; a module that is `missing` (never
# brought up, or removed) is as much out of service as one that stopped.
_SERVICE_STATE: dict[ServiceHealth, State] = {
    ServiceHealth.HEALTHY: State.OK,
    ServiceHealth.COMPLETED: State.OK,
    ServiceHealth.STARTING: State.WARN,
    ServiceHealth.UNKNOWN: State.UNKNOWN,
    ServiceHealth.UNHEALTHY: State.WARN,
    ServiceHealth.STOPPED: State.CRITICAL,
    ServiceHealth.MISSING: State.CRITICAL,
}

_EVENT_LABEL: dict[Event, str] = {
    Event.BACKUP_OVERDUE: N_("Backup"),
    Event.SERVICE_UNHEALTHY: N_("Services"),
    Event.CERT_EXPIRING: N_("Certificates"),
    Event.UPDATE_AVAILABLE: N_("Updates"),
}

_NO_HOST_CHECK = (
    N_("This papAIa core does not provide the host check yet. Upgrade the core to 1.4.0 or newer.")
)


def _document(
    view: str,
    state: State,
    data: dict[str, Any],
    *,
    now: datetime,
    link: tuple[str, str] | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "widget_api": WIDGET_API,
        "view": view,
        "state": state.value,
        "data": data,
        "updated_at": now.isoformat(),
    }
    if link is not None:
        document["link"] = {"label": link[0], "href": link[1]}
    return document


def _unit_word(kind: str, count: int) -> str:
    """The word for a unit of time in the number it needs: "day" or "days"."""
    # With a context: "hour" and "minute" are also field names, translated as singular words.
    if kind == "day":
        return npgettext("unit of time", "day", "days", count)
    if kind == "hour":
        return npgettext("unit of time", "hour", "hours", count)
    return npgettext("unit of time", "minute", "minutes", count)


def _span(delta: timedelta) -> tuple[str, int, str]:
    """`("3", 3, "day")`: how long as shown, as a count, and its unit; at the precision a person
    reads."""
    seconds = max(int(delta.total_seconds()), 0)
    for unit, size in (("day", 86_400), ("hour", 3_600), ("minute", 60)):
        if seconds >= size:
            count = seconds // size
            return str(count), count, unit
    return "<1", 1, "minute"


def _limit(delta: timedelta) -> tuple[str, int, str]:
    """`("36", 36, "hour")`: a limit in the largest unit that holds it exactly.

    An age is rounded down (3 days and 2 hours is "3 days"); a limit is a promise, and "within
    1 day" for 36 hours would be a different one.
    """
    seconds = max(int(delta.total_seconds()), 0)
    for unit, size in (("day", 86_400), ("hour", 3_600), ("minute", 60)):
        if seconds >= size and seconds % size == 0:
            count = seconds // size
            return str(count), count, unit
    return _span(delta)


def _overdue_label(limit: str, count: int, kind: str) -> str:
    """One whole sentence per unit: the unit is a noun that a translation has to inflect."""
    if kind == "day":
        text = ngettext(
            "Overdue. The schedule expects one within %(limit)s day.",
            "Overdue. The schedule expects one within %(limit)s days.",
            count,
        )
    elif kind == "hour":
        text = ngettext(
            "Overdue. The schedule expects one within %(limit)s hour.",
            "Overdue. The schedule expects one within %(limit)s hours.",
            count,
        )
    else:
        text = ngettext(
            "Overdue. The schedule expects one within %(limit)s minute.",
            "Overdue. The schedule expects one within %(limit)s minutes.",
            count,
        )
    return text % {"limit": limit}


def _needs_host_check(settings: Settings) -> str | None:
    if host_health.is_supported(settings.papaia_workspace_dir):
        return None
    return gettext(_NO_HOST_CHECK)


# ---------------------------------------------------------------------------
# core-version
# ---------------------------------------------------------------------------


async def _core_version(context: WidgetContext) -> dict[str, Any]:
    config_dir = context.settings.papaia_config_dir
    workspace_dir = context.settings.papaia_workspace_dir
    version = await asyncio.to_thread(upgrade.current_version, config_dir, workspace_dir)
    # The cached check only, never a fetch: it is the Upgrade page's, and it exists once
    # somebody has opened that page in this process. A check made for another version than the
    # one installed now belongs to a deployment that has since moved.
    check = upgrade.cached_check()
    current = version.current

    if not current:
        message, state = gettext("unknown"), State.UNKNOWN
        label = gettext("No version is recorded for this installation.")
    elif version.mismatch:
        message, state = current, State.WARN
        label = (
            gettext(
                "The installed files are %(checkout)s; the configuration was last set up for "
                "%(recorded)s."
            ) % {"checkout": version.checkout, "recorded": version.recorded}
        )
    elif check is None or check.current != current:
        message, state = current, State.OK
        label = gettext("No update check has run since the manager started.")
    elif check.up_to_date:
        message, state, label = current, State.OK, gettext("Up to date.")
    else:
        message, state = current, State.WARN
        label = (
            gettext("Update to %(target)s is available.") % {"target": check.target}
            if check.target
            else gettext("A newer release is available.")
        )
    return _document(
        "status",
        state,
        {"message": message, "label": label},
        now=context.now,
        link=(gettext("Open upgrade"), "/upgrade"),
    )


# ---------------------------------------------------------------------------
# backup-age
# ---------------------------------------------------------------------------


def _backup_age_sync(config_dir: str, now: datetime) -> dict[str, Any]:
    backup_dir = backups.resolve_backup_dir(config_dir)
    if not backups.is_reachable(backup_dir):
        raise WidgetUnavailableError(gettext(
            "The backup directory is not reachable from the manager."
        ))
    points = backups.load_restore_points(backup_dir)
    load = schedule.load_schedule(config_dir)
    status = schedule.evaluate(load.schedule, points, now=now, next_run=None)
    link = (gettext("Open backups"), "/backup")

    if status.age is None:
        if not points:
            return _document(
                "stat",
                State.UNKNOWN,
                {"value": gettext("none"), "label": gettext("No backup has been taken yet.")},
                now=now,
                link=link,
            )
        return _document(
            "stat",
            State.WARN,
            {
                "value": gettext("none"),
                "label": (
                    gettext(
                        "No backup has succeeded yet; the latest one ended %(value)s."
                    ) % {"value": status.latest_result or gettext("without a result")}
                ),
            },
            now=now,
            link=link,
        )

    value, count, kind = _span(status.age)
    unit = _unit_word(kind, count)
    if status.overdue and status.overdue_after is not None:
        limit, limit_count, limit_kind = _limit(status.overdue_after)
        label = _overdue_label(limit, limit_count, limit_kind)
        state = State.WARN
    else:
        label, state = gettext("Last successful backup."), State.OK
    return _document(
        "stat", state, {"value": value, "unit": unit, "label": label}, now=now, link=link
    )


async def _backup_age(context: WidgetContext) -> dict[str, Any]:
    config_dir = context.settings.papaia_config_dir
    return await asyncio.to_thread(_backup_age_sync, config_dir, context.now)


# ---------------------------------------------------------------------------
# disk-space, cert-expiry (the host check)
# ---------------------------------------------------------------------------


async def _host(context: WidgetContext) -> host_health.HostHealth:
    health = await host_health.load_host_health(
        config_dir=context.settings.papaia_config_dir,
        workspace_dir=context.settings.papaia_workspace_dir,
    )
    if not health.available:
        raise WidgetUnavailableError(health.reason or gettext("The host check is not available."))
    return health


async def _disk_space(context: WidgetContext) -> dict[str, Any]:
    health = await _host(context)
    disks = [disk for disk in health.disks if disk.used_percent is not None]
    if not disks:
        raise WidgetUnavailableError(gettext("The host check reported no disks."))
    # The one that needs attention most: the worst verdict, the fullest among equals. The
    # verdict is the core's (its thresholds are free bytes, not a percentage); the percentage
    # is only what the bar is drawn from.
    disk = min(disks, key=lambda d: (_HOST_RANK[d.state], -(d.used_percent or 0.0)))
    label = disk.label
    if disk.free_text and disk.total_text:
        label = gettext("%(label)s: %(free)s free of %(total)s") % {
            "label": disk.label,
            "free": disk.free_text,
            "total": disk.total_text,
        }
    return _document(
        "progress",
        _HOST_STATE[disk.state],
        {"value": disk.used_percent, "max": 100, "unit": "%", "label": label},
        now=context.now,
        link=(gettext("Open host"), "/host"),
    )


async def _cert_expiry(context: WidgetContext) -> dict[str, Any]:
    health = await _host(context)
    certs = [cert for cert in health.certs if cert.days_left is not None]
    link = (gettext("Open host"), "/host")
    if not certs:
        return _document(
            "stat",
            State.UNKNOWN,
            {
                "value": gettext("none"),
                "label": gettext("The host check sees no certificate to watch."),
            },
            now=context.now,
            link=link,
        )
    nearest = min(certs, key=lambda cert: cert.days_left if cert.days_left is not None else 0)
    worst = min((cert.state for cert in certs), key=_HOST_RANK.__getitem__)
    days = nearest.days_left if nearest.days_left is not None else 0
    if days < 0:
        label = ngettext(
            "%(label)s expired %(days)s day ago.",
            "%(label)s expired %(days)s days ago.",
            -days,
        ) % {"label": nearest.label, "days": -days}
        data = {"value": gettext("expired"), "label": label}
    else:
        unit = npgettext("unit of time", "day", "days", days)
        data = {"value": str(days), "unit": unit, "label": nearest.label}
    return _document("stat", _HOST_STATE[worst], data, now=context.now, link=link)


# ---------------------------------------------------------------------------
# services-health
# ---------------------------------------------------------------------------


async def _services_health(context: WidgetContext) -> dict[str, Any]:
    snapshot = await asyncio.to_thread(
        services.load_snapshot,
        context.settings.papaia_config_dir,
        context.settings.papaia_workspace_dir,
    )
    modules = [*snapshot.core, *snapshot.addons]
    if not modules:
        # Docker did not answer, or nothing is deployed: neither says anything about a service.
        raise WidgetUnavailableError(gettext("Docker did not report any service."))
    counts = services.count_by_health(modules)
    running = counts[ServiceHealth.HEALTHY] + counts[ServiceHealth.COMPLETED]
    # An empty section is "no information", which must not drag a stack without add-ons down.
    verdict = services.worst(
        services.overall_health(section) for section in (snapshot.core, snapshot.addons) if section
    )
    attention = len(modules) - running
    label = (
        gettext("Every service is running.")
        if attention == 0
        else gettext("%(count)d need attention.") % {"count": attention}
    )
    return _document(
        "stat",
        _SERVICE_STATE[verdict],
        {"value": f"{running}/{len(modules)}", "unit": gettext("running"), "label": label},
        now=context.now,
        link=(gettext("Open services"), "/services"),
    )


# ---------------------------------------------------------------------------
# open-warnings
# ---------------------------------------------------------------------------


async def _open_warnings(context: WidgetContext) -> dict[str, Any]:
    """What the notification checks find now, as a count and the kinds of finding.

    The same functions the notifications use, so the card and the mail cannot disagree
    about what is wrong. It reads the caches and the files they read and starts no fetch:
    the update check is the cached one, and add-on updates come from the catalogues on disk.
    """
    config_dir = context.settings.papaia_config_dir
    workspace_dir = context.settings.papaia_workspace_dir
    findings: list[detect.Finding] = []

    findings += await asyncio.to_thread(detect.backup_overdue, config_dir, now=context.now)

    # While the stack is being worked on a reading of it shows the operation, not a fault.
    if not await detect.stack_is_busy(context.queue):
        snapshot = await asyncio.to_thread(detect.read_services, config_dir, workspace_dir)
        if snapshot is not None:
            was_up = frozenset(load_state(config_dir).up)
            found, _ = detect.service_findings(snapshot, was_up)
            findings += found

    if host_health.is_supported(workspace_dir):
        health = await host_health.load_host_health(
            config_dir=config_dir, workspace_dir=workspace_dir
        )
        lead_days = load_config(config_dir).config.settings.cert_lead_days
        findings += detect.certificates(health, lead_days=lead_days)

    current = (await asyncio.to_thread(upgrade.current_version, config_dir, workspace_dir)).current
    check = upgrade.cached_check()
    newer = check.available if check is not None and check.current == current else None
    findings += detect.core_update(current, newer)
    findings += await asyncio.to_thread(detect.addon_updates, config_dir, workspace_dir)

    total = sum(len(finding.conditions) for finding in findings)
    kinds = [
        gettext(_EVENT_LABEL[event])
        for event in _EVENT_LABEL
        if any(f.event is event and f.conditions for f in findings)
    ]
    if total == 0:
        data = {
            "value": "0",
            "unit": gettext("open"),
            "label": gettext("Nothing needs attention."),
        }
        state = State.OK
    else:
        data = {"value": str(total), "unit": gettext("open"), "label": ", ".join(kinds)}
        state = State.WARN
    return _document("stat", state, data, now=context.now)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

register(
    WidgetType(
        id="core-version",
        name=N_("papAIa version"),
        description=N_("The installed release, and whether the last check found a newer one."),
        icon="tag",
        view="status",
        provider=_core_version,
    )
)
register(
    WidgetType(
        id="backup-age",
        name=N_("Backup age"),
        description=N_("Time since the newest successful backup, against the schedule."),
        icon="archive",
        view="stat",
        provider=_backup_age,
    )
)
register(
    WidgetType(
        id="disk-space",
        name=N_("Disk space"),
        description=N_("The disk that needs attention most, as the host check sees it."),
        icon="server",
        view="progress",
        provider=_disk_space,
        visibility="all",
        requires=_needs_host_check,
    )
)
register(
    WidgetType(
        id="services-health",
        name="Services",
        description=N_("Running against expected services of the stack and its add-ons."),
        icon="heart",
        view="stat",
        provider=_services_health,
        refresh_seconds=30,
    )
)
register(
    WidgetType(
        id="open-warnings",
        name=N_("Open warnings"),
        description=N_("Everything the notification checks find, as one count."),
        icon="alert",
        view="stat",
        provider=_open_warnings,
        visibility="all",
    )
)
register(
    WidgetType(
        id="cert-expiry",
        name=N_("Certificate expiry"),
        description=N_("Days until the nearest certificate the host check sees expires."),
        icon="shield",
        view="stat",
        provider=_cert_expiry,
        requires=_needs_host_check,
    )
)
