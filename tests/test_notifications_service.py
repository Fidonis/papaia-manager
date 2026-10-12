"""The background task: who is asked, what is sent, and what is remembered.

The detectors are stand-ins that return what they are told to, so a pass can be driven by hand
(`run_once`) without a loop, a clock or a stack. The failed-job path goes through a real,
started `JobQueue`, because the hook is only worth anything if the worker reaches it.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest

for _key, _value in {
    "OIDC_ISSUER_KC_AUTH": "https://kc.test/auth",
    "OIDC_ISSUER_KC_TOKEN": "https://kc.test/token",
    "OIDC_ISSUER_KC_CERTS": "https://kc.test/certs",
    "MANAGER_ADMIN_ROLE": "admin",
    "MANAGER_USER_ROLE": "user",
    "MANAGER_HOST": "https://manager.test",
    "MANAGER_OIDC_CLIENT_SECRET": "client-secret",
    "MANAGER_SESSION_SECRET": "test-session-secret-value",
    "PAPAIA_CONFIG_DIR": tempfile.mkdtemp(prefix="papaia-notify-config-"),
    "PAPAIA_WORKSPACE_DIR": tempfile.mkdtemp(prefix="papaia-notify-workspace-"),
}.items():
    os.environ.setdefault(_key, _value)

from app.config import Settings  # noqa: E402
from app.core import services  # noqa: E402
from app.core.audit import audit_path  # noqa: E402
from app.core.jobs import Job, JobContext, JobQueue, JobStatus  # noqa: E402
from app.core.notifications import detect, store  # noqa: E402
from app.core.notifications.detect import Condition, Finding  # noqa: E402
from app.core.notifications.model import Event, EventSettings, Kind  # noqa: E402
from app.core.notifications.service import NotificationService, send_test  # noqa: E402

_HOOK = "https://hooks.example.com/services/T000/B000/s3cr3t-token"
_OTHER_HOOK = "https://other.example.com/hook"


class _Receiver:
    """Every webhook post, by URL."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.posts: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.posts.append((str(request.url), json.loads(request.content)))
        return httpx.Response(self.status)

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)

    def to(self, url: str) -> list[dict[str, Any]]:
        return [body for target, body in self.posts if target == url]


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    config = tmp_path / "config"
    config.mkdir()
    return Settings(  # type: ignore[call-arg]
        papaia_config_dir=str(config), papaia_workspace_dir=str(tmp_path / "workspace")
    )


@pytest.fixture
def receiver() -> _Receiver:
    return _Receiver()


def _subscribe(
    settings: Settings,
    name: str,
    url: str,
    events: list[Event],
    *,
    enabled: bool = True,
) -> str:
    revision = store.load_config(settings.papaia_config_dir).revision
    _, recipient = store.add_recipient(
        settings.papaia_config_dir,
        revision,
        name=name,
        kind=Kind.WEBHOOK,
        target=url,
        enabled=enabled,
        events=events,
    )
    return recipient.id


def _detector(finding: Callable[[], list[Finding]]) -> detect.Detector:
    async def run(_: EventSettings) -> list[Finding]:
        return finding()

    return run


def _service(
    settings: Settings, receiver: _Receiver, detectors: dict[Event, detect.Detector], **kwargs: Any
) -> NotificationService:
    return NotificationService(
        settings, None, detectors=detectors, transport=receiver.transport, retry_delay=0, **kwargs
    )


def _down(key: str = "litellm") -> list[Finding]:
    condition = Condition(
        Event.SERVICE_UNHEALTHY, key, "", key, f"{key} is unhealthy.", {"service": key}
    )
    return [Finding(Event.SERVICE_UNHEALTHY, (condition,))]


def _audit(settings: Settings) -> list[dict[str, Any]]:
    path = audit_path(settings.papaia_config_dir)
    return (
        [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        if path.exists()
        else []
    )


# -- the periodic pass ---------------------------------------------------------------


async def test_a_pass_tells_exactly_the_recipients_who_chose_the_event(
    settings: Settings, receiver: _Receiver
) -> None:
    _subscribe(settings, "Wants services", _HOOK, [Event.SERVICE_UNHEALTHY, Event.JOB_FAILED])
    _subscribe(settings, "Wants updates", _OTHER_HOOK, [Event.UPDATE_AVAILABLE])
    service = _service(settings, receiver, {Event.SERVICE_UNHEALTHY: _detector(_down)})
    result = await service.run_once()
    assert result.notified == 1
    (body,) = receiver.to(_HOOK)
    assert body["event"] == "service_unhealthy"
    assert body["severity"] == "error"
    assert body["message"] == "litellm is unhealthy."
    assert body["details"] == {"service": "litellm"}
    assert receiver.to(_OTHER_HOOK) == []


async def test_a_paused_recipient_hears_nothing(settings: Settings, receiver: _Receiver) -> None:
    _subscribe(settings, "Paused", _HOOK, [Event.SERVICE_UNHEALTHY], enabled=False)
    service = _service(settings, receiver, {Event.SERVICE_UNHEALTHY: _detector(_down)})
    assert (await service.run_once()).notified == 0
    assert receiver.posts == []


async def test_an_event_nobody_chose_is_not_even_looked_for(
    settings: Settings, receiver: _Receiver
) -> None:
    _subscribe(settings, "Services only", _HOOK, [Event.SERVICE_UNHEALTHY])
    looked: list[str] = []

    def spy(event: Event) -> Callable[[], list[Finding]]:
        def run() -> list[Finding]:
            looked.append(event.value)
            return []

        return run

    detectors = {
        Event.SERVICE_UNHEALTHY: _detector(spy(Event.SERVICE_UNHEALTHY)),
        Event.UPDATE_AVAILABLE: _detector(spy(Event.UPDATE_AVAILABLE)),
        Event.CERT_EXPIRING: _detector(spy(Event.CERT_EXPIRING)),
    }
    await _service(settings, receiver, detectors).run_once()
    assert looked == ["service_unhealthy"]


async def test_a_pass_with_no_recipients_looks_at_nothing(
    settings: Settings, receiver: _Receiver
) -> None:
    def never() -> list[Finding]:
        raise AssertionError("no one is subscribed")

    service = _service(settings, receiver, {Event.SERVICE_UNHEALTHY: _detector(never)})
    assert (await service.run_once()).notified == 0


async def test_the_same_condition_is_not_sent_twice(
    settings: Settings, receiver: _Receiver
) -> None:
    _subscribe(settings, "Chat", _HOOK, [Event.SERVICE_UNHEALTHY])
    service = _service(settings, receiver, {Event.SERVICE_UNHEALTHY: _detector(_down)})
    assert (await service.run_once()).notified == 1
    assert (await service.run_once()).notified == 0
    assert len(receiver.posts) == 1


async def test_a_restart_does_not_send_it_again(settings: Settings, receiver: _Receiver) -> None:
    """A new service object is a restarted manager: only the state file carries over."""
    _subscribe(settings, "Chat", _HOOK, [Event.SERVICE_UNHEALTHY])
    detectors = {Event.SERVICE_UNHEALTHY: _detector(_down)}
    await _service(settings, receiver, detectors).run_once()
    assert (await _service(settings, receiver, detectors).run_once()).notified == 0
    assert len(receiver.posts) == 1


async def test_the_state_is_stored_before_the_delivery_so_a_crash_cannot_repeat_it(
    settings: Settings,
) -> None:
    _subscribe(settings, "Chat", _HOOK, [Event.SERVICE_UNHEALTHY])

    def explode(request: httpx.Request) -> httpx.Response:
        raise asyncio.CancelledError  # the manager is gone mid-delivery

    service = NotificationService(
        settings,
        None,
        detectors={Event.SERVICE_UNHEALTHY: _detector(_down)},
        transport=httpx.MockTransport(explode),
        retry_delay=0,
    )
    with pytest.raises(asyncio.CancelledError):
        await service.run_once()
    assert store.load_state(settings.papaia_config_dir).seen["service_unhealthy|litellm"].active


async def test_a_recipient_whose_delivery_fails_is_logged_and_the_next_still_gets_it(
    settings: Settings,
) -> None:
    _subscribe(settings, "Broken", _OTHER_HOOK, [Event.SERVICE_UNHEALTHY])
    _subscribe(settings, "Chat", _HOOK, [Event.SERVICE_UNHEALTHY])

    def answer(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500 if str(request.url) == _OTHER_HOOK else 200)

    service = NotificationService(
        settings,
        None,
        detectors={Event.SERVICE_UNHEALTHY: _detector(_down)},
        transport=httpx.MockTransport(answer),
        retry_delay=0,
    )
    await service.run_once()
    results = {e["target"]: e["action"] for e in _audit(settings)}
    assert results == {"Broken": "notify.failed", "Chat": "notify.sent"}


async def test_a_detector_that_breaks_costs_its_event_and_not_the_pass(
    settings: Settings, receiver: _Receiver
) -> None:
    _subscribe(settings, "Chat", _HOOK, [Event.SERVICE_UNHEALTHY, Event.CERT_EXPIRING])

    def broken() -> list[Finding]:
        raise RuntimeError("docker fell over")

    service = _service(
        settings,
        receiver,
        {Event.CERT_EXPIRING: _detector(broken), Event.SERVICE_UNHEALTHY: _detector(_down)},
    )
    assert (await service.run_once()).notified == 1


async def test_the_interval_is_the_one_in_the_settings_and_is_read_every_pass(
    settings: Settings, receiver: _Receiver
) -> None:
    service = _service(settings, receiver, {})
    assert (await service.run_once()).interval_seconds == 300
    revision = store.load_config(settings.papaia_config_dir).revision
    store.save_settings(
        settings.papaia_config_dir, revision, EventSettings(check_interval_minutes=12)
    )
    assert (await service.run_once()).interval_seconds == 720


async def test_a_configuration_nobody_can_read_means_no_pass_and_no_crash(
    settings: Settings, receiver: _Receiver
) -> None:
    path = store.config_path(settings.papaia_config_dir)
    path.parent.mkdir(parents=True)
    path.write_text("surprise: 1\n", encoding="utf-8")
    service = _service(settings, receiver, {Event.SERVICE_UNHEALTHY: _detector(_down)})
    assert (await service.run_once()).notified == 0
    assert receiver.posts == []


async def test_release_tags_are_not_fetched_on_every_pass(
    settings: Settings, receiver: _Receiver
) -> None:
    _subscribe(settings, "Chat", _HOOK, [Event.UPDATE_AVAILABLE])
    looked: list[int] = []

    def updates() -> list[Finding]:
        looked.append(1)
        return [Finding(Event.UPDATE_AVAILABLE, (), scope="core")]

    service = _service(settings, receiver, {Event.UPDATE_AVAILABLE: _detector(updates)})
    for _ in range(3):
        await service.run_once()
    assert looked == [1]


async def test_a_pass_notes_when_it_ran(settings: Settings, receiver: _Receiver) -> None:
    service = _service(settings, receiver, {})
    assert service.last_pass_at is None
    await service.run_once()
    assert service.last_pass_at is not None


# -- failed jobs ---------------------------------------------------------------------


async def _run_jobs(
    settings: Settings, service: NotificationService, *callbacks: Callable[[JobContext], Any]
) -> JobQueue:
    queue = JobQueue(settings.papaia_config_dir)
    queue.add_finished_listener(service.on_job_finished)
    queue.start()
    try:
        for index, callback in enumerate(callbacks):
            await queue.enqueue(
                action="backup", target=f"core-{index}", user="marko", callback=callback
            )
        await asyncio.wait_for(queue._queue.join(), timeout=5)  # noqa: SLF001
        await service.settled()
    finally:
        queue.stop()
    return queue


async def _fails(ctx: JobContext) -> None:
    raise RuntimeError("secret detail the log holds: hunter2")


async def _works(ctx: JobContext) -> None:
    ctx.log("fine")


async def test_a_failed_job_is_reported_to_whoever_chose_it_and_to_no_one_else(
    settings: Settings, receiver: _Receiver
) -> None:
    _subscribe(settings, "Wants jobs", _HOOK, [Event.JOB_FAILED])
    _subscribe(settings, "Wants services", _OTHER_HOOK, [Event.SERVICE_UNHEALTHY])
    service = _service(settings, receiver, {})
    queue = await _run_jobs(settings, service, _fails)
    (job,) = queue.list_jobs()
    assert job.status is JobStatus.FAILED
    (body,) = receiver.to(_HOOK)
    assert body["event"] == "job_failed" and body["severity"] == "error"
    assert body["message"] == "The job backup core-0 failed."
    assert body["details"] == {
        "job_id": job.id,
        "action": "backup",
        "target": "core-0",
        "started_by": "marko",
        "exit_code": 1,
    }
    assert receiver.to(_OTHER_HOOK) == []


async def test_what_the_job_wrote_or_raised_never_leaves_the_manager(
    settings: Settings, receiver: _Receiver
) -> None:
    _subscribe(settings, "Chat", _HOOK, [Event.JOB_FAILED])
    await _run_jobs(settings, _service(settings, receiver, {}), _fails)
    assert "hunter2" not in json.dumps(receiver.posts)


async def test_a_job_that_succeeds_says_nothing(settings: Settings, receiver: _Receiver) -> None:
    _subscribe(settings, "Chat", _HOOK, [Event.JOB_FAILED])
    await _run_jobs(settings, _service(settings, receiver, {}), _works)
    assert receiver.posts == []


async def test_every_failed_job_is_its_own_notification(
    settings: Settings, receiver: _Receiver
) -> None:
    _subscribe(settings, "Chat", _HOOK, [Event.JOB_FAILED])
    await _run_jobs(settings, _service(settings, receiver, {}), _fails, _works, _fails)
    assert sorted(b["message"] for b in receiver.to(_HOOK)) == [
        "The job backup core-0 failed.",
        "The job backup core-2 failed.",
    ]


async def test_a_listener_that_raises_does_not_stop_the_queue(
    settings: Settings, receiver: _Receiver
) -> None:
    queue = JobQueue(settings.papaia_config_dir)

    def broken(_: Any) -> None:
        raise RuntimeError("listener bug")

    queue.add_finished_listener(broken)
    queue.start()
    try:
        await queue.enqueue(action="a", target="x", user="u", callback=_works)
        await queue.enqueue(action="b", target="x", user="u", callback=_works)
        await asyncio.wait_for(queue._queue.join(), timeout=5)  # noqa: SLF001
    finally:
        queue.stop()
    assert [j.status for j in queue.list_jobs()] == [JobStatus.SUCCEEDED, JobStatus.SUCCEEDED]


# -- the test button -----------------------------------------------------------------


async def test_a_test_goes_to_one_recipient_and_the_result_is_remembered(
    settings: Settings, receiver: _Receiver
) -> None:
    rid = _subscribe(settings, "Chat", _HOOK, [Event.JOB_FAILED])
    _subscribe(settings, "Other", _OTHER_HOOK, [Event.JOB_FAILED])
    outcome = await send_test(settings, rid, actor="marko", transport=receiver.transport)
    assert outcome is not None and outcome.ok
    (body,) = receiver.to(_HOOK)
    assert body["event"] == "test" and body["severity"] == "info"
    assert receiver.to(_OTHER_HOOK) == []
    remembered = store.load_state(settings.papaia_config_dir).tests[rid]
    assert remembered.ok and remembered.reason == ""
    (entry,) = _audit(settings)
    assert entry["user"] == "marko" and entry["params"]["event"] == "test"


async def test_a_failed_test_is_remembered_with_its_reason(settings: Settings) -> None:
    rid = _subscribe(settings, "Chat", _HOOK, [Event.JOB_FAILED])
    outcome = await send_test(
        settings, rid, actor="marko", transport=_Receiver(404).transport, retry_delay=0
    )
    assert outcome is not None and not outcome.ok
    remembered = store.load_state(settings.papaia_config_dir).tests[rid]
    assert not remembered.ok and remembered.reason == "the webhook answered 404"


async def test_a_test_for_a_recipient_that_is_gone_is_none(settings: Settings) -> None:
    assert await send_test(settings, "nope", actor="marko") is None


# -- the loop ------------------------------------------------------------------------


async def test_the_loop_waits_for_the_stack_to_settle_and_then_runs(
    settings: Settings, receiver: _Receiver
) -> None:
    _subscribe(settings, "Chat", _HOOK, [Event.SERVICE_UNHEALTHY])
    service = _service(
        settings, receiver, {Event.SERVICE_UNHEALTHY: _detector(_down)}, first_pass_delay=0.05
    )
    service.start()
    try:
        assert receiver.posts == []
        for _ in range(100):
            if receiver.posts:
                break
            await asyncio.sleep(0.02)
        assert len(receiver.posts) == 1
    finally:
        service.shutdown()
        await service.stopped()


# -- a service that was running and is gone ------------------------------------------


def _stack(health: services.ServiceHealth | None) -> services.StackSnapshot | None:
    """Docker's answer: `localai` in this health, with `keycloak` healthy; None for no answer."""
    if health is None:
        return None
    container = services.ServiceContainer(
        name="localai-1" if health is not services.ServiceHealth.MISSING else "",
        service="localai",
        role="",
        state="x",
        status_text="x",
        health=health,
    )
    keycloak = services.ServiceContainer(
        name="keycloak-1",
        service="keycloak",
        role="",
        state="running",
        status_text="Up",
        health=services.ServiceHealth.HEALTHY,
    )
    return services.StackSnapshot(
        core=[
            services.ServiceModule(name="keycloak", containers=[keycloak]),
            services.ServiceModule(name="localai", containers=[container]),
        ]
    )


class _Docker:
    """What `detect` is told about the stack: one answer at a time, and whether it is busy."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.answer: services.ServiceHealth | None = services.ServiceHealth.HEALTHY
        self.busy = False

        async def busy(_: Any) -> bool:
            return self.busy

        monkeypatch.setattr(detect, "read_services", lambda *_: _stack(self.answer))
        monkeypatch.setattr(detect, "stack_is_busy", busy)


def _real_detectors(settings: Settings) -> dict[Event, detect.Detector]:
    return {
        Event.SERVICE_UNHEALTHY: detect.build_detectors(settings, None)[Event.SERVICE_UNHEALTHY]
    }


async def test_a_service_stopped_with_clean_up_is_reported_once_it_was_seen_running(
    settings: Settings, receiver: _Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    _subscribe(settings, "Chat", _HOOK, [Event.SERVICE_UNHEALTHY])
    docker = _Docker(monkeypatch)
    service = _service(settings, receiver, _real_detectors(settings))

    assert (await service.run_once()).notified == 0  # running: remembered, nothing to say
    assert store.load_state(settings.papaia_config_dir).up == {"keycloak", "localai"}

    docker.answer = services.ServiceHealth.MISSING  # "Clean up": the containers are gone
    assert (await service.run_once()).notified == 1
    (body,) = receiver.to(_HOOK)
    assert body["event"] == "service_unhealthy"
    assert body["message"] == "localai is not running: its containers are gone."
    assert body["details"]["state"] == "missing"

    assert (await service.run_once()).notified == 0  # still gone: said once
    assert len(receiver.posts) == 1


async def test_a_service_that_was_never_seen_running_says_nothing_when_it_is_gone(
    settings: Settings, receiver: _Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    _subscribe(settings, "Chat", _HOOK, [Event.SERVICE_UNHEALTHY])
    docker = _Docker(monkeypatch)
    docker.answer = services.ServiceHealth.MISSING
    service = _service(settings, receiver, _real_detectors(settings))
    assert (await service.run_once()).notified == 0
    assert store.load_state(settings.papaia_config_dir).up == {"keycloak"}
    assert receiver.posts == []


async def test_a_restart_remembers_what_was_running_and_repeats_nothing(
    settings: Settings, receiver: _Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    _subscribe(settings, "Chat", _HOOK, [Event.SERVICE_UNHEALTHY])
    docker = _Docker(monkeypatch)
    await _service(settings, receiver, _real_detectors(settings)).run_once()
    docker.answer = services.ServiceHealth.MISSING
    await _service(settings, receiver, _real_detectors(settings)).run_once()
    assert len(receiver.posts) == 1
    # The manager restarts: its memory is the state file.
    assert (await _service(settings, receiver, _real_detectors(settings)).run_once()).notified == 0
    assert len(receiver.posts) == 1


async def test_a_pass_while_the_stack_is_busy_neither_reports_nor_forgets(
    settings: Settings, receiver: _Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    _subscribe(settings, "Chat", _HOOK, [Event.SERVICE_UNHEALTHY])
    docker = _Docker(monkeypatch)
    service = _service(settings, receiver, _real_detectors(settings))
    await service.run_once()
    docker.answer = services.ServiceHealth.MISSING
    docker.busy = True  # the stop is still being carried out
    assert (await service.run_once()).notified == 0
    assert store.load_state(settings.papaia_config_dir).up == {"keycloak", "localai"}
    docker.busy = False  # the job is over, the service is still gone
    assert (await service.run_once()).notified == 1


async def test_a_docker_that_does_not_answer_neither_reports_nor_forgets(
    settings: Settings, receiver: _Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    _subscribe(settings, "Chat", _HOOK, [Event.SERVICE_UNHEALTHY])
    docker = _Docker(monkeypatch)
    service = _service(settings, receiver, _real_detectors(settings))
    await service.run_once()
    docker.answer = None
    assert (await service.run_once()).notified == 0
    assert store.load_state(settings.papaia_config_dir).up == {"keycloak", "localai"}
    docker.answer = services.ServiceHealth.MISSING
    assert (await service.run_once()).notified == 1


# -- the interval ----------------------------------------------------------------------


def _interval(monkeypatch: pytest.MonkeyPatch, service: NotificationService, seconds: list[float]):
    """The check interval as the page would change it: whatever `seconds[0]` is now."""
    monkeypatch.setattr(service, "_interval_seconds", lambda: seconds[0])


async def test_the_interval_is_the_one_in_the_configuration(
    settings: Settings, receiver: _Receiver
) -> None:
    service = _service(settings, receiver, {})
    assert service._interval_seconds() == 300  # noqa: SLF001 -- no file: the default
    revision = store.load_config(settings.papaia_config_dir).revision
    store.save_settings(
        settings.papaia_config_dir, revision, EventSettings(check_interval_minutes=3)
    )
    assert service._interval_seconds() == 180  # noqa: SLF001


async def test_a_file_nobody_can_read_leaves_the_default_interval(
    settings: Settings, receiver: _Receiver
) -> None:
    path = store.config_path(settings.papaia_config_dir)
    path.parent.mkdir(parents=True)
    path.write_text("surprise: 1\n", encoding="utf-8")
    assert _service(settings, receiver, {})._interval_seconds() == 300  # noqa: SLF001


async def test_a_shorter_interval_takes_effect_while_the_loop_waits(
    settings: Settings, receiver: _Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bug: the sleep was as long as the interval read before the pass, so lowering 5 to 3
    minutes changed nothing until the five had run out."""
    service = _service(settings, receiver, {}, settings_poll=0.01)
    seconds = [3600.0]
    _interval(monkeypatch, service, seconds)
    waiting = asyncio.create_task(service._wait_for_next(time.monotonic()))  # noqa: SLF001
    await asyncio.sleep(0.1)
    assert not waiting.done()  # an hour is an hour
    seconds[0] = 0.0  # "3 minutes" saved on the page, with more than that already gone
    await asyncio.wait_for(waiting, timeout=2)


async def test_a_longer_interval_takes_effect_while_the_loop_waits(
    settings: Settings, receiver: _Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(settings, receiver, {}, settings_poll=0.01)
    seconds = [0.15]
    _interval(monkeypatch, service, seconds)
    waiting = asyncio.create_task(service._wait_for_next(time.monotonic()))  # noqa: SLF001
    await asyncio.sleep(0.05)
    seconds[0] = 3600.0  # lengthened before the first one ran out
    await asyncio.sleep(0.3)
    assert not waiting.done()
    waiting.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiting


async def test_the_next_pass_is_due_one_interval_after_the_last_began(
    settings: Settings, receiver: _Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(settings, receiver, {}, settings_poll=0.01)
    seconds = [10.0]
    _interval(monkeypatch, service, seconds)
    # The last pass began 4 seconds ago; with 3 seconds as the new interval it is overdue.
    seconds[0] = 3.0
    await asyncio.wait_for(service._wait_for_next(time.monotonic() - 4.0), timeout=1)  # noqa: SLF001


async def test_the_running_loop_passes_again_when_the_interval_is_shortened(
    settings: Settings, receiver: _Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    _subscribe(settings, "Chat", _HOOK, [Event.SERVICE_UNHEALTHY])
    looked: list[int] = []

    def watched() -> list[Finding]:
        looked.append(1)
        return []

    service = _service(
        settings,
        receiver,
        {Event.SERVICE_UNHEALTHY: _detector(watched)},
        first_pass_delay=0,
        settings_poll=0.01,
    )
    seconds = [3600.0]
    _interval(monkeypatch, service, seconds)
    service.start()
    try:
        for _ in range(100):
            if looked:
                break
            await asyncio.sleep(0.02)
        assert len(looked) == 1
        await asyncio.sleep(0.2)
        assert len(looked) == 1  # an hour away
        seconds[0] = 0.05
        for _ in range(100):
            if len(looked) >= 2:
                break
            await asyncio.sleep(0.02)
        assert len(looked) >= 2
    finally:
        service.shutdown()
        await service.stopped()


# -- a job that ends -------------------------------------------------------------------


def _finished(status: JobStatus = JobStatus.SUCCEEDED) -> Job:
    return Job(
        id="j-1",
        action="group-start",
        target="localai",
        user="admin",
        created_at=datetime.now(tz=UTC),
        status=status,
    )


async def _until(condition: Callable[[], object], seconds: float = 3.0) -> None:
    for _ in range(int(seconds / 0.02)):
        if condition():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("did not happen in time")


async def test_a_job_that_ends_cuts_the_wait_for_the_next_pass_short(
    settings: Settings, receiver: _Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = _service(settings, receiver, {}, settings_poll=0.01)
    _interval(monkeypatch, service, [3600.0])
    waiting = asyncio.create_task(service._wait_for_next(time.monotonic()))  # noqa: SLF001
    await asyncio.sleep(0.1)
    assert not waiting.done()
    service.on_job_finished(_finished())  # a start that worked: nothing to report about it
    await asyncio.wait_for(waiting, timeout=2)
    assert receiver.posts == []


async def test_a_job_that_ended_before_the_first_pass_does_not_ask_for_a_second_one(
    settings: Settings, receiver: _Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    _subscribe(settings, "Chat", _HOOK, [Event.SERVICE_UNHEALTHY])
    looked: list[int] = []

    def watched() -> list[Finding]:
        looked.append(1)
        return []

    service = _service(
        settings,
        receiver,
        {Event.SERVICE_UNHEALTHY: _detector(watched)},
        first_pass_delay=0.2,
        settings_poll=0.01,
    )
    _interval(monkeypatch, service, [3600.0])
    service.start()
    try:
        service.on_job_finished(_finished())  # during the wait for the first pass
        await _until(lambda: looked)
        await asyncio.sleep(0.3)
        assert len(looked) == 1
    finally:
        service.shutdown()
        await service.stopped()


async def test_a_service_started_and_stopped_in_the_manager_within_one_interval_is_reported(
    settings: Settings, receiver: _Receiver, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The case that was missed: `localai` started with the Services page, stopped with "Clean
    up" a minute later, an interval of minutes in between. The start job's end asks for a pass,
    which sees the service deployed; the stop job's end asks for another, which finds it gone."""
    _subscribe(settings, "Chat", _HOOK, [Event.SERVICE_UNHEALTHY])
    docker = _Docker(monkeypatch)
    docker.answer = services.ServiceHealth.MISSING
    service = _service(
        settings, receiver, _real_detectors(settings), first_pass_delay=0, settings_poll=0.01
    )
    _interval(monkeypatch, service, [3600.0])  # only a job that ends can ask for the next pass
    queue = JobQueue(settings.papaia_config_dir)
    queue.add_finished_listener(service.on_job_finished)
    queue.start()
    service.start()
    try:
        up = lambda: store.load_state(settings.papaia_config_dir).up  # noqa: E731
        await _until(lambda: up() == {"keycloak"})  # the first pass: localai is not deployed

        docker.answer = services.ServiceHealth.STARTING  # "Start": still starting at the end
        await queue.enqueue(action="group-start", target="localai", user="admin", callback=_works)
        await _until(lambda: "localai" in up())
        assert receiver.posts == []

        docker.answer = services.ServiceHealth.MISSING  # "Stop" with "Clean up"
        await queue.enqueue(action="group-stop", target="localai", user="admin", callback=_works)
        await _until(lambda: receiver.posts)
        (body,) = receiver.to(_HOOK)
        assert body["message"] == "localai is not running: its containers are gone."
    finally:
        queue.stop()
        service.shutdown()
        await service.stopped()
