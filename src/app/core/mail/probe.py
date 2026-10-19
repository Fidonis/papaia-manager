"""SMTP conversations the page starts on purpose: the mail server, and the relay.

Two blocking helpers (`smtplib`) and their async wrappers, which run them in a thread:

* `probe_upstream` talks to the mail server the relay forwards to, the way the relay
  does: greeting, EHLO, STARTTLS or TLS from the first byte, and a login. It sends no
  mail. It mirrors `probe_upstream` of the core's `tools/lib/smtp.py`, which `doctor`
  uses on the host and does not use in a container; the manager runs it because an
  operator asked, and from the manager's container, which reaches the provider over the
  same Docker network egress as the relay does.
* `send_test_message` hands one message to the relay on `smtp-relay:587`, the way the
  stack's services do (no login, no TLS). The relay accepting it says nothing about
  delivery: it queues and retries.

Neither writes a password anywhere: it comes in as an argument, goes to the socket and
is not part of any result or log line.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import smtplib
import socket
import ssl
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr, formatdate, make_msgid

from app.core.mail.settings import RELAY_HOST, RELAY_PORT

PROBE_TIMEOUT_SECONDS = 8.0
SEND_TIMEOUT_SECONDS = 15.0

_RECIPIENT_RE = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,253}\.[A-Za-z]{2,63}$")


@dataclass(frozen=True)
class ProbeResult:
    """How far a handshake with the mail server got. `stage` is where it stopped:
    resolve, connect, tls, starttls or auth; `ok` only after all of them."""

    ok: bool
    stage: str
    reason: str = ""
    login: str = "not tested"  # "ok" | "not tested" | "not needed"

    def as_json(self) -> dict[str, object]:
        return {"ok": self.ok, "stage": self.stage, "reason": self.reason, "login": self.login}


@dataclass(frozen=True)
class SendResult:
    ok: bool
    reason: str = ""

    def as_json(self) -> dict[str, object]:
        return {"ok": self.ok, "reason": self.reason}


def _short(exc: BaseException) -> str:
    text = " ".join(str(exc).split()) or type(exc).__name__
    return text[:200]


def probe_upstream(
    host: str,
    port: int,
    security: str,
    user: str,
    password: str,
    timeout: float = PROBE_TIMEOUT_SECONDS,
) -> ProbeResult:
    """Connect to the mail server the way the relay does. No mail is sent. A login is
    only attempted over an encrypted connection."""
    smtp: smtplib.SMTP | None = None
    stage = "connect"
    try:
        context = ssl.create_default_context()
        if security == "tls":
            stage = "tls"
            smtp = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
        else:
            smtp = smtplib.SMTP(host, port, timeout=timeout)
        stage = "connect"
        smtp.ehlo()
        if security == "starttls":
            stage = "starttls"
            smtp.starttls(context=context)
            smtp.ehlo()
        login = "not needed"
        if user and security != "none":
            stage = "auth"
            smtp.login(user, password)
            login = "ok"
        return ProbeResult(True, "ok", login=login)
    except socket.gaierror as exc:
        return ProbeResult(False, "resolve", f"cannot resolve {host}: {_short(exc)}")
    except ssl.SSLError as exc:
        return ProbeResult(False, stage, f"TLS handshake failed: {_short(exc)}")
    except smtplib.SMTPNotSupportedError as exc:
        return ProbeResult(False, stage, f"server does not offer it: {_short(exc)}")
    except smtplib.SMTPAuthenticationError as exc:
        return ProbeResult(False, "auth", f"login refused: {_short(exc)}")
    except (smtplib.SMTPException, OSError) as exc:
        return ProbeResult(False, stage, _short(exc))
    finally:
        if smtp is not None:
            with contextlib.suppress(OSError):
                smtp.close()


def validate_recipient(value: str) -> str:
    """The address of a test mail. A plain `name@domain.tld`: no display name, no list."""
    address = value.strip()
    if len(address) > 254 or not _RECIPIENT_RE.match(address):
        raise ValueError(f"'{value}' is not a valid address (expected name@example.org).")
    return address


def build_test_message(*, to: str, from_address: str, from_name: str) -> EmailMessage:
    message = EmailMessage()
    message["From"] = formataddr((from_name, from_address))
    message["To"] = to
    message["Subject"] = "papAIa test mail"
    message["Date"] = formatdate(localtime=False)
    message["Message-ID"] = make_msgid(domain=from_address.rsplit("@", 1)[1])
    message.set_content(
        "This is a test mail from the papAIa manager.\n\n"
        "If you can read it, the mail relay accepted it and the mail server behind it took it.\n"
    )
    return message


def send_test_message(
    *,
    to: str,
    from_address: str,
    from_name: str,
    host: str = RELAY_HOST,
    port: int = RELAY_PORT,
    timeout: float = SEND_TIMEOUT_SECONDS,
) -> SendResult:
    """Hand the test mail to the relay. `ok` means the relay accepted it, not that it arrived."""
    message = build_test_message(to=to, from_address=from_address, from_name=from_name)
    return send_message(message, host=host, port=port, timeout=timeout)


def send_message(
    message: EmailMessage,
    *,
    host: str = RELAY_HOST,
    port: int = RELAY_PORT,
    timeout: float = SEND_TIMEOUT_SECONDS,
) -> SendResult:
    """Hand one message to the relay. `ok` means the relay accepted it, not that it arrived.

    The one place that speaks to the relay: the test mail and the notifications both end here,
    so a refusal reads the same wherever it is shown."""
    try:
        with smtplib.SMTP(host, port, timeout=timeout) as smtp:
            smtp.ehlo()
            smtp.send_message(message)
    except smtplib.SMTPRecipientsRefused as exc:
        code, text = next(iter(exc.recipients.values()), (0, b""))
        return SendResult(False, f"the relay refused the recipient: {code} {_text(text)}")
    except smtplib.SMTPSenderRefused as exc:
        return SendResult(
            False, f"the relay refused the sender address: {exc.smtp_code} {_text(exc.smtp_error)}"
        )
    except smtplib.SMTPResponseException as exc:
        return SendResult(False, f"the relay answered {exc.smtp_code} {_text(exc.smtp_error)}")
    except socket.gaierror:
        return SendResult(False, f"cannot resolve {host}: the relay is not on this network")
    except (smtplib.SMTPException, OSError) as exc:
        return SendResult(False, _short(exc))
    return SendResult(True)


def _text(value: bytes | str) -> str:
    text = value.decode("utf-8", "replace") if isinstance(value, bytes) else value
    return " ".join(text.split())[:200]


async def probe(host: str, port: int, security: str, user: str, password: str) -> ProbeResult:
    return await asyncio.to_thread(probe_upstream, host, port, security, user, password)


async def send(*, to: str, from_address: str, from_name: str) -> SendResult:
    return await asyncio.to_thread(
        send_test_message, to=to, from_address=from_address, from_name=from_name
    )


async def send_async(message: EmailMessage) -> SendResult:
    return await asyncio.to_thread(send_message, message)
