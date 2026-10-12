"""The background task that looks for what calls for a notification, and sends it.

Two ways in, one way out:

* a failed job is reported the moment the job queue says so (`on_job_finished`), so it needs
  no polling;
* everything else is looked for every `check_interval_minutes`, by the detectors in `detect`,
  and only for the events somebody chose -- an event nobody receives costs no `docker ps`, no
  `git fetch` and no `doctor`;
* both end in `deliver`, to the recipients that chose the event.

The loop is one task that waits between passes, so a pass cannot overlap the next. The
configuration is read at the start of every pass and again while waiting, so a change on the page
needs no rescheduling and a new interval takes effect within seconds.
It assumes one process, like the backup scheduler. What it has reported is remembered in the
state file (`store.NotifyState`) before the delivery starts: a manager that restarts between
the two loses a notification instead of repeating it.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import Settings
from app.core.jobs import Job, JobQueue, JobStatus
from app.core.notifications import detect, store
from app.core.notifications.delivery import (
    TEST_EVENT,
    Notification,
    Outcome,
    deliver,
)
from app.core.notifications.model import INFO_BY_EVENT, Event, Severity

log = logging.getLogger(__name__)

# Before the first pass, so a manager that starts together with the stack does not report the
# containers that are still coming up.
FIRST_PASS_DELAY_SECONDS = 60.0
# Release tags come from the network; a release is not a thing to look for every five minutes.
UPDATE_CHECK_INTERVAL_SECONDS = 3600.0
# How often the check interval is read again while the loop waits for the next pass.
SETTINGS_POLL_SECONDS = 10.0


@dataclass(frozen=True)
class PassResult:
    """What one pass did. `interval_seconds` is how long to sleep before the next."""

    notified: int
    interval_seconds: float


class NotificationService:
    def __init__(
        self,
        settings: Settings,
        queue: JobQueue | None = None,
        *,
        detectors: dict[Event, detect.Detector] | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        retry_delay: float | None = None,
        first_pass_delay: float = FIRST_PASS_DELAY_SECONDS,
        update_interval: float = UPDATE_CHECK_INTERVAL_SECONDS,
        settings_poll: float = SETTINGS_POLL_SECONDS,
    ) -> None:
        self._settings = settings
        self._detectors = (
            detectors if detectors is not None else detect.build_detectors(settings, queue)
        )
        self._transport = transport
        self._delivery_args: dict[str, Any] = {}
        if retry_delay is not None:
            self._delivery_args["retry_delay"] = retry_delay
        self._first_delay = first_pass_delay
        self._update_interval = update_interval
        self._settings_poll = settings_poll
        self._last_update_check: float | None = None
        self._task: asyncio.Task[None] | None = None
        self._stopping: asyncio.Task[None] | None = None
        self._pending: set[asyncio.Task[None]] = set()
        self._pass_lock = asyncio.Lock()
        # Set when a job ends: the stack was just changed, so the next pass is not for later.
        self._wake = asyncio.Event()
        self.last_pass_at: datetime | None = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Begin the loop. Needs the running event loop, so it is called from the startup hook."""
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(
                self._run(), name="notification-service"
            )

    def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._stopping, self._task = self._task, None

    async def stopped(self) -> None:
        """Wait until a cancelled task is gone (used by the tests)."""
        task = self._stopping
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._stopping = None

    async def _run(self) -> None:
        await asyncio.sleep(self._first_delay)
        while True:
            started = time.monotonic()
            # Before the pass, not after: a job that ends while it runs may have changed what it
            # already looked at, and asks for another.
            self._wake.clear()
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed pass costs this round of notifications, never the manager.
                log.exception("a notification pass failed; it will be tried again")
            await self._wait_for_next(started)

    def _interval_seconds(self) -> float:
        """The check interval as the configuration says now; the default when it is unusable."""
        load = store.load_config(self._settings.papaia_config_dir)
        return load.config.settings.check_interval_minutes * 60.0

    async def _wait_for_next(self, started: float) -> None:
        """Wait until one interval has passed since the last pass began, or a job has ended.

        The interval is read again while waiting, not once before the sleep: a change on the
        page then takes effect within `settings_poll` seconds. Shortened below the time that has
        already passed, the next pass is due at once; lengthened, it waits the rest.

        A job that ends cuts the wait short. Starting or stopping something in the manager is the
        change an administrator most wants to see reported (or not missed): a service that is
        started and stopped again within one interval would otherwise never be seen running, and
        a stop would be reported up to an interval late."""
        while True:
            if self._wake.is_set():
                return
            remaining = started + self._interval_seconds() - time.monotonic()
            if remaining <= 0:
                return
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(
                    self._wake.wait(), timeout=min(remaining, self._settings_poll)
                )

    # -- the periodic pass -------------------------------------------------

    async def run_once(self) -> PassResult:
        """Look for what calls for a notification, record it and deliver it."""
        async with self._pass_lock:
            config_dir = self._settings.papaia_config_dir
            load = store.load_config(config_dir)
            interval = load.config.settings.check_interval_minutes * 60.0
            if load.error:
                log.warning("notifications are not checked: %s", load.error)
                return PassResult(0, interval)

            wanted = {
                event
                for recipient in load.config.recipients
                if recipient.enabled
                for event in recipient.events
            }
            findings: list[detect.Finding] = []
            for event, detector in self._detectors.items():
                if event not in wanted or not self._due(event):
                    continue
                try:
                    findings += await detector(load.config.settings)
                except Exception:
                    log.exception("looking for %s failed", event.value)

            now = datetime.now(tz=UTC)
            event_settings = load.config.settings
            new = store.update_state(
                config_dir,
                lambda state: detect.decide(findings, state, now=now, settings=event_settings),
            )
            self.last_pass_at = now
            for condition in new:
                notification = Notification(
                    event=condition.event.value,
                    severity=INFO_BY_EVENT[condition.event].severity,
                    message=condition.message,
                    subject=condition.subject,
                    details=condition.details,
                    timestamp=now,
                )
                await self._deliver(notification, condition.event)
            return PassResult(len(new), interval)

    def _due(self, event: Event) -> bool:
        """Release tags are fetched from the network: not on every pass."""
        if event is not Event.UPDATE_AVAILABLE:
            return True
        now = time.monotonic()
        if self._last_update_check is not None and (
            now - self._last_update_check < self._update_interval
        ):
            return False
        self._last_update_check = now
        return True

    async def _deliver(self, notification: Notification, event: Event) -> list[Outcome]:
        # Read again: the configuration may have changed while the detectors ran.
        config = store.load_config(self._settings.papaia_config_dir).config
        return await deliver(
            notification,
            config.subscribed_to(event),
            settings=self._settings,
            transport=self._transport,
            **self._delivery_args,
        )

    # -- failed jobs -------------------------------------------------------

    def on_job_finished(self, job: Job) -> None:
        """The job queue's listener. Asks for a pass at once, and hands a failed job to a task of
        its own for the report. Returns at once."""
        self._wake.set()
        if job.status is not JobStatus.FAILED:
            return
        task = asyncio.get_running_loop().create_task(self._report_job(job))
        # The loop keeps only a weak reference to a task: hold it until it is done.
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    async def _report_job(self, job: Job) -> None:
        try:
            subject = f"{job.action} {job.target}".strip()
            notification = Notification(
                event=Event.JOB_FAILED.value,
                severity=INFO_BY_EVENT[Event.JOB_FAILED].severity,
                message=f"The job {subject} failed.",
                subject=subject,
                # What a person needs to find the job, and nothing the job wrote: its log and
                # the exception text can hold a secret, and both leave the manager here.
                details={
                    "job_id": job.id,
                    "action": job.action,
                    "target": job.target,
                    "started_by": job.user,
                    "exit_code": job.exit_code,
                },
            )
            await self._deliver(notification, Event.JOB_FAILED)
        except Exception:
            log.exception("a failed job could not be reported")

    async def settled(self) -> None:
        """Wait for the notifications in flight (used by the tests)."""
        while self._pending:
            tasks = list(self._pending)
            await asyncio.gather(*tasks, return_exceptions=True)
            # Not left to the done callback: awaiting tasks that are already done does not
            # give it a turn, and this would spin.
            self._pending.difference_update(tasks)


async def send_test(
    settings: Settings,
    recipient_id: str,
    *,
    actor: str,
    transport: httpx.AsyncBaseTransport | None = None,
    retry_delay: float | None = None,
) -> Outcome | None:
    """Send a test notification to one recipient and remember how it went. None if the
    recipient does not exist (any more)."""
    config_dir = settings.papaia_config_dir
    recipient = store.load_config(config_dir).config.find(recipient_id)
    if recipient is None:
        return None
    notification = Notification(
        event=TEST_EVENT,
        severity=Severity.INFO,
        message="This is a test notification from the papAIa manager.",
        details={"recipient": recipient.name},
    )
    args: dict[str, Any] = {} if retry_delay is None else {"retry_delay": retry_delay}
    (outcome,) = await deliver(
        notification, [recipient], settings=settings, transport=transport, actor=actor, **args
    )

    def remember(state: store.NotifyState) -> None:
        state.tests[recipient.id] = store.TestResult(
            at=datetime.now(tz=UTC).isoformat(timespec="seconds"),
            ok=outcome.ok,
            reason=outcome.reason,
        )

    store.update_state(config_dir, remember)
    return outcome
