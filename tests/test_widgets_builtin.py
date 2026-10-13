"""The built-in widgets: what each one says for what the manager knows.

Every document a provider returns goes through `parse_payload`, the parser the runtime uses
for any widget, so a built-in that drifts from the contract fails here and not on a dashboard.
The data sources are the manager's own (backup catalogue, host check, container snapshot,
upgrade check); they are set up on disk or replaced where the source is a process.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.config import Settings
from app.core import host_health, schedule, services, upgrade
from app.core.host_health import HostCheck, HostHealth, HostState
from app.core.notifications import detect
from app.core.notifications.model import Event
from app.core.services import (
    ServiceContainer,
    ServiceHealth,
    ServiceModule,
    StackSnapshot,
)
from app.core.widgets import registry
from app.core.widgets.model import State, WidgetPayload, parse_payload
from app.core.widgets.registry import WidgetContext, WidgetUnavailableError

NOW = datetime.now(tz=UTC)


class Deployment:
    """A configuration directory and a workspace to read from."""

    def __init__(self, tmp_path: Path) -> None:
        self.config_dir = tmp_path / "config"
        self.backup_dir = tmp_path / "backups"
        self.workspace_dir = tmp_path / "workspace"
        for directory in (self.config_dir, self.backup_dir, self.workspace_dir):
            directory.mkdir()
        (self.config_dir / ".env").write_text(
            f"PAPAIA_BACKUP_DIR={self.backup_dir}\n", encoding="utf-8"
        )
        self.settings = Settings(  # type: ignore[call-arg]
            oidc_issuer_kc_auth="https://kc.test/auth",
            oidc_issuer_kc_token="https://kc.test/token",
            oidc_issuer_kc_certs="https://kc.test/certs",
            manager_host="http://localhost:8120",
            manager_oidc_client_secret="secret",
            manager_session_secret="secret",
            papaia_config_dir=str(self.config_dir),
            papaia_workspace_dir=str(self.workspace_dir),
        )

    def catalogue(self, *entries: tuple[timedelta, str]) -> None:
        rows = []
        for i, (age, result) in enumerate(entries):
            taken = NOW - age
            rows.append(
                {
                    "id": taken.strftime("%Y-%m-%d_%H-%M-%S") + f"-{i}",
                    "created_at": taken.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "result": result,
                }
            )
        (self.backup_dir / "backup.yaml").write_text(
            yaml.safe_dump({"version": 1, "backups": rows}), encoding="utf-8"
        )

    def schedule(self, **fields: Any) -> None:
        fields.setdefault("cron", "0 3 * * *")
        fields.setdefault("timezone", "UTC")
        schedule.save_schedule(str(self.config_dir), schedule.build_schedule(**fields))

    def context(self) -> WidgetContext:
        return WidgetContext(settings=self.settings, now=NOW)


@pytest.fixture
def deployment(tmp_path: Path) -> Deployment:
    return Deployment(tmp_path)


async def _read(widget_id: str, deployment: Deployment) -> WidgetPayload:
    widget_type = registry.get(widget_id)
    assert widget_type is not None
    document = await widget_type.provider(deployment.context())
    return parse_payload(document)


# ---------------------------------------------------------------------------
# The registry
# ---------------------------------------------------------------------------


def test_the_built_in_widgets_are_registered() -> None:
    assert registry.known_ids() == {
        "core-version",
        "backup-age",
        "disk-space",
        "services-health",
        "open-warnings",
        "cert-expiry",
    }


def test_every_built_in_uses_a_view_and_an_icon_the_manager_has() -> None:
    for widget_type in registry.all_types():
        assert widget_type.icon in registry.ICON_PATHS
        assert widget_type.view in ("stat", "status", "progress", "list")


def test_the_defaults_of_the_widgets_for_everyone_and_for_administrators() -> None:
    by_id = {t.id: t.visibility for t in registry.all_types()}

    assert by_id["disk-space"] == "all"
    assert by_id["open-warnings"] == "all"
    assert by_id["backup-age"] == "admin"
    assert by_id["core-version"] == "admin"
    assert by_id["services-health"] == "admin"
    assert by_id["cert-expiry"] == "admin"


@pytest.fixture
def scratch_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(registry, "_TYPES", dict(registry._TYPES))  # noqa: SLF001


def _probe(**changes: Any) -> registry.WidgetType:
    async def provider(context: WidgetContext) -> dict[str, Any]:
        return {}

    fields: dict[str, Any] = {
        "id": "probe",
        "name": "Probe",
        "description": "",
        "icon": "tag",
        "view": "stat",
        "provider": provider,
    }
    fields.update(changes)
    return registry.WidgetType(**fields)


@pytest.mark.usefixtures("scratch_registry")
class TestRegistering:
    def test_a_good_type_is_added(self) -> None:
        registry.register(_probe())

        assert registry.get("probe") is not None

    @pytest.mark.parametrize("bad", ["Probe", "1probe", "has space", "", "a" * 41, "../x"])
    def test_an_id_that_is_not_a_slug_is_refused(self, bad: str) -> None:
        with pytest.raises(ValueError, match="must match"):
            registry.register(_probe(id=bad))

    def test_the_same_id_twice_is_refused(self) -> None:
        with pytest.raises(ValueError, match="twice"):
            registry.register(_probe(id="backup-age"))

    def test_an_icon_that_is_not_in_the_set_is_refused(self) -> None:
        with pytest.raises(ValueError, match="icon"):
            registry.register(_probe(icon="https://tracker.test/pixel.png"))

    def test_a_view_that_is_not_in_the_set_is_refused(self) -> None:
        with pytest.raises(ValueError, match="view"):
            registry.register(_probe(view="html"))


def test_a_type_that_needs_the_host_check_says_so_when_the_core_has_none(
    deployment: Deployment,
) -> None:
    by_id = {d["id"]: d for d in registry.describe(deployment.settings)}

    assert by_id["disk-space"]["available"] is False
    assert "host check" in by_id["disk-space"]["reason"]
    assert by_id["cert-expiry"]["available"] is False
    assert by_id["backup-age"]["available"] is True and by_id["backup-age"]["reason"] == ""


def test_a_type_that_needs_the_host_check_is_available_when_the_core_has_it(
    deployment: Deployment,
) -> None:
    doctor = deployment.workspace_dir / "papaia" / "tools" / "lib" / "doctor.py"
    doctor.parent.mkdir(parents=True)
    doctor.write_text("", encoding="utf-8")

    by_id = {d["id"]: d for d in registry.describe(deployment.settings)}

    assert by_id["disk-space"]["available"] is True


# ---------------------------------------------------------------------------
# backup-age
# ---------------------------------------------------------------------------


async def test_backup_age_without_a_reachable_directory_is_unavailable(
    deployment: Deployment,
) -> None:
    (deployment.config_dir / ".env").write_text("", encoding="utf-8")

    with pytest.raises(WidgetUnavailableError, match="backup directory"):
        await _read("backup-age", deployment)


async def test_backup_age_with_an_empty_catalogue_is_not_a_warning(
    deployment: Deployment,
) -> None:
    payload = await _read("backup-age", deployment)

    assert payload.state is State.UNKNOWN and payload.data.value == "none"  # type: ignore[union-attr]


async def test_backup_age_says_how_old_the_newest_good_backup_is(deployment: Deployment) -> None:
    deployment.catalogue((timedelta(days=3, hours=2), "ok"), (timedelta(days=9), "ok"))

    payload = await _read("backup-age", deployment)

    assert payload.state is State.OK
    assert (payload.data.value, payload.data.unit) == ("3", "days")  # type: ignore[union-attr]
    assert payload.link is not None and payload.link.href == "/backup"


async def test_backup_age_counts_a_single_day_in_the_singular(deployment: Deployment) -> None:
    deployment.catalogue((timedelta(hours=30), "ok"))

    payload = await _read("backup-age", deployment)

    assert (payload.data.value, payload.data.unit) == ("1", "day")  # type: ignore[union-attr]


async def test_backup_age_counts_hours_and_minutes(deployment: Deployment) -> None:
    deployment.catalogue((timedelta(hours=5, minutes=3), "ok"))
    assert (await _read("backup-age", deployment)).data.unit == "hours"  # type: ignore[union-attr]

    deployment.catalogue((timedelta(minutes=20), "ok"))
    assert (await _read("backup-age", deployment)).data.unit == "minutes"  # type: ignore[union-attr]


async def test_a_partial_backup_is_not_a_successful_one(deployment: Deployment) -> None:
    deployment.catalogue((timedelta(hours=1), "partial"))

    payload = await _read("backup-age", deployment)

    assert payload.state is State.WARN and payload.data.value == "none"  # type: ignore[union-attr]
    assert "partial" in payload.data.label  # type: ignore[union-attr]


async def test_backup_age_is_overdue_against_the_schedule(deployment: Deployment) -> None:
    deployment.schedule()
    deployment.catalogue((timedelta(days=5), "ok"))

    payload = await _read("backup-age", deployment)

    assert payload.state is State.WARN
    assert "Overdue" in payload.data.label  # type: ignore[union-attr]


async def test_the_overdue_limit_is_not_rounded_into_another_promise(
    deployment: Deployment,
) -> None:
    """A daily schedule is overdue after 1.5 days, and that is 36 hours, not "1 day"."""
    deployment.schedule()
    deployment.catalogue((timedelta(days=5), "ok"))

    payload = await _read("backup-age", deployment)

    assert payload.data.label == "Overdue. The schedule expects one within 36 hours."  # type: ignore[union-attr]


async def test_backup_age_is_fine_within_the_schedule(deployment: Deployment) -> None:
    deployment.schedule()
    deployment.catalogue((timedelta(hours=2), "ok"))

    assert (await _read("backup-age", deployment)).state is State.OK


async def test_backup_age_without_a_schedule_is_never_overdue(deployment: Deployment) -> None:
    deployment.catalogue((timedelta(days=400), "ok"))

    assert (await _read("backup-age", deployment)).state is State.OK


# ---------------------------------------------------------------------------
# core-version
# ---------------------------------------------------------------------------


def _version(
    monkeypatch: pytest.MonkeyPatch,
    *,
    recorded: str = "1.5.0",
    checkout: str = "1.5.0",
    check: upgrade.UpgradeCheck | None = None,
) -> None:
    monkeypatch.setattr(
        upgrade, "current_version", lambda c, w: upgrade.VersionState(recorded, checkout)
    )
    monkeypatch.setattr(upgrade, "cached_check", lambda: check)


async def test_core_version_without_a_check_says_none_has_run(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _version(monkeypatch)

    payload = await _read("core-version", deployment)

    assert payload.state is State.OK and payload.data.message == "1.5.0"  # type: ignore[union-attr]
    assert "No update check" in payload.data.label  # type: ignore[union-attr]


async def test_core_version_up_to_date(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _version(monkeypatch, check=upgrade.UpgradeCheck(current="1.5.0", status="up-to-date"))

    payload = await _read("core-version", deployment)

    assert payload.state is State.OK and payload.data.label == "Up to date."  # type: ignore[union-attr]


async def test_core_version_with_an_update_on_offer(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _version(
        monkeypatch,
        check=upgrade.UpgradeCheck(current="1.5.0", target="1.6.0", status="upgrade"),
    )

    payload = await _read("core-version", deployment)

    assert payload.state is State.WARN
    assert payload.data.label == "Update to 1.6.0 is available."  # type: ignore[union-attr]
    assert payload.link is not None and payload.link.href == "/upgrade"


async def test_a_check_made_for_another_version_is_ignored(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The deployment moved since: that verdict belongs to the version it was made for."""
    _version(
        monkeypatch,
        check=upgrade.UpgradeCheck(current="1.4.0", target="1.5.0", status="upgrade"),
    )

    payload = await _read("core-version", deployment)

    assert payload.state is State.OK
    assert "No update check" in payload.data.label  # type: ignore[union-attr]


async def test_core_version_when_the_files_and_the_record_disagree(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _version(monkeypatch, recorded="1.4.0", checkout="1.5.0")

    payload = await _read("core-version", deployment)

    assert payload.state is State.WARN
    assert "1.5.0" in payload.data.label and "1.4.0" in payload.data.label  # type: ignore[union-attr]


async def test_core_version_when_none_is_recorded(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _version(monkeypatch, recorded="", checkout="")

    payload = await _read("core-version", deployment)

    assert payload.state is State.UNKNOWN and payload.data.message == "unknown"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# disk-space and cert-expiry
# ---------------------------------------------------------------------------

_GIB = 1024**3


def _disk(key: str, label: str, used_percent: float, state: HostState) -> HostCheck:
    total = 100 * _GIB
    return HostCheck(
        kind="disk",
        key=key,
        label=label,
        detail=f"/srv/{key}",
        state=state,
        free_bytes=int(total * (1 - used_percent / 100)),
        total_bytes=total,
    )


def _cert(label: str, days: int, state: HostState = HostState.OK) -> HostCheck:
    return HostCheck(
        kind="cert",
        key=f"certs/{label}",
        label=label,
        detail="Bundled",
        state=state,
        days_left=days,
        not_after="2026-12-01T00:00:00Z",
    )


def _host(
    monkeypatch: pytest.MonkeyPatch,
    *,
    disks: tuple[HostCheck, ...] = (),
    certs: tuple[HostCheck, ...] = (),
    available: bool = True,
    reason: str = "",
) -> None:
    health = HostHealth(available=available, reason=reason, disks=disks, certs=certs)

    async def load(**_: Any) -> HostHealth:
        return health

    monkeypatch.setattr(host_health, "load_host_health", load)


async def test_disk_space_shows_the_disk_with_the_worst_verdict(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The core's verdict decides: its thresholds are free bytes, not a percentage."""
    _host(
        monkeypatch,
        disks=(
            _disk("config_dir", "Config disk", 90, HostState.OK),
            _disk("backup_dir", "Backup disk", 50, HostState.WARN),
        ),
    )

    payload = await _read("disk-space", deployment)

    assert payload.state is State.WARN
    assert payload.data.percent == 50.0  # type: ignore[union-attr]
    assert payload.data.label.startswith("Backup disk")  # type: ignore[union-attr]


async def test_disk_space_shows_the_fullest_of_equals(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host(
        monkeypatch,
        disks=(
            _disk("config_dir", "Config disk", 40, HostState.OK),
            _disk("backup_dir", "Backup disk", 75, HostState.OK),
        ),
    )

    payload = await _read("disk-space", deployment)

    assert payload.data.label.startswith("Backup disk")  # type: ignore[union-attr]
    assert "free of" in payload.data.label  # type: ignore[union-attr]


async def test_disk_space_names_no_path(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The widget may be set for everyone, and a filesystem path is for administrators."""
    _host(monkeypatch, disks=(_disk("config_dir", "Config disk", 40, HostState.OK),))

    payload = await _read("disk-space", deployment)

    assert "/srv" not in payload.model_dump_json()


async def test_disk_space_when_the_host_check_is_unavailable(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host(monkeypatch, available=False, reason="doctor timed out")

    with pytest.raises(WidgetUnavailableError, match="doctor timed out"):
        await _read("disk-space", deployment)


async def test_disk_space_when_the_host_check_found_no_disk(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host(monkeypatch)

    with pytest.raises(WidgetUnavailableError, match="no disks"):
        await _read("disk-space", deployment)


async def test_cert_expiry_shows_the_nearest_certificate(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host(monkeypatch, certs=(_cert("far.crt", 80), _cert("near.crt", 12)))

    payload = await _read("cert-expiry", deployment)

    assert (payload.data.value, payload.data.unit, payload.data.label) == (  # type: ignore[union-attr]
        "12",
        "days",
        "near.crt",
    )
    assert payload.state is State.OK


async def test_cert_expiry_takes_the_worst_verdict_of_all_certificates(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host(
        monkeypatch,
        certs=(_cert("a.crt", 80, HostState.OK), _cert("b.crt", 20, HostState.WARN)),
    )

    assert (await _read("cert-expiry", deployment)).state is State.WARN


async def test_cert_expiry_for_a_certificate_that_has_expired(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host(monkeypatch, certs=(_cert("old.crt", -3, HostState.CRITICAL),))

    payload = await _read("cert-expiry", deployment)

    assert payload.state is State.CRITICAL
    assert payload.data.value == "expired" and "3 days ago" in payload.data.label  # type: ignore[union-attr]


async def test_cert_expiry_counts_one_day_in_the_singular(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host(monkeypatch, certs=(_cert("a.crt", 1),))

    assert (await _read("cert-expiry", deployment)).data.unit == "day"  # type: ignore[union-attr]


async def test_cert_expiry_without_a_certificate_is_not_a_warning(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _host(monkeypatch)

    payload = await _read("cert-expiry", deployment)

    assert payload.state is State.UNKNOWN and payload.data.value == "none"  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# services-health
# ---------------------------------------------------------------------------


def _module(name: str, *healths: ServiceHealth, addon: str = "") -> ServiceModule:
    containers = [
        ServiceContainer(
            name=f"{name}-{i}",
            service=name,
            role="",
            state="running",
            status_text="",
            health=health,
        )
        for i, health in enumerate(healths)
    ]
    return ServiceModule(name=name, containers=containers, addon=addon)


def _stack(
    monkeypatch: pytest.MonkeyPatch,
    core: list[ServiceModule],
    addons: list[ServiceModule] | None = None,
) -> None:
    snapshot = StackSnapshot(core=core, addons=addons or [])
    monkeypatch.setattr(services, "load_snapshot", lambda config_dir, workspace_dir: snapshot)


async def test_services_health_when_everything_runs(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stack(
        monkeypatch,
        [_module("keycloak", ServiceHealth.HEALTHY), _module("litellm", ServiceHealth.HEALTHY)],
    )

    payload = await _read("services-health", deployment)

    assert payload.state is State.OK
    assert (payload.data.value, payload.data.unit) == ("2/2", "running")  # type: ignore[union-attr]
    assert payload.data.label == "Every service is running."  # type: ignore[union-attr]


async def test_services_health_counts_a_finished_one_shot_as_running(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stack(
        monkeypatch,
        [_module("localai", ServiceHealth.COMPLETED, ServiceHealth.HEALTHY)],
    )

    payload = await _read("services-health", deployment)

    assert payload.state is State.OK and payload.data.value == "1/1"  # type: ignore[union-attr]


async def test_services_health_with_an_unhealthy_service(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stack(
        monkeypatch,
        [_module("keycloak", ServiceHealth.HEALTHY), _module("litellm", ServiceHealth.UNHEALTHY)],
    )

    payload = await _read("services-health", deployment)

    assert payload.state is State.WARN
    assert payload.data.value == "1/2" and payload.data.label == "1 need attention."  # type: ignore[union-attr]


async def test_services_health_with_a_stopped_service(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stack(monkeypatch, [_module("keycloak", ServiceHealth.STOPPED)])

    assert (await _read("services-health", deployment)).state is State.CRITICAL


async def test_services_health_names_no_service(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Like the status row: a plain user may learn that something is wrong, not what."""
    _stack(monkeypatch, [_module("secret-service-name", ServiceHealth.UNHEALTHY)])

    payload = await _read("services-health", deployment)

    assert "secret-service-name" not in payload.model_dump_json()


async def test_services_health_without_add_ons_is_not_dragged_down_by_their_absence(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stack(monkeypatch, [_module("keycloak", ServiceHealth.HEALTHY)], addons=[])

    assert (await _read("services-health", deployment)).state is State.OK


async def test_services_health_counts_the_add_ons_too(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stack(
        monkeypatch,
        [_module("keycloak", ServiceHealth.HEALTHY)],
        addons=[_module("paperless", ServiceHealth.STOPPED, addon="paperless")],
    )

    payload = await _read("services-health", deployment)

    assert payload.state is State.CRITICAL and payload.data.value == "1/2"  # type: ignore[union-attr]


async def test_services_health_when_docker_did_not_answer(
    deployment: Deployment, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stack(monkeypatch, [])

    with pytest.raises(WidgetUnavailableError, match="Docker"):
        await _read("services-health", deployment)


# ---------------------------------------------------------------------------
# open-warnings
# ---------------------------------------------------------------------------


def _condition(event: Event, key: str) -> detect.Condition:
    return detect.Condition(event, key, "", key, f"{key} needs attention.")


@pytest.fixture
def quiet(monkeypatch: pytest.MonkeyPatch) -> pytest.MonkeyPatch:
    """Every source of findings finds nothing; a test turns one of them on."""

    async def idle(_: Any) -> bool:
        return False

    monkeypatch.setattr(detect, "backup_overdue", lambda config_dir, now: [])
    monkeypatch.setattr(detect, "stack_is_busy", idle)
    monkeypatch.setattr(detect, "read_services", lambda config_dir, workspace_dir: None)
    monkeypatch.setattr(detect, "addon_updates", lambda config_dir, workspace_dir: [])
    monkeypatch.setattr(host_health, "is_supported", lambda workspace_dir: False)
    _version(monkeypatch)
    return monkeypatch


async def test_open_warnings_with_nothing_to_report(
    deployment: Deployment, quiet: pytest.MonkeyPatch
) -> None:
    payload = await _read("open-warnings", deployment)

    assert payload.state is State.OK
    assert (payload.data.value, payload.data.unit) == ("0", "open")  # type: ignore[union-attr]
    assert payload.data.label == "Nothing needs attention."  # type: ignore[union-attr]


async def test_open_warnings_counts_what_the_notification_checks_find(
    deployment: Deployment, quiet: pytest.MonkeyPatch
) -> None:
    quiet.setattr(
        detect,
        "backup_overdue",
        lambda config_dir, now: [
            detect.Finding(Event.BACKUP_OVERDUE, (_condition(Event.BACKUP_OVERDUE, "backup"),))
        ],
    )
    quiet.setattr(
        detect,
        "addon_updates",
        lambda config_dir, workspace_dir: [
            detect.Finding(
                Event.UPDATE_AVAILABLE,
                (
                    _condition(Event.UPDATE_AVAILABLE, "addon:paperless"),
                    _condition(Event.UPDATE_AVAILABLE, "addon:n8n"),
                ),
                scope="addon:",
            )
        ],
    )

    payload = await _read("open-warnings", deployment)

    assert payload.state is State.WARN
    assert payload.data.value == "3"  # type: ignore[union-attr]
    assert payload.data.label == "Backup, Updates"  # type: ignore[union-attr]


async def test_open_warnings_says_what_kind_and_no_more(
    deployment: Deployment, quiet: pytest.MonkeyPatch
) -> None:
    """Set for everyone, it tells a plain user the kinds of finding and nothing about them."""
    quiet.setattr(
        detect,
        "backup_overdue",
        lambda config_dir, now: [
            detect.Finding(
                Event.BACKUP_OVERDUE, (_condition(Event.BACKUP_OVERDUE, "the-secret-detail"),)
            )
        ],
    )

    assert "the-secret-detail" not in (await _read("open-warnings", deployment)).model_dump_json()


async def test_open_warnings_reads_the_stack_unless_it_is_being_worked_on(
    deployment: Deployment, quiet: pytest.MonkeyPatch
) -> None:
    snapshot = StackSnapshot(core=[_module("litellm", ServiceHealth.UNHEALTHY)])
    quiet.setattr(detect, "read_services", lambda config_dir, workspace_dir: snapshot)
    assert (await _read("open-warnings", deployment)).data.value == "1"  # type: ignore[union-attr]

    async def busy(_: Any) -> bool:
        return True

    quiet.setattr(detect, "stack_is_busy", busy)
    assert (await _read("open-warnings", deployment)).data.value == "0"  # type: ignore[union-attr]


async def test_open_warnings_includes_a_certificate_close_to_its_end(
    deployment: Deployment, quiet: pytest.MonkeyPatch
) -> None:
    quiet.setattr(host_health, "is_supported", lambda workspace_dir: True)
    _host(quiet, certs=(_cert("soon.crt", 3, HostState.WARN),))

    payload = await _read("open-warnings", deployment)

    assert payload.data.value == "1" and payload.data.label == "Certificates"  # type: ignore[union-attr]


async def test_open_warnings_includes_a_newer_core_release_from_the_last_check(
    deployment: Deployment, quiet: pytest.MonkeyPatch
) -> None:
    _version(
        quiet,
        check=upgrade.UpgradeCheck(current="1.5.0", target="1.6.0", available=["1.6.0"]),
    )

    payload = await _read("open-warnings", deployment)

    assert payload.data.value == "1" and payload.data.label == "Updates"  # type: ignore[union-attr]


async def test_open_warnings_does_not_ask_the_network_about_updates(
    deployment: Deployment, quiet: pytest.MonkeyPatch
) -> None:
    """A poll must not cause a fetch: the update check is the Upgrade page's."""

    async def forbidden(**_: Any) -> list[str] | None:
        raise AssertionError("a poll must not fetch release tags")

    quiet.setattr(upgrade, "newer_releases", forbidden)

    await _read("open-warnings", deployment)
