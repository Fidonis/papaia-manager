"""The backup scheduler: what it schedules, what it starts, and what holds it back.

The APScheduler instance is real and started inside the test's event loop; its
timers are never waited for. What fires is `fire()`, called directly, against a
job queue that is constructed but not started -- the same shape as
test_api_maintenance.py -- so an enqueued backup stays queued and its callback is
invoked by hand against a stubbed `papaia-ctl`.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator, AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml

from app.config import Settings
from app.core import backup_run, runner, schedule, scheduler
from app.core.jobs import JobContext, JobQueue


class _Env:
    """A config directory with a backup directory and a catalogue, and a queue."""

    def __init__(self, tmp_path: Path) -> None:
        self.config_dir = tmp_path / "config"
        self.backup_dir = tmp_path / "backups"
        self.config_dir.mkdir()
        self.backup_dir.mkdir()
        self.set_env(f"PAPAIA_BACKUP_DIR={self.backup_dir}\n")
        self.settings = Settings(  # type: ignore[call-arg]
            oidc_issuer_kc_auth="https://kc.test/auth",
            oidc_issuer_kc_token="https://kc.test/token",
            oidc_issuer_kc_certs="https://kc.test/certs",
            manager_host="http://localhost:8120",
            manager_oidc_client_secret="secret",
            manager_session_secret="secret",
            papaia_config_dir=str(self.config_dir),
            papaia_workspace_dir=str(tmp_path / "workspace"),
        )
        self.queue = JobQueue(config_dir=str(self.config_dir))
        self.verb_calls: list[dict[str, Any]] = []

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

    async def run_queued(self, job_id: str) -> str:
        """Run a queued job's callback by hand and return its log."""
        callback = self.queue._callbacks[job_id]  # noqa: SLF001
        self.queue.jobs_dir.mkdir(parents=True, exist_ok=True)
        job = self.queue.get_job(job_id)
        assert job is not None
        await callback(JobContext(job=job, log_path=self.queue.jobs_dir / f"{job_id}.log"))
        return self.queue.read_log(job_id)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Env:
    environment = _Env(tmp_path)

    async def _idle(kind: object = None) -> None:
        return None

    async def _fake_verb(**kwargs: Any) -> AsyncGenerator[str, None]:
        environment.verb_calls.append(kwargs)

        async def _gen() -> AsyncGenerator[str, None]:
            yield "archiving"

        return _gen()

    monkeypatch.setattr(runner, "find_runner", _idle)
    monkeypatch.setattr(backup_run, "run_core_verb", _fake_verb)
    return environment


@pytest.fixture
async def sched(env: _Env) -> AsyncIterator[scheduler.BackupScheduler]:
    created = scheduler.BackupScheduler(env.settings, env.queue)
    yield created
    created.shutdown()


async def _until(condition: Callable[[], object], *, seconds: float = 5.0) -> None:
    """Wait for something APScheduler does on its own, a poll at a time."""
    deadline = asyncio.get_running_loop().time() + seconds
    while not condition():
        assert asyncio.get_running_loop().time() < deadline, "the scheduler never got there"
        await asyncio.sleep(0.05)


def _flags(env: _Env) -> list[str]:
    assert len(env.verb_calls) == 1
    return list(env.verb_calls[0]["extra_flags"])


# ---------------------------------------------------------------------------
# The scheduled job follows the file
# ---------------------------------------------------------------------------


async def test_starting_loads_the_schedule_and_plans_the_next_run(
    env: _Env, sched: scheduler.BackupScheduler
) -> None:
    env.catalogue((timedelta(hours=1), "ok"))
    env.save(cron="0 3 * * mon", timezone="Europe/Berlin")

    sched.start()

    assert sched.running
    upcoming = sched.next_run_time()
    assert upcoming is not None
    assert upcoming > datetime.now(tz=UTC)
    local = upcoming.astimezone(schedule.zone("Europe/Berlin"))
    assert (local.weekday(), local.hour, local.minute) == (0, 3, 0)  # Monday 03:00


async def test_starting_without_a_file_schedules_nothing(
    env: _Env, sched: scheduler.BackupScheduler
) -> None:
    sched.start()
    assert sched.running
    assert sched.next_run_time() is None


async def test_a_damaged_file_schedules_nothing_and_does_not_stop_the_start(
    env: _Env, sched: scheduler.BackupScheduler
) -> None:
    path = schedule.schedule_path(str(env.config_dir))
    path.parent.mkdir(parents=True)
    path.write_text("cron: not-a-cron\n", encoding="utf-8")

    sched.start()

    assert sched.running
    assert sched.next_run_time() is None


async def test_applying_a_schedule_replaces_the_previous_one(
    env: _Env, sched: scheduler.BackupScheduler
) -> None:
    env.catalogue((timedelta(hours=1), "ok"))
    sched.start()
    sched.apply(env.save(cron="0 3 * * *"))
    daily = sched.next_run_time()
    sched.apply(env.save(cron="0 3 * * thu"))
    weekly = sched.next_run_time()

    assert daily is not None
    assert weekly is not None
    assert weekly.astimezone(UTC).weekday() == 3
    assert [job.id for job in sched._scheduler.get_jobs()] == [scheduler.JOB_ID]  # noqa: SLF001


async def test_removing_or_pausing_the_schedule_unschedules_the_job(
    env: _Env, sched: scheduler.BackupScheduler
) -> None:
    env.catalogue((timedelta(hours=1), "ok"))
    sched.start()
    sched.apply(env.save())
    assert sched.next_run_time() is not None

    sched.apply(env.save(enabled=False))
    assert sched.next_run_time() is None

    sched.apply(env.save())
    sched.apply(None)
    assert sched.next_run_time() is None


async def test_shutdown_stops_the_scheduler_and_may_be_repeated(
    env: _Env, sched: scheduler.BackupScheduler
) -> None:
    sched.start()
    assert sched.running
    sched.shutdown()
    sched.shutdown()  # APScheduler stops a turn later; a second call must not queue a second stop
    assert not sched.running
    await asyncio.sleep(0)
    assert not sched._scheduler.running  # noqa: SLF001


# ---------------------------------------------------------------------------
# A scheduled run
# ---------------------------------------------------------------------------


async def test_a_run_with_no_schedule_does_nothing(
    env: _Env, sched: scheduler.BackupScheduler
) -> None:
    assert await sched.fire() == "disabled"
    assert env.queue.active_job() is None


async def test_a_run_queues_the_same_backup_the_button_does(
    env: _Env, sched: scheduler.BackupScheduler
) -> None:
    env.catalogue((timedelta(hours=20), "ok"))
    env.save()

    assert await sched.fire() == "started"

    job = env.queue.active_job()
    assert job is not None
    assert (job.action, job.user, job.target) == ("backup", "scheduler", str(env.backup_dir))

    log = await env.run_queued(job.id)
    assert _flags(env) == [f"--backup-dir={env.backup_dir}"]
    assert env.verb_calls[0]["verb"] == "backup"
    assert "[ctl] papaia-ctl backup" in log
    assert "archiving" in log

    entry = env.audit()[-1]
    assert (entry["user"], entry["action"], entry["result"]) == ("scheduler", "backup", "ok")
    assert entry["params"] == {"retention_days": None, "trigger": "schedule"}
    assert entry["job_id"] == job.id


async def test_the_retention_is_passed_while_the_catalogue_has_a_recent_good_point(
    env: _Env, sched: scheduler.BackupScheduler
) -> None:
    env.catalogue((timedelta(hours=20), "ok"))
    env.save(retention_days=14)

    assert await sched.fire() == "started"
    job = env.queue.active_job()
    assert job is not None
    log = await env.run_queued(job.id)

    assert "--retention-period-days=14" in _flags(env)
    assert "retention not applied" not in log
    assert env.audit()[-1]["params"]["retention_days"] == 14


async def test_the_retention_is_held_back_after_a_series_of_bad_runs(
    env: _Env, sched: scheduler.BackupScheduler
) -> None:
    """The newest good restore point is two days old and the runs since failed. A
    prune now could only remove good restore points, so this run takes no part in
    it, and says so at the top of its log."""
    env.catalogue(
        (timedelta(days=2), "ok"), (timedelta(hours=20), "failed"), (timedelta(hours=1), "failed")
    )
    env.save(retention_days=14)

    assert await sched.fire() == "started"
    job = env.queue.active_job()
    assert job is not None
    log = await env.run_queued(job.id)

    assert _flags(env) == [f"--backup-dir={env.backup_dir}"]
    assert log.splitlines()[0].startswith("[schedule] retention not applied: the newest successful")
    assert env.audit()[-1]["params"]["retention_days"] is None


# ---------------------------------------------------------------------------
# A run that cannot start now
# ---------------------------------------------------------------------------


def _retry_job(sched: scheduler.BackupScheduler) -> Any:
    return sched._scheduler.get_job(scheduler.RETRY_JOB_ID)  # noqa: SLF001


async def test_an_unreachable_backup_directory_skips_the_run_and_plans_a_retry(
    env: _Env, sched: scheduler.BackupScheduler
) -> None:
    env.save()
    env.set_env(f"PAPAIA_BACKUP_DIR={env.backup_dir / 'not-mounted'}\n")
    sched.start()

    assert await sched.fire() == "retry"

    assert env.queue.active_job() is None
    retry = _retry_job(sched)
    assert retry is not None
    assert retry.args == (2,)
    skip = env.audit()[-1]
    assert (skip["user"], skip["action"], skip["result"]) == (
        "scheduler",
        "backup.schedule.skip",
        "skipped",
    )
    assert skip["params"] == {
        "reason": "the backup directory is not reachable",
        "attempt": 1,
        "gave_up": False,
    }


async def test_an_unset_backup_directory_is_skipped_too(
    env: _Env, sched: scheduler.BackupScheduler
) -> None:
    env.save()
    env.set_env("PAPAIA_HOST=https://papaia.test\n")
    sched.start()
    assert await sched.fire() == "retry"
    assert env.audit()[-1]["target"] == ""


async def test_a_running_restore_holds_the_run_back(
    env: _Env, sched: scheduler.BackupScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _restoring(kind: object = None) -> Any:
        if kind == runner.RESTORE_KIND:
            return SimpleNamespace(is_running=True, target="2026-07-30_10-19-38")
        return None

    monkeypatch.setattr(runner, "find_runner", _restoring)
    env.save()
    sched.start()

    assert await sched.fire() == "retry"

    assert env.queue.active_job() is None
    reason = env.audit()[-1]["params"]["reason"]
    assert reason == "a restore of 2026-07-30_10-19-38 is still running"


async def test_a_running_upgrade_holds_the_run_back(
    env: _Env, sched: scheduler.BackupScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _upgrading(kind: object = None) -> Any:
        if kind == runner.UPGRADE_KIND:
            return SimpleNamespace(is_running=True, target="1.5.0")
        return None

    monkeypatch.setattr(runner, "find_runner", _upgrading)
    env.save()
    sched.start()
    assert await sched.fire() == "retry"
    assert env.audit()[-1]["params"]["reason"] == "a upgrade of 1.5.0 is still running"


async def test_a_finished_runner_does_not_hold_the_run_back(
    env: _Env, sched: scheduler.BackupScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _done(kind: object = None) -> Any:
        return SimpleNamespace(is_running=False, target="x")

    monkeypatch.setattr(runner, "find_runner", _done)
    env.save()
    assert await sched.fire() == "started"


async def test_another_job_in_the_queue_holds_the_run_back(
    env: _Env, sched: scheduler.BackupScheduler
) -> None:
    async def _noop(ctx: JobContext) -> None:
        return None

    await env.queue.enqueue(action="addon-install", target="paperless", user="u", callback=_noop)
    env.save()
    sched.start()

    assert await sched.fire() == "retry"

    assert [job.action for job in env.queue.list_jobs()] == ["addon-install"]
    assert env.audit()[-1]["params"]["reason"] == "a addon-install job is already running"


async def test_docker_being_unreachable_does_not_stop_a_backup(
    env: _Env, sched: scheduler.BackupScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _no_docker(kind: object = None) -> None:
        raise runner.RunnerError("docker is not reachable")

    monkeypatch.setattr(runner, "find_runner", _no_docker)
    env.save()
    assert await sched.fire() == "started"


async def test_the_last_attempt_gives_up_and_plans_nothing_more(
    env: _Env, sched: scheduler.BackupScheduler
) -> None:
    env.save()
    env.set_env("PAPAIA_HOST=https://papaia.test\n")
    sched.start()

    assert await sched.fire(attempt=scheduler.MAX_ATTEMPTS) == "gave-up"

    assert _retry_job(sched) is None
    assert env.audit()[-1]["params"]["gave_up"] is True


async def test_the_retries_count_up_to_the_limit(
    env: _Env, sched: scheduler.BackupScheduler
) -> None:
    env.save()
    env.set_env("PAPAIA_HOST=https://papaia.test\n")
    sched.start()
    outcomes = [await sched.fire(attempt=n) for n in range(1, scheduler.MAX_ATTEMPTS + 1)]
    assert outcomes == ["retry"] * (scheduler.MAX_ATTEMPTS - 1) + ["gave-up"]
    attempts = [entry["params"]["attempt"] for entry in env.audit()]
    assert attempts == list(range(1, scheduler.MAX_ATTEMPTS + 1))


async def test_a_new_schedule_drops_a_pending_retry(
    env: _Env, sched: scheduler.BackupScheduler
) -> None:
    """A retry belongs to the schedule that was running. A removed schedule must not
    start one more backup because of it."""
    env.save()
    env.set_env("PAPAIA_HOST=https://papaia.test\n")
    sched.start()
    assert await sched.fire() == "retry"
    assert _retry_job(sched) is not None

    sched.apply(None)

    assert _retry_job(sched) is None


# ---------------------------------------------------------------------------
# Making up a missed run after a start
# ---------------------------------------------------------------------------


def _catchup_job(sched: scheduler.BackupScheduler) -> Any:
    return sched._scheduler.get_job(scheduler.CATCHUP_JOB_ID)  # noqa: SLF001


async def test_a_run_is_made_up_when_the_newest_success_is_overdue(
    env: _Env, sched: scheduler.BackupScheduler
) -> None:
    env.catalogue((timedelta(hours=40), "ok"))  # daily: overdue after 36 hours
    env.save()

    sched.start()

    catchup = _catchup_job(sched)
    assert catchup is not None
    assert catchup.args == (1,)
    delay = catchup.next_run_time - datetime.now(tz=UTC)
    assert timedelta(seconds=100) < delay <= scheduler.CATCHUP_DELAY


async def test_nothing_is_made_up_while_the_last_success_is_recent(
    env: _Env, sched: scheduler.BackupScheduler
) -> None:
    env.catalogue((timedelta(hours=30), "ok"))
    env.save()
    sched.start()
    assert _catchup_job(sched) is None


async def test_a_run_is_made_up_when_there_has_never_been_a_backup(
    env: _Env, sched: scheduler.BackupScheduler
) -> None:
    env.save()
    sched.start()
    assert _catchup_job(sched) is not None


async def test_a_partial_or_failed_run_does_not_satisfy_the_catch_up(
    env: _Env, sched: scheduler.BackupScheduler
) -> None:
    env.catalogue((timedelta(hours=2), "partial"), (timedelta(hours=1), "failed"))
    env.save()
    sched.start()
    assert _catchup_job(sched) is not None


async def test_a_schedule_can_opt_out_of_catching_up(
    env: _Env, sched: scheduler.BackupScheduler
) -> None:
    env.catalogue((timedelta(days=9), "ok"))
    env.save(run_on_startup="never")
    sched.start()
    assert _catchup_job(sched) is None


async def test_a_paused_or_absent_schedule_makes_nothing_up(
    env: _Env, sched: scheduler.BackupScheduler
) -> None:
    env.catalogue((timedelta(days=9), "ok"))
    sched.start()
    assert _catchup_job(sched) is None

    sched.shutdown()
    env.save(enabled=False)
    paused = scheduler.BackupScheduler(env.settings, env.queue)
    try:
        paused.start()
        assert _catchup_job(paused) is None
    finally:
        paused.shutdown()


async def test_a_weekly_schedule_is_not_called_overdue_after_a_day(
    env: _Env, sched: scheduler.BackupScheduler
) -> None:
    env.catalogue((timedelta(days=6), "ok"))
    env.save(cron="0 3 * * mon")  # overdue after 10.5 days
    sched.start()
    assert _catchup_job(sched) is None


# ---------------------------------------------------------------------------
# Through APScheduler itself
#
# Everything above calls `fire()`. These let the scheduler's own machinery run the
# job -- a bound coroutine on the event loop, from a date trigger -- with the
# delays shortened to a fraction of a second.
# ---------------------------------------------------------------------------


async def test_a_retry_is_really_run_by_the_scheduler_and_then_starts_the_backup(
    env: _Env, sched: scheduler.BackupScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler, "RETRY_DELAY", timedelta(milliseconds=150))
    env.save()
    env.set_env("PAPAIA_HOST=https://papaia.test\n")  # no backup directory: the run is skipped
    sched.start()
    assert await sched.fire() == "retry"
    assert env.queue.active_job() is None

    env.set_env(f"PAPAIA_BACKUP_DIR={env.backup_dir}\n")  # the mount comes back
    await _until(lambda: env.queue.active_job() is not None)

    job = env.queue.active_job()
    assert job is not None
    assert (job.action, job.user) == ("backup", "scheduler")
    assert _retry_job(sched) is None  # a one-shot job is gone once it has run


async def test_a_missed_run_is_really_made_up_by_the_scheduler_after_the_start(
    env: _Env, sched: scheduler.BackupScheduler, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(scheduler, "CATCHUP_DELAY", timedelta(milliseconds=150))
    env.catalogue((timedelta(days=3), "ok"))
    env.save()

    sched.start()
    assert env.queue.active_job() is None
    await _until(lambda: env.queue.active_job() is not None)

    job = env.queue.active_job()
    assert job is not None
    assert (job.action, job.user) == ("backup", "scheduler")
    assert _catchup_job(sched) is None


async def test_the_scheduled_job_runs_the_same_fire_the_tests_call(
    env: _Env, sched: scheduler.BackupScheduler
) -> None:
    """The job APScheduler holds for the cron trigger is `_on_schedule`; running it
    through the executor starts a backup, so the wiring between trigger and `fire`
    is not just the test calling `fire` itself."""
    env.catalogue((timedelta(hours=5), "ok"))
    env.save()
    sched.start()
    job = sched._scheduler.get_job(scheduler.JOB_ID)  # noqa: SLF001
    assert job is not None

    await job.func()

    active = env.queue.active_job()
    assert active is not None
    assert active.user == "scheduler"
