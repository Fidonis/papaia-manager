"""The schedule's REST API, its two page fragments, and the scheduler's start.

The scheduler itself is faked here (it is exercised in test_scheduler.py): what is
asserted is what the routes hand it, what they write, and what they refuse.
"""
from __future__ import annotations

import base64
import json
import os
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

_SESSION_SECRET = "test-session-secret-value"

os.environ.update(
    OIDC_ISSUER_KC_AUTH="https://kc.test/auth",
    OIDC_ISSUER_KC_TOKEN="https://kc.test/token",
    OIDC_ISSUER_KC_CERTS="https://kc.test/certs",
    MANAGER_ADMIN_ROLE="admin",
    MANAGER_USER_ROLE="user",
    MANAGER_HOST="http://localhost:8120",
    MANAGER_OIDC_CLIENT_SECRET="client-secret",
    MANAGER_SESSION_SECRET=_SESSION_SECRET,
)

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app import main as app_main  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core import schedule  # noqa: E402
from app.core.jobs import JobQueue  # noqa: E402
from app.core.scheduler import BackupScheduler  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers import api_maintenance  # noqa: E402

_CSRF = "test-csrf-token"
_URL = "/api/v1/maintenance/schedule"
_HEADERS = {"X-CSRF-Token": _CSRF}


class _FakeScheduler:
    """Records what the routes ask of the scheduler."""

    def __init__(self, next_run: datetime | None = None) -> None:
        self.applied: list[schedule.BackupSchedule | None] = []
        self._next_run = next_run

    def apply(self, current: schedule.BackupSchedule | None) -> None:
        self.applied.append(current)

    def next_run_time(self) -> datetime | None:
        return self._next_run


class _Env:
    def __init__(self, tmp_path: Path) -> None:
        self.config_dir = tmp_path / "config"
        self.backup_dir = tmp_path / "backups"
        self.config_dir.mkdir()
        self.backup_dir.mkdir()
        self.scheduler = _FakeScheduler()

    def set_env(self, text: str) -> None:
        (self.config_dir / ".env").write_text(text, encoding="utf-8")

    def catalogue(self, *entries: tuple[timedelta, str]) -> None:
        rows = []
        for i, (age, result) in enumerate(entries):
            taken = datetime.now(tz=UTC) - age
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

    def save(self, **fields: Any) -> schedule.BackupSchedule:
        fields.setdefault("cron", "0 3 * * *")
        fields.setdefault("timezone", "UTC")
        saved = schedule.build_schedule(**fields)
        schedule.save_schedule(str(self.config_dir), saved)
        return saved

    def audit(self) -> list[dict[str, Any]]:
        path = self.config_dir / "manager" / "audit.log"
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    @property
    def schedule_file(self) -> Path:
        return schedule.schedule_path(str(self.config_dir))


async def _idle_runner(kind: object = None) -> None:
    return None


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[_Env]:
    environment = _Env(tmp_path)
    environment.set_env(f"PAPAIA_BACKUP_DIR={environment.backup_dir}\n")
    monkeypatch.setenv("PAPAIA_CONFIG_DIR", str(environment.config_dir))
    monkeypatch.setenv("PAPAIA_WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.delenv("TZ", raising=False)
    get_settings.cache_clear()
    monkeypatch.setattr(api_maintenance.runner, "find_runner", _idle_runner)
    app_main._job_queue = JobQueue(config_dir=str(environment.config_dir))  # noqa: SLF001
    app_main._backup_scheduler = environment.scheduler  # type: ignore[assignment]  # noqa: SLF001
    yield environment
    get_settings.cache_clear()
    app_main._job_queue = None  # noqa: SLF001
    app_main._backup_scheduler = None  # noqa: SLF001


@pytest.fixture
def client(env: _Env) -> TestClient:
    return TestClient(create_app(), follow_redirects=False)


def _login(client: TestClient, roles: list[str] | None = None) -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": "u-1",
            "preferred_username": "tester",
            "roles": roles if roles is not None else ["admin"],
            "exp": int(time.time()) + 3600,
        },
        "_csrf_token": _CSRF,
    }
    payload = base64.b64encode(json.dumps(session).encode())
    client.cookies.clear()
    client.cookies.set(
        "papaia_manager_session", TimestampSigner(_SESSION_SECRET).sign(payload).decode()
    )
    return client


def _admin(client: TestClient) -> TestClient:
    return _login(client)


_DAILY = {"cron": "0 3 * * *", "timezone": "UTC"}


# ---------------------------------------------------------------------------
# Who may, and what is checked first
# ---------------------------------------------------------------------------


def test_reading_without_a_schedule_says_so(client: TestClient) -> None:
    data = _admin(client).get(_URL).json()
    assert data["configured"] is False
    assert data["error"] is None
    assert data["schedule"] is None
    assert data["backup_dir_reachable"] is True


@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_a_user_without_the_admin_role_is_refused(client: TestClient, method: str) -> None:
    _login(client, ["user"])
    kwargs: dict[str, Any] = {"headers": _HEADERS}
    if method == "put":
        kwargs["json"] = _DAILY
    assert getattr(client, method)(_URL, **kwargs).status_code == 403


@pytest.mark.parametrize("method", ["get", "put", "delete"])
def test_a_request_without_a_session_gets_a_401(client: TestClient, method: str) -> None:
    kwargs: dict[str, Any] = {"headers": _HEADERS}
    if method == "put":
        kwargs["json"] = _DAILY
    response = getattr(client, method)(_URL, **kwargs)
    assert response.status_code == 401


def test_changing_the_schedule_needs_the_csrf_token(client: TestClient, env: _Env) -> None:
    _admin(client)
    assert client.put(_URL, json=_DAILY).status_code == 403
    assert client.delete(_URL).status_code == 403
    assert not env.schedule_file.exists()
    assert env.scheduler.applied == []


# ---------------------------------------------------------------------------
# Saving
# ---------------------------------------------------------------------------


def test_a_schedule_is_saved_audited_and_handed_to_the_scheduler(
    client: TestClient, env: _Env
) -> None:
    env.scheduler._next_run = datetime.now(tz=UTC) + timedelta(hours=3)  # noqa: SLF001
    env.catalogue((timedelta(hours=5), "ok"))

    response = _admin(client).put(
        _URL,
        json={**_DAILY, "retention_days": 14, "run_on_startup": "never"},
        headers=_HEADERS,
    )

    assert response.status_code == 200
    stored = yaml.safe_load(env.schedule_file.read_text(encoding="utf-8"))
    assert stored == {
        "enabled": True,
        "cron": "0 3 * * *",
        "timezone": "UTC",
        "retention_days": 14,
        "misfire_grace_seconds": 3600,
        "run_on_startup": "never",
    }
    assert [s.cron for s in env.scheduler.applied if s is not None] == ["0 3 * * *"]

    data = response.json()
    assert data["configured"] is True
    assert data["description"] == "Every day at 03:00"
    assert data["next_run"] is not None
    assert data["schedule"]["retention_days"] == 14
    assert data["preset"]["mode"] == "daily"

    entry = env.audit()[-1]
    assert (entry["user"], entry["action"], entry["result"]) == (
        "tester",
        "backup.schedule.update",
        "ok",
    )
    assert entry["params"]["cron"] == "0 3 * * *"
    assert entry["params"]["retention_days"] == 14


def test_a_numeric_weekday_is_stored_as_the_day_cron_means(
    client: TestClient, env: _Env
) -> None:
    response = _admin(client).put(
        _URL, json={"cron": "0 3 * * 1", "timezone": "UTC"}, headers=_HEADERS
    )
    assert response.status_code == 200
    assert response.json()["schedule"]["cron"] == "0 3 * * mon"
    assert response.json()["description"] == "Every Monday at 03:00"


def test_fields_that_are_left_out_take_their_defaults(client: TestClient, env: _Env) -> None:
    response = _admin(client).put(_URL, json={"cron": "30 4 * * *"}, headers=_HEADERS)
    assert response.status_code == 200
    data = response.json()["schedule"]
    assert data["timezone"] == "UTC"
    assert data["retention_days"] is None
    assert data["run_on_startup"] == "if_missed"


def test_the_default_timezone_follows_the_containers_tz(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("TZ", "Europe/Berlin")
    response = _admin(client).put(_URL, json={"cron": "30 4 * * *"}, headers=_HEADERS)
    assert response.json()["schedule"]["timezone"] == "Europe/Berlin"


def test_a_retention_of_null_keeps_every_restore_point(client: TestClient, env: _Env) -> None:
    env.save(retention_days=14)
    response = _admin(client).put(
        _URL, json={**_DAILY, "retention_days": None}, headers=_HEADERS
    )
    assert response.status_code == 200
    assert response.json()["schedule"]["retention_days"] is None


def test_a_paused_schedule_is_saved_and_unscheduled(client: TestClient, env: _Env) -> None:
    response = _admin(client).put(
        _URL, json={**_DAILY, "enabled": False}, headers=_HEADERS
    )
    assert response.status_code == 200
    assert response.json()["schedule"]["enabled"] is False
    applied = env.scheduler.applied[-1]
    assert applied is not None
    assert applied.enabled is False


def test_saving_works_while_the_scheduler_did_not_start(client: TestClient, env: _Env) -> None:
    app_main._backup_scheduler = None  # noqa: SLF001
    response = _admin(client).put(_URL, json=_DAILY, headers=_HEADERS)
    assert response.status_code == 200
    assert response.json()["next_run"] is None
    assert env.schedule_file.exists()


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ({"cron": "not a cron", "timezone": "UTC"}, "five fields"),
        ({"cron": "*/10 * * * *", "timezone": "UTC"}, "more often than once an hour"),
        ({"cron": "0 3 1 * mon", "timezone": "UTC"}, "either the day of the month"),
        ({"cron": "0 3 * * *", "timezone": "Mars/Base"}, "Unknown timezone"),
        ({"cron": "0 3 * * *", "timezone": "UTC", "retention_days": 1}, "at least 2 days"),
        ({"cron": "0 3 * * *", "timezone": "UTC", "retention_days": 0}, "retention_days"),
        ({"cron": "0 3 * * mon", "timezone": "UTC", "retention_days": 10}, "at least 14 days"),
        (
            {"cron": "0 3 * * *", "timezone": "UTC", "misfire_grace_seconds": 5},
            "misfire_grace_seconds",
        ),
    ],
)
def test_an_unusable_schedule_is_refused_with_a_sentence_and_changes_nothing(
    client: TestClient, env: _Env, body: dict[str, Any], message: str
) -> None:
    response = _admin(client).put(_URL, json=body, headers=_HEADERS)

    assert response.status_code == 422
    detail = response.json()["detail"]
    assert isinstance(detail, str)
    assert message in detail
    assert not env.schedule_file.exists()
    assert env.scheduler.applied == []
    assert env.audit() == []


def test_a_refused_change_leaves_the_existing_schedule_alone(client: TestClient, env: _Env) -> None:
    before = env.save(cron="0 4 * * *")
    response = _admin(client).put(
        _URL, json={"cron": "*/5 * * * *", "timezone": "UTC"}, headers=_HEADERS
    )
    assert response.status_code == 422
    assert schedule.load_schedule(str(env.config_dir)).schedule == before


def test_saving_is_refused_without_a_backup_directory(client: TestClient, env: _Env) -> None:
    env.set_env("PAPAIA_HOST=https://papaia.test\n")
    response = _admin(client).put(_URL, json=_DAILY, headers=_HEADERS)
    assert response.status_code == 409
    assert "PAPAIA_BACKUP_DIR" in response.json()["detail"]
    assert not env.schedule_file.exists()


def test_saving_is_refused_while_the_backup_directory_is_not_mounted(
    client: TestClient, env: _Env
) -> None:
    env.set_env(f"PAPAIA_BACKUP_DIR={env.backup_dir / 'gone'}\n")
    response = _admin(client).put(_URL, json=_DAILY, headers=_HEADERS)
    assert response.status_code == 409
    assert "not reachable" in response.json()["detail"]


@pytest.mark.parametrize("kind", ["restore", "upgrade"])
@pytest.mark.parametrize("method", ["put", "delete"])
def test_the_schedule_cannot_change_while_a_restore_or_upgrade_runs(
    client: TestClient,
    env: _Env,
    monkeypatch: pytest.MonkeyPatch,
    kind: str,
    method: str,
) -> None:
    """Both replace $PAPAIA_CONFIG_DIR, and a schedule written meanwhile would be
    overwritten with the state being restored."""
    wanted = api_maintenance.runner.RESTORE_KIND if kind == "restore" else (
        api_maintenance.runner.UPGRADE_KIND
    )

    async def _running(k: object = None) -> Any:
        return SimpleNamespace(is_running=True, target="the-target") if k == wanted else None

    monkeypatch.setattr(api_maintenance.runner, "find_runner", _running)
    env.save()
    kwargs: dict[str, Any] = {"headers": _HEADERS}
    if method == "put":
        kwargs["json"] = {"cron": "0 5 * * *", "timezone": "UTC"}

    response = getattr(_admin(client), method)(_URL, **kwargs)

    assert response.status_code == 409
    assert f"a {kind} of the-target is still running" in response.json()["detail"]
    assert schedule.load_schedule(str(env.config_dir)).schedule is not None
    assert env.scheduler.applied == []


# ---------------------------------------------------------------------------
# Removing
# ---------------------------------------------------------------------------


def test_removing_the_schedule_deletes_the_file_and_the_scheduled_job(
    client: TestClient, env: _Env
) -> None:
    env.save()

    response = _admin(client).delete(_URL, headers=_HEADERS)

    assert response.status_code == 200
    assert response.json()["configured"] is False
    assert not env.schedule_file.exists()
    assert env.scheduler.applied == [None]
    entry = env.audit()[-1]
    assert (entry["user"], entry["action"], entry["params"]) == (
        "tester",
        "backup.schedule.delete",
        {"removed": True},
    )


def test_removing_a_schedule_that_is_not_there_is_not_an_error(
    client: TestClient, env: _Env
) -> None:
    response = _admin(client).delete(_URL, headers=_HEADERS)
    assert response.status_code == 200
    assert env.audit()[-1]["params"] == {"removed": False}


def test_a_damaged_file_is_reported_and_can_be_replaced_or_removed(
    client: TestClient, env: _Env
) -> None:
    env.schedule_file.parent.mkdir(parents=True)
    env.schedule_file.write_text("cron: nonsense\n", encoding="utf-8")

    data = _admin(client).get(_URL).json()
    assert data["configured"] is False
    assert "schedule.yaml is invalid" in data["error"]

    assert _admin(client).put(_URL, json=_DAILY, headers=_HEADERS).json()["error"] is None

    env.schedule_file.write_text("cron: nonsense\n", encoding="utf-8")
    assert _admin(client).delete(_URL, headers=_HEADERS).json()["error"] is None
    assert not env.schedule_file.exists()


# ---------------------------------------------------------------------------
# The strip on the Backup page
# ---------------------------------------------------------------------------

_STRIP = "/partials/backup/schedule"


def test_the_strip_without_a_schedule_offers_to_set_one(client: TestClient, env: _Env) -> None:
    env.catalogue((timedelta(days=3, hours=2), "ok"))
    html = _admin(client).get(_STRIP).text
    assert "No backup is scheduled." in html
    assert "Set schedule" in html
    assert "3 days 2 hours ago" in html
    # Nobody promised a backup, so the age is shown and never coloured.
    assert "border-warning" not in html


def test_the_strip_names_the_schedule_the_next_run_and_the_last_backup(
    client: TestClient, env: _Env
) -> None:
    env.catalogue((timedelta(hours=20), "ok"))
    env.save(cron="30 4 * * mon,thu", timezone="Europe/Berlin", retention_days=14)
    env.scheduler._next_run = datetime.now(tz=UTC) + timedelta(hours=9)  # noqa: SLF001

    html = _admin(client).get(_STRIP).text

    assert "Every Monday, Thursday at 04:30" in html
    assert "Europe/Berlin" in html
    assert "Next run" in html
    assert "Keeps restore points for 14 days" in html
    assert "Edit schedule" in html
    assert "20 hours ago" in html
    assert "border-warning" not in html
    assert "not running" not in html


def test_the_strip_turns_to_a_warning_once_the_last_success_is_overdue(
    client: TestClient, env: _Env
) -> None:
    env.catalogue((timedelta(days=4), "ok"))
    env.save()
    env.scheduler._next_run = datetime.now(tz=UTC) + timedelta(hours=1)  # noqa: SLF001

    html = _admin(client).get(_STRIP).text

    assert "border-warning" in html
    assert "Older than 1 day 12 hours, the longest this schedule should go without one." in html


def test_the_strip_flags_a_latest_run_that_was_not_ok(client: TestClient, env: _Env) -> None:
    env.catalogue((timedelta(hours=30), "ok"), (timedelta(hours=2), "partial"))
    env.save()
    env.scheduler._next_run = datetime.now(tz=UTC) + timedelta(hours=1)  # noqa: SLF001
    html = _admin(client).get(_STRIP).text
    assert "The latest run finished as" in html
    assert "partial" in html


def test_the_strip_says_when_the_scheduler_is_not_running(client: TestClient, env: _Env) -> None:
    env.catalogue((timedelta(hours=5), "ok"))
    env.save()
    app_main._backup_scheduler = None  # noqa: SLF001
    html = _admin(client).get(_STRIP).text
    assert "The scheduler is not running" in html
    assert "No run is planned" in html


def test_a_paused_schedule_is_shown_as_paused_without_a_next_run(
    client: TestClient, env: _Env
) -> None:
    env.catalogue((timedelta(days=20), "ok"))
    env.save(enabled=False)
    html = _admin(client).get(_STRIP).text
    assert "paused" in html
    assert "Next run" not in html
    assert "border-warning" not in html  # a paused schedule promises nothing
    assert "not running" not in html


def test_the_strip_reports_a_damaged_schedule_file(client: TestClient, env: _Env) -> None:
    env.schedule_file.parent.mkdir(parents=True)
    env.schedule_file.write_text("cron: nonsense\n", encoding="utf-8")
    html = _admin(client).get(_STRIP).text
    assert "schedule.yaml is invalid" in html
    assert "No backup is scheduled until this is fixed" in html
    assert "Edit schedule" in html


def test_the_strip_is_admin_only(client: TestClient) -> None:
    assert _login(client, ["user"]).get(_STRIP).status_code == 403


def test_the_backup_page_carries_the_slot_and_the_editor(client: TestClient) -> None:
    html = _admin(client).get("/backup").text
    assert 'id="backup-schedule-slot"' in html
    assert 'hx-get="/partials/backup/schedule"' in html
    assert 'id="schedule-modal"' in html
    assert 'hx-get="/partials/backup/schedule/preview"' in html
    assert 'onsubmit="return false"' in html
    assert '<option value="Europe/Berlin">' in html
    assert 'name="weekdays" value="mon"' in html
    assert 'name="weekdays" value="sun"' in html
    assert '<option value="12">12 hours</option>' in html
    assert "function openScheduleModal()" in html


# ---------------------------------------------------------------------------
# The editor's live preview
# ---------------------------------------------------------------------------

_PREVIEW = "/partials/backup/schedule/preview"


def _preview(client: TestClient, **params: Any) -> str:
    return _admin(client).get(_PREVIEW, params=params).text


def test_the_default_form_previews_a_valid_daily_schedule(client: TestClient) -> None:
    html = _preview(client)
    assert 'data-valid="true"' in html
    assert 'id="schedule-compiled-cron" value="0 3 * * *"' in html
    assert 'id="schedule-compiled-timezone" value="UTC"' in html
    assert "Every day at 03:00" in html
    assert html.count("<li>") == 3


def test_a_weekly_preset_is_compiled_on_the_server(client: TestClient) -> None:
    html = _preview(
        client, mode="weekly", time="04:15", weekdays=["thu", "mon"], timezone="Europe/Berlin"
    )
    assert 'id="schedule-compiled-cron" value="15 4 * * mon,thu"' in html
    assert "Every Monday, Thursday at 04:15" in html
    assert "Europe/Berlin" in html


def test_an_hourly_preset_takes_its_minute_from_its_own_field(client: TestClient) -> None:
    html = _preview(client, mode="hourly", every_hours="6", minute="30")
    assert 'value="30 */6 * * *"' in html
    assert "Every 6 hours at :30" in html


def test_a_custom_expression_is_read_the_way_cron_reads_it(client: TestClient) -> None:
    html = _preview(client, mode="custom", cron="0 3 * * 1")
    assert 'value="0 3 * * mon"' in html
    first_run = html.split("<li>")[1]
    assert first_run.startswith("Mon ")


@pytest.mark.parametrize(
    ("params", "message"),
    [
        ({"mode": "weekly"}, "Pick at least one day of the week."),
        ({"mode": "hourly", "minute": "99"}, "The minute must be between 0 and 59."),
        ({"mode": "hourly", "minute": ""}, "The minute must be between 0 and 59."),
        ({"mode": "daily", "time": "25:00"}, "The time must look like 03:00."),
        ({"mode": "custom", "cron": "*/10 * * * *"}, "more often than once an hour"),
        ({"mode": "custom", "cron": ""}, "five fields"),
        ({"mode": "daily", "timezone": "Mars/Base"}, "Unknown timezone"),
        ({"mode": "daily", "retention_days": "1"}, "at least 2 days"),
        ({"mode": "daily", "retention_days": "abc"}, "whole number of days"),
        ({"mode": "daily", "retention_days": "-3"}, "whole number of days"),
        ({"mode": "nonsense"}, "Unknown schedule type"),
    ],
)
def test_the_preview_refuses_exactly_what_saving_refuses(
    client: TestClient, params: dict[str, str], message: str
) -> None:
    html = _preview(client, **params)
    assert 'data-valid="false"' in html
    assert 'data-valid="true"' not in html
    assert message in html


def test_the_preview_counts_the_restore_points_a_retention_would_delete(
    client: TestClient, env: _Env
) -> None:
    env.catalogue(
        (timedelta(days=40), "ok"), (timedelta(days=20), "failed"), (timedelta(hours=10), "ok")
    )
    html = _preview(client, mode="daily", retention_days="14")
    assert "The next run would delete 2 existing restore points older than 14 days." in html


def test_the_preview_says_when_a_retention_would_delete_nothing(
    client: TestClient, env: _Env
) -> None:
    env.catalogue((timedelta(hours=10), "ok"))
    html = _preview(client, mode="daily", retention_days="14")
    assert "No existing restore point is older than 14 days." in html


def test_the_preview_changes_nothing(client: TestClient, env: _Env) -> None:
    _preview(client, mode="daily")
    assert not env.schedule_file.exists()
    assert env.scheduler.applied == []
    assert env.audit() == []


def test_the_preview_is_admin_only(client: TestClient) -> None:
    assert _login(client, ["user"]).get(_PREVIEW).status_code == 403


# ---------------------------------------------------------------------------
# Starting with the application
# ---------------------------------------------------------------------------


def test_the_scheduler_starts_with_the_manager_and_stops_with_it(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    env.catalogue((timedelta(hours=2), "ok"))
    env.save(cron="0 3 * * mon", timezone="UTC")
    app_main._backup_scheduler = None  # noqa: SLF001

    with TestClient(create_app()):
        started = app_main._backup_scheduler  # noqa: SLF001
        assert isinstance(started, BackupScheduler)
        assert started.running
        assert started.next_run_time() is not None

    assert not started.running


def test_a_scheduler_that_cannot_start_does_not_stop_the_manager(
    env: _Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    def _boom(self: BackupScheduler) -> None:
        raise RuntimeError("no scheduler today")

    monkeypatch.setattr(BackupScheduler, "start", _boom)
    app_main._backup_scheduler = None  # noqa: SLF001

    with TestClient(create_app()) as client:
        assert app_main._backup_scheduler is None  # noqa: SLF001
        assert client.get("/health").status_code == 200
