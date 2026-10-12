"""Carrying out a notification: the email, the webhook post, and what is written down.

The webhook goes through `httpx.MockTransport`, so what leaves the manager is what is checked.
The relay is a stand-in for `probe.send_async`. The audit log is read back from the temporary
configuration directory, because what it must never contain is as much the point as what it does.
"""

from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime
from email.message import EmailMessage
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
from app.core.audit import audit_path  # noqa: E402
from app.core.mail import probe as mail_probe  # noqa: E402
from app.core.mail.settings import MailSettings  # noqa: E402
from app.core.notifications import delivery  # noqa: E402
from app.core.notifications.delivery import Notification, build_email, deliver  # noqa: E402
from app.core.notifications.model import Event, Kind, Recipient, Severity  # noqa: E402

_HOOK = "https://hooks.example.com/services/T000/B000/s3cr3t-token"
_ADDRESS = "ops@customer.example"
_WHEN = datetime(2026, 10, 12, 14, 2, tzinfo=UTC)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    workspace = tmp_path / "workspace" / "papaia" / "tools" / "lib"
    workspace.mkdir(parents=True)
    (workspace / "smtp.py").write_text("", encoding="utf-8")
    (workspace / "doctor.py").write_text('    ("smtp", check_smtp),\n', encoding="utf-8")
    config = tmp_path / "config"
    config.mkdir()
    module = config / "infra" / "smtp-relay"
    module.mkdir(parents=True)
    (config / ".env").write_text("COMPOSE_PROFILES=nginx,smtp\n", encoding="utf-8")
    (module / ".env").write_text(
        "SMTP_RELAY_UPSTREAM_HOST=smtp.example.org\n"
        "SMTP_RELAY_FROM_ADDRESS=noreply@example.org\n"
        "SMTP_RELAY_FROM_NAME=Our Mail\n",
        encoding="utf-8",
    )
    return Settings(  # type: ignore[call-arg]
        papaia_config_dir=str(config),
        papaia_workspace_dir=str(tmp_path / "workspace"),
    )


def _recipient(kind: Kind, target: str, *, name: str = "Ops", rid: str = "abc12345") -> Recipient:
    return Recipient(id=rid, name=name, kind=kind, target=target, events=[Event.JOB_FAILED])


def _notification(**changes: Any) -> Notification:
    values: dict[str, Any] = {
        "event": "job_failed",
        "severity": Severity.ERROR,
        "message": "The job backup core failed.",
        "subject": "backup core",
        "details": {"job_id": "j-1", "exit_code": 1},
        "timestamp": _WHEN,
    }
    values.update(changes)
    return Notification(**values)


def _log(settings: Settings) -> list[dict[str, Any]]:
    path = audit_path(settings.papaia_config_dir)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


class _Hook:
    """A webhook receiver: answers what it is told to, in order, and keeps what it got."""

    def __init__(self, *answers: int | Exception) -> None:
        self.answers = list(answers) or [200]
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, Exception):
            raise answer
        return httpx.Response(answer, headers={"location": "https://elsewhere.example/x"})

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self)


# -- the payload ---------------------------------------------------------------------


def test_the_webhook_gets_the_documented_json() -> None:
    assert _notification().payload() == {
        "event": "job_failed",
        "severity": "error",
        "message": "The job backup core failed.",
        "timestamp": "2026-10-12T14:02:00+00:00",
        "details": {"job_id": "j-1", "exit_code": 1},
    }


async def test_a_webhook_post_carries_the_payload_and_nothing_else(settings: Settings) -> None:
    hook = _Hook(200)
    (outcome,) = await deliver(
        _notification(),
        [_recipient(Kind.WEBHOOK, _HOOK)],
        settings=settings,
        transport=hook.transport,
    )
    assert outcome.ok and outcome.reason == ""
    (request,) = hook.requests
    assert str(request.url) == _HOOK
    assert request.method == "POST"
    assert json.loads(request.content) == _notification().payload()
    assert request.headers["content-type"] == "application/json"


# -- the webhook's answers -----------------------------------------------------------


async def test_a_server_error_is_tried_once_more(settings: Settings) -> None:
    hook = _Hook(502, 200)
    (outcome,) = await deliver(
        _notification(),
        [_recipient(Kind.WEBHOOK, _HOOK)],
        settings=settings,
        transport=hook.transport,
        retry_delay=0,
    )
    assert outcome.ok
    assert len(hook.requests) == 2


async def test_a_server_that_keeps_failing_is_tried_twice_and_no_more(settings: Settings) -> None:
    hook = _Hook(503)
    (outcome,) = await deliver(
        _notification(),
        [_recipient(Kind.WEBHOOK, _HOOK)],
        settings=settings,
        transport=hook.transport,
        retry_delay=0,
    )
    assert not outcome.ok
    assert outcome.reason == "the webhook answered 503"
    assert len(hook.requests) == 2


async def test_a_refusal_is_not_repeated(settings: Settings) -> None:
    hook = _Hook(404)
    (outcome,) = await deliver(
        _notification(),
        [_recipient(Kind.WEBHOOK, _HOOK)],
        settings=settings,
        transport=hook.transport,
        retry_delay=0,
    )
    assert not outcome.ok and outcome.reason == "the webhook answered 404"
    assert len(hook.requests) == 1


async def test_a_redirect_is_not_followed_and_not_repeated(settings: Settings) -> None:
    hook = _Hook(302)
    (outcome,) = await deliver(
        _notification(),
        [_recipient(Kind.WEBHOOK, _HOOK)],
        settings=settings,
        transport=hook.transport,
        retry_delay=0,
    )
    assert not outcome.ok
    assert "302" in outcome.reason and "redirects are not followed" in outcome.reason
    assert len(hook.requests) == 1


@pytest.mark.parametrize(
    ("failure", "words"),
    [
        (httpx.ConnectTimeout("timed out"), "did not answer in time"),
        (httpx.ReadTimeout("timed out"), "did not answer in time"),
        (httpx.ConnectError("refused"), "could not connect"),
        (httpx.RemoteProtocolError("garbage"), "RemoteProtocolError"),
    ],
)
async def test_a_network_failure_is_tried_again_and_then_explained_without_the_url(
    settings: Settings, failure: Exception, words: str
) -> None:
    hook = _Hook(failure)
    (outcome,) = await deliver(
        _notification(),
        [_recipient(Kind.WEBHOOK, _HOOK)],
        settings=settings,
        transport=hook.transport,
        retry_delay=0,
    )
    assert not outcome.ok
    assert words in outcome.reason
    assert "s3cr3t" not in outcome.reason and "hooks.example.com" not in outcome.reason
    assert len(hook.requests) == 2


# -- the email -----------------------------------------------------------------------


def _mail() -> MailSettings:
    return MailSettings(
        profile_on=True,
        host="smtp.example.org",
        from_address="noreply@example.org",
        from_name="Our Mail",
    )


def test_the_email_says_what_happened_and_where_it_came_from() -> None:
    message = build_email(_notification(), to=_ADDRESS, mail=_mail(), link="https://manager.test")
    assert message["To"] == _ADDRESS
    assert "noreply@example.org" in message["From"] and "Our Mail" in message["From"]
    assert message["Subject"] == "[papAIa] Job failed: The job backup core failed."
    body = message.get_content()
    assert "The job backup core failed." in body
    assert "Severity: error" in body
    assert "Time:     2026-10-12 14:02 UTC" in body
    assert "job_id: j-1" in body and "exit_code: 1" in body
    assert "https://manager.test" in body
    assert message["Message-ID"].endswith("@example.org>")


def test_a_header_cannot_be_split_by_what_a_name_contains() -> None:
    nasty = _notification(message="litellm\r\nBcc: attacker@example.org\r\n is unhealthy")
    message = build_email(nasty, to=_ADDRESS, mail=_mail(), link="https://manager.test")
    assert "\n" not in message["Subject"] and "\r" not in message["Subject"]
    assert message["Bcc"] is None


async def test_an_email_goes_to_the_relay_and_is_recorded_without_the_address(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[EmailMessage] = []

    async def send(message: EmailMessage) -> mail_probe.SendResult:
        sent.append(message)
        return mail_probe.SendResult(True)

    monkeypatch.setattr(delivery.mail_probe, "send_async", send)
    (outcome,) = await deliver(
        _notification(), [_recipient(Kind.EMAIL, _ADDRESS)], settings=settings
    )
    assert outcome.ok
    (message,) = sent
    assert message["To"] == _ADDRESS
    (entry,) = _log(settings)
    assert entry["action"] == "notify.sent" and entry["result"] == "ok"
    assert entry["target"] == "Ops" and entry["user"] == "notifier"
    assert entry["params"] == {"event": "job_failed", "subject": "backup core"}
    assert _ADDRESS not in json.dumps(entry)


async def test_a_refusal_by_the_relay_is_the_recipients_failure_in_the_relays_words(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def send(message: EmailMessage) -> mail_probe.SendResult:
        return mail_probe.SendResult(False, "the relay refused the recipient: 550 no such domain")

    monkeypatch.setattr(delivery.mail_probe, "send_async", send)
    (outcome,) = await deliver(
        _notification(), [_recipient(Kind.EMAIL, _ADDRESS)], settings=settings
    )
    assert not outcome.ok and "550" in outcome.reason
    (entry,) = _log(settings)
    assert entry["action"] == "notify.failed" and entry["result"] == "failed"
    assert "550" in entry["params"]["reason"]


async def test_email_waits_for_a_mail_server_that_is_not_set_up(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    (Path(settings.papaia_config_dir) / ".env").write_text(
        "COMPOSE_PROFILES=nginx\n", encoding="utf-8"
    )

    async def send(message: EmailMessage) -> mail_probe.SendResult:
        raise AssertionError("nothing may be sent without a Mail Server")

    monkeypatch.setattr(delivery.mail_probe, "send_async", send)
    (outcome,) = await deliver(
        _notification(), [_recipient(Kind.EMAIL, _ADDRESS)], settings=settings
    )
    assert not outcome.ok and outcome.reason == "the Mail Server is not set up"


async def test_a_webhook_does_not_wait_for_the_mail_server(settings: Settings) -> None:
    (Path(settings.papaia_config_dir) / ".env").write_text(
        "COMPOSE_PROFILES=nginx\n", encoding="utf-8"
    )
    hook = _Hook(200)
    (outcome,) = await deliver(
        _notification(),
        [_recipient(Kind.WEBHOOK, _HOOK)],
        settings=settings,
        transport=hook.transport,
    )
    assert outcome.ok


# -- several recipients, and what is written down ------------------------------------


async def test_one_recipient_that_fails_does_not_hold_up_the_next(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def send(message: EmailMessage) -> mail_probe.SendResult:
        raise RuntimeError(f"boom with {message['To']}")

    monkeypatch.setattr(delivery.mail_probe, "send_async", send)
    hook = _Hook(200)
    outcomes = await deliver(
        _notification(),
        [
            _recipient(Kind.EMAIL, _ADDRESS, name="Mail", rid="aaaaaaaa"),
            _recipient(Kind.WEBHOOK, _HOOK, name="Chat", rid="bbbbbbbb"),
        ],
        settings=settings,
        transport=hook.transport,
    )
    assert [(o.name, o.ok) for o in outcomes] == [("Mail", False), ("Chat", True)]
    # What an exception says can carry the address: only its class is kept.
    assert outcomes[0].reason == "unexpected error (RuntimeError)"
    assert _ADDRESS not in json.dumps(_log(settings))


async def test_the_log_never_holds_a_webhook_url_not_even_when_the_post_fails(
    settings: Settings,
) -> None:
    hook = _Hook(httpx.ConnectError(f"cannot reach {_HOOK}"))
    await deliver(
        _notification(),
        [_recipient(Kind.WEBHOOK, _HOOK)],
        settings=settings,
        transport=hook.transport,
        retry_delay=0,
    )
    raw = audit_path(settings.papaia_config_dir).read_text(encoding="utf-8")
    assert "s3cr3t" not in raw and "hooks.example.com" not in raw


async def test_a_test_notification_is_labelled_as_one_and_recorded_under_the_administrator(
    settings: Settings,
) -> None:
    hook = _Hook(200)
    test = Notification(
        event=delivery.TEST_EVENT,
        severity=Severity.INFO,
        message="This is a test notification from the papAIa manager.",
        timestamp=_WHEN,
    )
    await deliver(
        test,
        [_recipient(Kind.WEBHOOK, _HOOK)],
        settings=settings,
        transport=hook.transport,
        actor="marko",
    )
    assert test.label == "Test notification"
    (entry,) = _log(settings)
    assert entry["user"] == "marko" and entry["params"]["event"] == "test"


async def test_without_recipients_nothing_is_done(settings: Settings) -> None:
    assert await deliver(_notification(), [], settings=settings) == []
    assert _log(settings) == []
