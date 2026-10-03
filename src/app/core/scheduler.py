"""The in-process backup scheduler.

It decides *when* to start a backup and leaves everything else to the code the
Backup page's button uses (`app.core.backup_run`): the job queue, the log, the
audit entry, the refusal while a restore or an upgrade runs. Being a thread of
this process rather than a timer on the host is the point -- the manager runs in a
container with no systemd and no cron, and the same code then works on any host
that can run the stack.

APScheduler 3.x with a memory job store, on purpose. `schedule.yaml` is the only
source of truth (`app.core.schedule`), so a persistent store would be a second one
and a schedule that was removed could come back from it after a restart. The
price is that APScheduler does not know about runs missed while the process was
down; `catchup` below is what covers that, from the catalogue instead.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from apscheduler.jobstores.memory import MemoryJobStore
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

from app.config import Settings
from app.core import backup_run, backups, schedule
from app.core.audit import write_audit_entry
from app.core.jobs import JobQueue

logger = logging.getLogger(__name__)

JOB_ID = "backup-schedule"
RETRY_JOB_ID = "backup-schedule-retry"
CATCHUP_JOB_ID = "backup-schedule-catchup"

# The user a scheduled run is attributed to in the job list and the audit log.
SCHEDULER_USER = "scheduler"

# After a start the manager waits this long before it makes up a missed run: the
# stack should be up, and a restore or upgrade that just restarted this container
# is still winding down in its own runner.
CATCHUP_DELAY = timedelta(seconds=120)

# A run that cannot start (a runner is active, another job is in the queue, the
# backup directory is not mounted) is tried again, up to an hour in all.
RETRY_DELAY = timedelta(minutes=10)
MAX_ATTEMPTS = 6

Clock = Callable[[], datetime]


def _utc_now() -> datetime:
    return datetime.now(tz=UTC)


class BackupScheduler:
    """Owns the AsyncIOScheduler and keeps its one job in step with schedule.yaml."""

    def __init__(self, settings: Settings, queue: JobQueue, *, clock: Clock = _utc_now) -> None:
        self._settings = settings
        self._queue = queue
        self._clock = clock
        self._stopping = False
        self._scheduler = AsyncIOScheduler(
            jobstores={"default": MemoryJobStore()},
            job_defaults={"coalesce": True, "max_instances": 1},
            timezone=UTC,
        )

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Start the scheduler on the running event loop and load the schedule."""
        self._scheduler.start()
        self.reload()
        self._schedule_catchup()

    def shutdown(self) -> None:
        # wait=False: no new firings. A backup that is already queued is the job
        # queue's, and it is stopped there.
        #
        # AsyncIOScheduler runs its shutdown on the event loop, a turn later, and
        # keeps reporting `running` until then. A second call in between would
        # queue a second shutdown that raises inside the loop, so a call that has
        # been made is remembered here instead of read back from APScheduler.
        if self._stopping:
            return
        self._stopping = True
        if self._scheduler.running:
            self._scheduler.shutdown(wait=False)

    @property
    def running(self) -> bool:
        return bool(self._scheduler.running) and not self._stopping

    # -- the schedule -------------------------------------------------------

    def reload(self) -> schedule.ScheduleLoad:
        """Read schedule.yaml and make the scheduler match it."""
        loaded = schedule.load_schedule(self._settings.papaia_config_dir)
        self.apply(loaded.schedule)
        return loaded

    def apply(self, current: schedule.BackupSchedule | None) -> None:
        """Replace the scheduled job with the one `current` describes, or none.

        A retry or a catch-up that is still pending belongs to the schedule that
        is being replaced, so it goes too: a schedule that was just removed must
        not start one more run.
        """
        for stale in (RETRY_JOB_ID, CATCHUP_JOB_ID):
            self._remove(stale)
        if current is None or not current.enabled:
            self._remove(JOB_ID)
            logger.info("backup schedule: nothing scheduled")
            return
        self._scheduler.add_job(
            self._on_schedule,
            trigger=schedule.build_trigger(current.cron, current.timezone),
            id=JOB_ID,
            name=JOB_ID,
            replace_existing=True,
            misfire_grace_time=current.misfire_grace_seconds,
        )
        logger.info(
            "backup schedule: %s (%s), next run %s",
            schedule.describe(current.cron),
            current.timezone,
            self.next_run_time(),
        )

    def next_run_time(self) -> datetime | None:
        """When the scheduled job fires next, or None if nothing is scheduled."""
        job: Any = self._scheduler.get_job(JOB_ID)
        value = getattr(job, "next_run_time", None) if job is not None else None
        return value if isinstance(value, datetime) else None

    def _remove(self, job_id: str) -> None:
        if self._scheduler.get_job(job_id) is not None:
            self._scheduler.remove_job(job_id)

    # -- firing -------------------------------------------------------------

    async def _on_schedule(self) -> None:
        await self.fire(attempt=1)

    async def _retry(self, attempt: int) -> None:
        await self.fire(attempt=attempt)

    async def fire(self, attempt: int = 1) -> str:
        """Start one scheduled backup, or say why that is not possible right now.

        Returns what happened -- `started`, `retry`, `gave-up` or `disabled` -- so a
        test can assert on it; nothing else reads it. A run that has to wait is
        retried rather than queued behind whatever is in the way: a backup that
        sat in the queue would start minutes later against a stack that has moved
        on, and a restore or upgrade is not in the queue to wait behind at all.
        """
        config_dir = self._settings.papaia_config_dir
        current = schedule.load_schedule(config_dir).schedule
        if current is None or not current.enabled:
            logger.info("backup schedule: a run came due but no schedule is active")
            return "disabled"

        backup_dir = backups.resolve_backup_dir(config_dir)
        if backup_dir is None or not backups.is_reachable(backup_dir):
            return self._skip(attempt, "the backup directory is not reachable", backup_dir)

        blocking = await backup_run.blocking_runner()
        if blocking is not None:
            return self._skip(
                attempt, f"a {blocking.label} of {blocking.target} is still running", backup_dir
            )
        active = self._queue.active_job()
        if active is not None:
            return self._skip(attempt, f"a {active.action} job is already running", backup_dir)

        points = await asyncio.get_running_loop().run_in_executor(
            None, backups.load_restore_points, backup_dir
        )
        plan = schedule.plan_retention(current, points, now=self._clock())
        note = f"retention not applied: {plan.skipped}" if plan.skipped else None
        job = await backup_run.enqueue_backup(
            self._queue,
            self._settings,
            backup_dir,
            user=SCHEDULER_USER,
            retention_days=plan.days,
            trigger=backup_run.SCHEDULE,
            note=note,
        )
        logger.info(
            "backup schedule: started job %s%s", job.id, f" ({note})" if note else ""
        )
        return "started"

    def _skip(self, attempt: int, reason: str, backup_dir: Path | None) -> str:
        """Record a run that could not start and plan the next try, if there is one."""
        giving_up = attempt >= MAX_ATTEMPTS
        write_audit_entry(
            self._settings.papaia_config_dir,
            user=SCHEDULER_USER,
            action="backup.schedule.skip",
            target=str(backup_dir) if backup_dir is not None else "",
            result="skipped",
            params={"reason": reason, "attempt": attempt, "gave_up": giving_up},
        )
        if giving_up:
            logger.warning(
                "backup schedule: giving up after %d attempts: %s", attempt, reason
            )
            return "gave-up"
        logger.info(
            "backup schedule: not started (%s); trying again in %s",
            reason,
            schedule.humanize(RETRY_DELAY),
        )
        self._scheduler.add_job(
            self._retry,
            trigger=DateTrigger(run_date=self._clock() + RETRY_DELAY, timezone=UTC),
            args=[attempt + 1],
            id=RETRY_JOB_ID,
            name=RETRY_JOB_ID,
            replace_existing=True,
            misfire_grace_time=3600,
        )
        return "retry"

    # -- catching up --------------------------------------------------------

    def _schedule_catchup(self) -> None:
        """Make up a run missed while the manager was down.

        The manager restarts on every upgrade, restore and host reboot, and a
        slot that falls in that window is gone: the job store is in memory, so
        APScheduler cannot know it was missed. The catalogue can -- if the newest
        successful restore point is older than the schedule's own interval (with
        the margin the status strip uses), one run is started shortly after the
        start. It counts manual and command-line backups too, which is why it asks
        the catalogue and not a record of its own.
        """
        current = schedule.load_schedule(self._settings.papaia_config_dir).schedule
        if current is None or not current.enabled or current.run_on_startup != "if_missed":
            return
        backup_dir = backups.resolve_backup_dir(self._settings.papaia_config_dir)
        points = backups.load_restore_points(backup_dir)
        if not schedule.catchup_due(current, points, now=self._clock()):
            return
        logger.info(
            "backup schedule: the newest successful backup is older than %s; "
            "making up a run in %s",
            schedule.humanize(schedule.overdue_after(current)),
            schedule.humanize(CATCHUP_DELAY),
        )
        self._scheduler.add_job(
            self._retry,
            trigger=DateTrigger(run_date=self._clock() + CATCHUP_DELAY, timezone=UTC),
            args=[1],
            id=CATCHUP_JOB_ID,
            name=CATCHUP_JOB_ID,
            replace_existing=True,
            misfire_grace_time=3600,
        )
