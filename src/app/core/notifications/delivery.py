"""Carrying out one notification: an email through the mail relay, a JSON post to a webhook.

What a notification says is decided before it gets here. This only delivers it, to each
recipient on its own, so that one that fails does not hold up the others, and records the
outcome in the audit log under the recipient's *name*. The address and the URL appear nowhere
a person or a log could read them back: not in a result, not in a reason, not in the log.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid
from typing import Any

import httpx

from app.config import Settings
from app.core.audit import write_audit_entry
from app.core.mail import probe as mail_probe
from app.core.mail.settings import MailSettings, load_mail_settings, supported
from app.core.notifications.model import INFO_BY_EVENT, Event, Kind, Recipient, Severity
from app.core.qdrant import tls_verify

logger = logging.getLogger(__name__)

NOTIFIER_USER = "notifier"
TEST_EVENT = "test"

WEBHOOK_TIMEOUT_SECONDS = 10.0
RETRY_DELAY_SECONDS = 2.0

_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]+")


@dataclass(frozen=True)
class Notification:
    """What is sent. `event` is an `Event` value, or `test`."""

    event: str
    severity: Severity
    message: str
    subject: str = ""
    details: dict[str, Any] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=UTC))

    @property
    def label(self) -> str:
        if self.event == TEST_EVENT:
            return "Test notification"
        return INFO_BY_EVENT[Event(self.event)].label

    def payload(self) -> dict[str, Any]:
        """The body of a webhook post. Plain data, the shape the documentation promises."""
        return {
            "event": self.event,
            "severity": self.severity.value,
            "message": self.message,
            "timestamp": self.timestamp.isoformat(timespec="seconds"),
            "details": self.details,
        }


@dataclass(frozen=True)
class Outcome:
    recipient_id: str
    name: str
    ok: bool
    reason: str = ""


def _one_line(text: str, limit: int = 200) -> str:
    return _CONTROL_RE.sub(" ", text).strip()[:limit]


def build_email(
    notification: Notification, *, to: str, mail: MailSettings, link: str
) -> EmailMessage:
    message = EmailMessage()
    message["From"] = formataddr((_one_line(mail.from_name), mail.from_address))
    message["To"] = to
    message["Subject"] = _one_line(f"[papAIa] {notification.label}: {notification.message}", 150)
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=mail.from_address.rsplit("@", 1)[1])
    lines = [
        notification.message,
        "",
        f"Event:    {notification.label}",
        f"Severity: {notification.severity.value}",
        f"Time:     {notification.timestamp:%Y-%m-%d %H:%M} UTC",
    ]
    if notification.details:
        lines.append("")
        lines += [
            f"{_one_line(str(key), 40)}: {_one_line(str(value))}"
            for key, value in notification.details.items()
            if value is not None
        ]
    lines += [
        "",
        "-- ",
        f"Sent by the papAIa manager ({link}).",
        "You get this because of the notification settings of the manager (Mail, Notifications).",
    ]
    message.set_content("\n".join(lines) + "\n")
    return message


async def send_email(
    recipient: Recipient, notification: Notification, *, mail: MailSettings, link: str
) -> mail_probe.SendResult:
    message = build_email(notification, to=recipient.target, mail=mail, link=link)
    return await mail_probe.send_async(message)


def _webhook_failure(exc: httpx.HTTPError) -> str:
    """Why a post failed, in words that name no host and no URL."""
    if isinstance(exc, httpx.TimeoutException):
        return "the webhook did not answer in time"
    if isinstance(exc, httpx.ConnectError):
        return "could not connect to the webhook"
    return f"the post failed ({type(exc).__name__})"


async def post_webhook(
    url: str,
    notification: Notification,
    *,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
    retry_delay: float = RETRY_DELAY_SECONDS,
) -> mail_probe.SendResult:
    """POST the notification as JSON. One more try after a network error or a 5xx answer; none
    after a 4xx (the receiver understood and said no) or a redirect (not followed: it would
    carry the body somewhere nobody configured). The answer's body is never read."""
    reason = ""
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(WEBHOOK_TIMEOUT_SECONDS),
        follow_redirects=False,
        verify=tls_verify(settings.ssl_cert_file),
        transport=transport,
    ) as client:
        for attempt in (1, 2):
            try:
                response = await client.post(url, json=notification.payload())
            except httpx.HTTPError as exc:
                reason = _webhook_failure(exc)
            else:
                code = response.status_code
                if code < 300:
                    return mail_probe.SendResult(True)
                if 300 <= code < 400:
                    return mail_probe.SendResult(
                        False, f"the webhook answered {code}: redirects are not followed"
                    )
                reason = f"the webhook answered {code}"
                if code < 500:
                    return mail_probe.SendResult(False, reason)
            if attempt == 1:
                await asyncio.sleep(retry_delay)
    return mail_probe.SendResult(False, reason)


async def deliver_one(
    recipient: Recipient,
    notification: Notification,
    *,
    settings: Settings,
    mail: MailSettings,
    mail_supported: bool,
    transport: httpx.AsyncBaseTransport | None = None,
    retry_delay: float = RETRY_DELAY_SECONDS,
    actor: str = NOTIFIER_USER,
) -> Outcome:
    """Deliver to one recipient and write the outcome to the audit log. Never raises."""
    try:
        if recipient.kind is Kind.EMAIL:
            if not (mail_supported and mail.profile_on and mail.configured):
                result = mail_probe.SendResult(False, "the Mail Server is not set up")
            else:
                result = await send_email(
                    recipient, notification, mail=mail, link=settings.manager_host
                )
        else:
            result = await post_webhook(
                recipient.target,
                notification,
                settings=settings,
                transport=transport,
                retry_delay=retry_delay,
            )
    except Exception as exc:
        # Whatever it was, it is this recipient's failure and not the pass's. The class name
        # only: an exception text can carry the address or the URL.
        logger.warning("delivery to a recipient failed: %s", type(exc).__name__)
        result = mail_probe.SendResult(False, f"unexpected error ({type(exc).__name__})")

    params: dict[str, Any] = {"event": notification.event}
    if notification.subject:
        params["subject"] = _one_line(notification.subject, 80)
    if not result.ok:
        params["reason"] = _one_line(result.reason)
    try:
        write_audit_entry(
            settings.papaia_config_dir,
            user=actor,
            action="notify.sent" if result.ok else "notify.failed",
            target=recipient.name,
            params=params,
            result="ok" if result.ok else "failed",
        )
    except OSError as exc:
        logger.warning("the notification log cannot be written: %s", type(exc).__name__)
    return Outcome(recipient.id, recipient.name, result.ok, result.reason)


async def deliver(
    notification: Notification,
    recipients: list[Recipient],
    *,
    settings: Settings,
    transport: httpx.AsyncBaseTransport | None = None,
    retry_delay: float = RETRY_DELAY_SECONDS,
    actor: str = NOTIFIER_USER,
) -> list[Outcome]:
    """Deliver to each recipient in turn. The mail settings are read once for the batch."""
    if not recipients:
        return []
    mail = load_mail_settings(settings.papaia_config_dir)
    is_supported = supported(settings.papaia_workspace_dir)
    return [
        await deliver_one(
            recipient,
            notification,
            settings=settings,
            mail=mail,
            mail_supported=is_supported,
            transport=transport,
            retry_delay=retry_delay,
            actor=actor,
        )
        for recipient in recipients
    ]
