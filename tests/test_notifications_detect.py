"""What calls for a notification, and which of it is new.

`decide` holds every rule about repeating and staying quiet, as a function of what it is handed,
so it is tested here without a clock, a stack or a mail server. The detectors are tested against
stand-ins for what they read.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from app.core import backups, host_health, schedule, services
from app.core.notifications import detect
from app.core.notifications.detect import Condition, Finding, decide
from app.core.notifications.model import Event, EventSettings
from app.core.notifications.store import NotifyState, Seen
from app.core.resolve import AddonUpdate

NOW = datetime(2026, 10, 12, 14, 0, tzinfo=UTC)
SETTINGS = EventSettings(debounce_minutes=30)


def _cond(event: Event, key: str, marker: str = "") -> Condition:
    return Condition(event, key, marker, key, f"{key} message")


def _finding(event: Event, *conditions: Condition, scope: str = "") -> Finding:
    return Finding(event, conditions, scope)


def _decide(
    findings: list[Finding], state: NotifyState, *, minutes: float = 0.0
) -> list[Condition]:
    return decide(findings, state, now=NOW + timedelta(minutes=minutes), settings=SETTINGS)


def _keys(conditions: list[Condition]) -> list[str]:
    return [c.key for c in conditions]


# -- an edge: a backup that turns overdue --------------------------------------------


def test_an_overdue_backup_is_reported_once_while_it_stays_overdue() -> None:
    state = NotifyState()
    overdue = _finding(Event.BACKUP_OVERDUE, _cond(Event.BACKUP_OVERDUE, "backup"))
    assert _keys(_decide([overdue], state)) == ["backup"]
    assert _decide([overdue], state, minutes=5) == []
    assert _decide([overdue], state, minutes=24 * 60) == []


def test_a_backup_that_recovers_and_is_overdue_again_is_a_new_occurrence() -> None:
    state = NotifyState()
    overdue = _finding(Event.BACKUP_OVERDUE, _cond(Event.BACKUP_OVERDUE, "backup"))
    fine = _finding(Event.BACKUP_OVERDUE)
    assert _keys(_decide([overdue], state)) == ["backup"]
    assert _decide([fine], state, minutes=5) == []
    assert _keys(_decide([overdue], state, minutes=10)) == ["backup"]


# -- an edge with a debounce: a service that flaps -----------------------------------


def test_a_service_that_flaps_is_reported_once_per_window() -> None:
    state = NotifyState()
    down = _finding(Event.SERVICE_UNHEALTHY, _cond(Event.SERVICE_UNHEALTHY, "litellm"))
    up = _finding(Event.SERVICE_UNHEALTHY)
    assert _keys(_decide([down], state, minutes=0)) == ["litellm"]
    assert _decide([up], state, minutes=5) == []
    assert _decide([down], state, minutes=10) == []  # inside the window: quiet
    assert _decide([up], state, minutes=15) == []
    assert _decide([down], state, minutes=20) == []  # still inside
    assert _decide([up], state, minutes=25) == []
    assert _keys(_decide([down], state, minutes=35)) == ["litellm"]  # a new window


def test_a_service_that_stays_down_past_the_window_is_not_reported_again() -> None:
    state = NotifyState()
    down = _finding(Event.SERVICE_UNHEALTHY, _cond(Event.SERVICE_UNHEALTHY, "litellm"))
    assert _keys(_decide([down], state, minutes=0)) == ["litellm"]
    assert _decide([down], state, minutes=31) == []
    assert _decide([down], state, minutes=300) == []


def test_two_services_are_two_conditions_each_with_its_own_window() -> None:
    state = NotifyState()
    both = _finding(
        Event.SERVICE_UNHEALTHY,
        _cond(Event.SERVICE_UNHEALTHY, "litellm"),
        _cond(Event.SERVICE_UNHEALTHY, "librechat"),
    )
    only_one = _finding(Event.SERVICE_UNHEALTHY, _cond(Event.SERVICE_UNHEALTHY, "librechat"))
    assert _keys(_decide([both], state)) == ["litellm", "librechat"]
    assert _decide([only_one], state, minutes=5) == []
    # litellm comes back down inside its window, librechat never left.
    assert _decide([both], state, minutes=10) == []


def test_a_window_is_the_one_the_settings_name() -> None:
    state = NotifyState()
    down = _finding(Event.SERVICE_UNHEALTHY, _cond(Event.SERVICE_UNHEALTHY, "litellm"))
    up = _finding(Event.SERVICE_UNHEALTHY)
    short = EventSettings(debounce_minutes=5)
    decide([down], state, now=NOW, settings=short)
    decide([up], state, now=NOW + timedelta(minutes=1), settings=short)
    new = decide([down], state, now=NOW + timedelta(minutes=6), settings=short)
    assert _keys(new) == ["litellm"]


# -- a marker: a version or an expiry date -------------------------------------------


def test_an_update_is_reported_once_per_version() -> None:
    state = NotifyState()
    v150 = _finding(
        Event.UPDATE_AVAILABLE, _cond(Event.UPDATE_AVAILABLE, "core", "1.5.0"), scope="core"
    )
    v151 = _finding(
        Event.UPDATE_AVAILABLE, _cond(Event.UPDATE_AVAILABLE, "core", "1.5.1"), scope="core"
    )
    assert _keys(_decide([v150], state)) == ["core"]
    assert _decide([v150], state, minutes=60) == []
    assert _keys(_decide([v151], state, minutes=120)) == ["core"]


def test_an_upgrade_forgets_the_update_so_the_next_one_is_news() -> None:
    state = NotifyState()
    v150 = _finding(
        Event.UPDATE_AVAILABLE, _cond(Event.UPDATE_AVAILABLE, "core", "1.5.0"), scope="core"
    )
    none_left = _finding(Event.UPDATE_AVAILABLE, scope="core")
    _decide([v150], state)
    assert _decide([none_left], state, minutes=60) == []
    assert "update_available|core" not in state.seen


def test_what_a_pass_could_not_read_is_not_cleared_by_one_that_could() -> None:
    """The core's release tags could not be fetched, the add-ons could be read: the add-on
    finding must not forget `core`, or the same release would be announced again."""
    state = NotifyState()
    core = _finding(
        Event.UPDATE_AVAILABLE, _cond(Event.UPDATE_AVAILABLE, "core", "1.5.0"), scope="core"
    )
    addons = _finding(Event.UPDATE_AVAILABLE, scope="addon:")
    _decide([core, addons], state)
    assert _decide([addons], state, minutes=60) == []  # the core was not looked at
    assert _decide([core, addons], state, minutes=120) == []  # and is not news when it is


def test_nothing_found_at_all_changes_nothing() -> None:
    state = NotifyState()
    down = _finding(Event.SERVICE_UNHEALTHY, _cond(Event.SERVICE_UNHEALTHY, "litellm"))
    _decide([down], state)
    before = {k: (v.active, v.marker, v.last_sent) for k, v in state.seen.items()}
    assert _decide([], state, minutes=5) == []
    assert {k: (v.active, v.marker, v.last_sent) for k, v in state.seen.items()} == before


def test_a_renewed_certificate_is_a_new_occurrence_and_the_same_one_is_not() -> None:
    state = NotifyState()
    expiring = _finding(
        Event.CERT_EXPIRING, _cond(Event.CERT_EXPIRING, "npm-1", "2026-10-20T00:00:00Z")
    )
    renewed_then_expiring = _finding(
        Event.CERT_EXPIRING, _cond(Event.CERT_EXPIRING, "npm-1", "2027-01-20T00:00:00Z")
    )
    fine = _finding(Event.CERT_EXPIRING)
    assert _keys(_decide([expiring], state)) == ["npm-1"]
    assert _decide([expiring], state, minutes=60 * 24) == []
    assert _decide([fine], state, minutes=60 * 48) == []  # renewed
    assert _keys(_decide([renewed_then_expiring], state, minutes=60 * 24 * 90)) == ["npm-1"]


def test_events_do_not_touch_each_others_memory() -> None:
    state = NotifyState()
    backup = _finding(Event.BACKUP_OVERDUE, _cond(Event.BACKUP_OVERDUE, "backup"))
    _decide([backup], state)
    assert _decide([_finding(Event.SERVICE_UNHEALTHY)], state, minutes=5) == []
    assert state.seen["backup_overdue|backup"].active


def test_a_condition_that_is_over_and_old_is_eventually_forgotten() -> None:
    state = NotifyState()
    old = (NOW - timedelta(days=45)).isoformat()
    state.seen["service_unhealthy|ghost"] = Seen(active=False, last_sent=old)
    state.seen["service_unhealthy|recent"] = Seen(active=False, last_sent=NOW.isoformat())
    _decide([_finding(Event.SERVICE_UNHEALTHY)], state)
    assert "service_unhealthy|ghost" not in state.seen
    assert "service_unhealthy|recent" in state.seen


# -- the detectors -------------------------------------------------------------------


def _point(age: timedelta, result: str = "ok") -> backups.RestorePoint:
    taken = NOW - age
    return backups.RestorePoint(
        id=f"p-{int(age.total_seconds())}",
        created_at=taken.strftime("%Y-%m-%dT%H:%M:%SZ"),
        result=result,
    )


def _backups(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    points: list[backups.RestorePoint],
    cron: str | None = "0 3 * * *",
    error: str | None = None,
    reachable: bool = True,
) -> None:
    found = schedule.build_schedule(cron=cron, timezone="UTC") if cron else None
    monkeypatch.setattr(schedule, "load_schedule", lambda _: schedule.ScheduleLoad(found, error))
    monkeypatch.setattr(backups, "resolve_backup_dir", lambda _: tmp_path)
    monkeypatch.setattr(backups, "is_reachable", lambda _: reachable)
    monkeypatch.setattr(backups, "load_restore_points", lambda _: points)


def test_a_backup_older_than_the_schedule_allows_is_overdue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _backups(monkeypatch, tmp_path, points=[_point(timedelta(days=3))])
    (finding,) = detect.backup_overdue("cfg", now=NOW)
    (condition,) = finding.conditions
    assert condition.key == "backup"
    assert "3 days" in condition.message
    assert condition.details["latest_result"] == "ok"


def test_a_recent_backup_is_not_overdue(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _backups(monkeypatch, tmp_path, points=[_point(timedelta(hours=5))])
    (finding,) = detect.backup_overdue("cfg", now=NOW)
    assert finding.conditions == ()


def test_without_a_schedule_nothing_is_overdue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _backups(monkeypatch, tmp_path, points=[_point(timedelta(days=90))], cron=None)
    (finding,) = detect.backup_overdue("cfg", now=NOW)
    assert finding.conditions == ()


def test_backups_that_never_succeeded_are_a_problem_in_their_own_words(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _backups(monkeypatch, tmp_path, points=[_point(timedelta(hours=2), "failed")])
    (finding,) = detect.backup_overdue("cfg", now=NOW)
    (condition,) = finding.conditions
    assert "No backup has succeeded yet" in condition.message
    assert "failed" in condition.message


def test_a_damaged_schedule_or_an_unreachable_directory_is_not_known(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _backups(monkeypatch, tmp_path, points=[], error="schedule.yaml cannot be read")
    assert detect.backup_overdue("cfg", now=NOW) == []
    _backups(monkeypatch, tmp_path, points=[], reachable=False)
    assert detect.backup_overdue("cfg", now=NOW) == []


def _module(
    name: str, health: services.ServiceHealth, *, addon: str = ""
) -> services.ServiceModule:
    container = services.ServiceContainer(
        name=f"{name}-1", service=name, role="", state="x", status_text="x", health=health
    )
    return services.ServiceModule(name=name, containers=[container], addon=addon)


_H = services.ServiceHealth


def _findings(
    snapshot: services.StackSnapshot, was_up: set[str] | None = None
) -> tuple[dict[str, Condition], frozenset[str]]:
    (finding,), up_now = detect.service_findings(snapshot, frozenset(was_up or ()))
    return {c.key: c for c in finding.conditions}, up_now


def test_stopped_and_unhealthy_modules_are_reported_and_the_rest_is_not() -> None:
    snapshot = services.StackSnapshot(
        core=[
            _module("keycloak", _H.HEALTHY),
            _module("litellm", _H.UNHEALTHY),
            _module("rag", _H.MISSING),
            _module("localai", _H.STARTING),
        ],
        addons=[_module("n8n", _H.STOPPED, addon="n8n")],
    )
    found, _ = _findings(snapshot)
    assert set(found) == {"litellm", "n8n/n8n"}
    assert found["litellm"].details["state"] == "unhealthy"
    assert found["n8n/n8n"].details["add_on"] == "n8n"
    assert found["litellm"].message.startswith("litellm is unhealthy: ")


def test_a_service_that_was_running_and_is_gone_is_an_outage() -> None:
    """Stopped with "Clean up", or `docker rm`: the profile stays active, the containers are
    gone, and the module is `missing` -- which is only an outage if it was running before."""
    snapshot = services.StackSnapshot(core=[_module("localai", _H.MISSING)])
    found, _ = _findings(snapshot, {"localai"})
    assert set(found) == {"localai"}
    assert found["localai"].message == "localai is not running: its containers are gone."
    assert found["localai"].details["state"] == "missing"
    assert found["localai"].details["service"] == "localai"


def test_a_service_that_was_never_seen_running_is_not_an_outage_when_it_is_missing() -> None:
    snapshot = services.StackSnapshot(core=[_module("rag", _H.MISSING)])
    found, up_now = _findings(snapshot, {"keycloak"})
    assert found == {}
    assert "rag" not in up_now


def test_an_add_on_that_was_running_and_is_gone_is_reported_under_its_own_label() -> None:
    snapshot = services.StackSnapshot(addons=[_module("n8n", _H.MISSING, addon="n8n")])
    found, _ = _findings(snapshot, {"n8n/n8n"})
    assert set(found) == {"n8n/n8n"}
    assert found["n8n/n8n"].details["add_on"] == "n8n"


def test_what_is_healthy_is_remembered_and_what_is_down_is_not_forgotten() -> None:
    snapshot = services.StackSnapshot(
        core=[
            _module("keycloak", _H.HEALTHY),
            _module("litellm", _H.UNHEALTHY),
            _module("localai", _H.MISSING),
            _module("localai-extra", _H.STARTING),
        ]
    )
    _, up_now = _findings(snapshot, {"litellm", "localai"})
    # `keycloak` joins; `litellm` and `localai` are down but stay known, so a later `missing`
    # is still an outage; `localai-extra` is only starting, which is deployed all the same.
    assert up_now == {"keycloak", "litellm", "localai", "localai-extra"}


def test_a_service_that_was_only_starting_when_it_was_last_seen_is_known() -> None:
    """The case of a service stopped before it passed its health check: a pass that saw it
    `starting` has seen it brought up, and its loss is an outage."""
    starting = services.StackSnapshot(core=[_module("localai", _H.STARTING)])
    found, up_now = _findings(starting)
    assert found == {} and up_now == {"localai"}
    gone = services.StackSnapshot(core=[_module("localai", _H.MISSING)])
    found, _ = _findings(gone, set(up_now))
    assert set(found) == {"localai"}


@pytest.mark.parametrize("health", [_H.COMPLETED, _H.UNKNOWN, _H.MISSING])
def test_a_one_shot_that_completed_or_a_module_nobody_can_read_is_not_known(
    health: services.ServiceHealth,
) -> None:
    """A job that ran and exited is not a service, and removing it later is no outage."""
    _, up_now = _findings(services.StackSnapshot(core=[_module("init", health)]))
    assert up_now == frozenset()


@pytest.mark.parametrize("health", [_H.HEALTHY, _H.STARTING, _H.UNHEALTHY, _H.STOPPED])
def test_a_module_in_a_state_that_says_it_was_brought_up_is_known(
    health: services.ServiceHealth,
) -> None:
    _, up_now = _findings(services.StackSnapshot(core=[_module("localai", health)]))
    assert up_now == {"localai"}


def test_a_module_that_left_the_stack_is_forgotten() -> None:
    """The profile was switched off or the add-on removed: it is no longer declared, so it is
    in no snapshot, and coming back later as `missing` must not be reported."""
    snapshot = services.StackSnapshot(core=[_module("keycloak", _H.HEALTHY)])
    found, up_now = _findings(snapshot, {"keycloak", "localai", "n8n/n8n"})
    assert found == {}
    assert up_now == {"keycloak"}


def test_a_service_that_comes_back_is_no_longer_a_condition() -> None:
    snapshot = services.StackSnapshot(core=[_module("localai", _H.HEALTHY)])
    found, up_now = _findings(snapshot, {"localai"})
    assert found == {} and up_now == {"localai"}


def test_the_message_of_a_partly_gone_module_says_how_much_is_gone() -> None:
    running = services.ServiceContainer(
        name="a-1", service="a", role="", state="x", status_text="x", health=_H.HEALTHY
    )
    gone = services.ServiceContainer(
        name="", service="b", role="", state="missing", status_text="x", health=_H.MISSING
    )
    module = services.ServiceModule(name="localai", containers=[running, gone])
    found, _ = _findings(services.StackSnapshot(core=[module]), {"localai"})
    assert found["localai"].message == "localai is not running (1 of 2 containers not deployed)."


def test_a_docker_that_does_not_answer_is_not_a_stack_without_problems(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(services, "load_snapshot", lambda *_: services.StackSnapshot())
    assert detect.read_services("cfg", "ws") is None


def test_the_stack_is_read_as_docker_reports_it(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot = services.StackSnapshot(core=[_module("keycloak", _H.HEALTHY)])
    monkeypatch.setattr(services, "load_snapshot", lambda *_: snapshot)
    assert detect.read_services("cfg", "ws") is snapshot


def test_a_missing_service_that_was_running_is_reported_once_like_any_edge() -> None:
    """The new condition goes through `decide` like the others: once, not again while it stays
    gone, and the debounce window holds for one that keeps coming and going."""
    state = NotifyState()
    gone_snapshot = services.StackSnapshot(core=[_module("localai", _H.MISSING)])
    up_snapshot = services.StackSnapshot(core=[_module("localai", _H.HEALTHY)])

    def pass_(snapshot: services.StackSnapshot, minutes: float) -> list[str]:
        findings, _ = detect.service_findings(snapshot, frozenset({"localai"}))
        return _keys(_decide(findings, state, minutes=minutes))

    assert pass_(gone_snapshot, 0) == ["localai"]
    assert pass_(gone_snapshot, 5) == []
    assert pass_(up_snapshot, 10) == []
    assert pass_(gone_snapshot, 15) == []  # back inside the 30 minute window
    assert pass_(up_snapshot, 20) == []
    assert pass_(gone_snapshot, 40) == ["localai"]  # a new window


def _cert(
    key: str, days: int | None, not_after: str = "2026-10-20T00:00:00Z"
) -> host_health.HostCheck:
    return host_health.HostCheck(
        kind="cert",
        key=key,
        label=f"cert {key}",
        detail="",
        state=host_health.HostState.OK,
        days_left=days,
        not_after=not_after,
    )


def test_certificates_inside_the_lead_time_are_reported_by_the_days_left() -> None:
    reading = host_health.HostHealth(
        available=True, certs=(_cert("a", 5), _cert("b", 14), _cert("c", 15), _cert("d", None))
    )
    (finding,) = detect.certificates(reading, lead_days=14)
    assert {c.key for c in finding.conditions} == {"a", "b"}
    by_key = {c.key: c for c in finding.conditions}
    assert by_key["a"].message == "cert a expires in 5 days."
    assert by_key["a"].marker == "2026-10-20T00:00:00Z"


@pytest.mark.parametrize(
    ("days", "text"),
    [(1, "expires in 1 day."), (0, "expires today."), (-3, "expired 3 days ago.")],
)
def test_the_wording_follows_the_days_left(days: int, text: str) -> None:
    reading = host_health.HostHealth(available=True, certs=(_cert("a", days),))
    (finding,) = detect.certificates(reading, lead_days=14)
    assert finding.conditions[0].message.endswith(text)


def test_no_host_reading_means_no_finding() -> None:
    assert detect.certificates(host_health.unavailable("no doctor"), lead_days=14) == []


def test_a_release_ahead_is_a_condition_with_the_newest_as_its_marker() -> None:
    (finding,) = detect.core_update("1.4.0", ["1.5.1", "1.5.0"])
    (condition,) = finding.conditions
    assert (condition.key, condition.marker, finding.scope) == ("core", "1.5.1", "core")
    assert "1.4.0" in condition.message and condition.details["releases_ahead"] == 2


def test_no_release_ahead_is_a_finding_with_no_condition_and_unknown_is_no_finding() -> None:
    (finding,) = detect.core_update("1.5.1", [])
    assert finding.conditions == () and finding.scope == "core"
    assert detect.core_update("1.5.1", None) == []


def test_add_on_updates_become_one_condition_each(monkeypatch: pytest.MonkeyPatch) -> None:
    installed: dict[str, Any] = {"paperless": object(), "n8n": object()}
    monkeypatch.setattr(detect, "load_installed", lambda _: installed)
    monkeypatch.setattr(detect, "load_registry", lambda _: object())
    monkeypatch.setattr(
        detect,
        "outdated_addons",
        lambda *_: [AddonUpdate("paperless", "1.0.0", "1.1.0", "fidonis")],
    )
    (finding,) = detect.addon_updates("cfg", "ws")
    (condition,) = finding.conditions
    assert (condition.key, condition.marker, finding.scope) == (
        "addon:paperless",
        "1.1.0",
        "addon:",
    )
    assert condition.details["installed"] == "1.0.0"


def test_without_installed_add_ons_the_catalogs_are_not_even_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(detect, "load_installed", lambda _: {})

    def explode(_: str) -> None:
        raise AssertionError("the catalog registry must not be read (it seeds a default)")

    monkeypatch.setattr(detect, "load_registry", explode)
    (finding,) = detect.addon_updates("cfg", "ws")
    assert finding.conditions == () and finding.scope == "addon:"
