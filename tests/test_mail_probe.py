"""The two SMTP conversations the Mail page starts: the mail server, and the relay.

The handshake with the mail server is driven through a scripted stand-in for `smtplib`, because
STARTTLS needs a certificate nobody wants in a test. The message to the relay goes to a real
(tiny) SMTP server on the loopback interface, so what leaves the manager is what is checked.
"""

from __future__ import annotations

import smtplib
import socket
import socketserver
import ssl
import threading
from collections.abc import Iterator
from email import message_from_string
from typing import Any

import pytest

from app.core.mail import probe as mail_probe
from app.core.mail.probe import (
    ProbeResult,
    build_test_message,
    probe_upstream,
    send_test_message,
    validate_recipient,
)

# -- the mail server -----------------------------------------------------------------


class _Script:
    """What the stand-in server does, and what it was asked."""

    def __init__(self, **faults: Any) -> None:
        self.faults = faults
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.closed = False


def _install(monkeypatch: pytest.MonkeyPatch, script: _Script) -> None:
    class _Fake:
        def __init__(self, host: str, port: int, **kwargs: Any) -> None:
            script.calls.append((type(self).__name__, (host, port)))
            if "connect" in script.faults:
                raise script.faults["connect"]

        def ehlo(self) -> None:
            script.calls.append(("ehlo", ()))

        def starttls(self, **kwargs: Any) -> None:
            script.calls.append(("starttls", ()))
            if "starttls" in script.faults:
                raise script.faults["starttls"]

        def login(self, user: str, password: str) -> None:
            script.calls.append(("login", (user, password)))
            if "login" in script.faults:
                raise script.faults["login"]

        def close(self) -> None:
            script.closed = True

    class _FakeSSL(_Fake):
        pass

    monkeypatch.setattr(mail_probe.smtplib, "SMTP", _Fake)
    monkeypatch.setattr(mail_probe.smtplib, "SMTP_SSL", _FakeSSL)


def test_a_server_that_needs_no_login_is_reached_and_none_is_attempted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _Script()
    _install(monkeypatch, script)
    result = probe_upstream("relay.lan", 25, "none", "", "")
    assert result == ProbeResult(True, "ok", login="not needed")
    assert [name for name, _ in script.calls] == ["_Fake", "ehlo"]
    assert script.closed


def test_starttls_upgrades_the_connection_before_the_login(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _Script()
    _install(monkeypatch, script)
    result = probe_upstream("smtp.example.org", 587, "starttls", "mailer", "pw")
    assert result.ok and result.login == "ok"
    assert [name for name, _ in script.calls] == ["_Fake", "ehlo", "starttls", "ehlo", "login"]
    assert script.calls[-1] == ("login", ("mailer", "pw"))


def test_implicit_tls_starts_encrypted_and_does_not_issue_starttls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    script = _Script()
    _install(monkeypatch, script)
    result = probe_upstream("smtp.example.org", 465, "tls", "mailer", "pw")
    assert result.ok
    assert script.calls[0][0] == "_FakeSSL"
    assert "starttls" not in [name for name, _ in script.calls]


def test_a_login_is_never_attempted_without_encryption(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _Script()
    _install(monkeypatch, script)
    result = probe_upstream("relay.lan", 25, "none", "mailer", "pw")
    assert result.login == "not needed"
    assert "login" not in [name for name, _ in script.calls]


def test_a_refused_login_says_so_and_stops_at_the_login(monkeypatch: pytest.MonkeyPatch) -> None:
    script = _Script(login=smtplib.SMTPAuthenticationError(535, b"5.7.8 credentials rejected"))
    _install(monkeypatch, script)
    result = probe_upstream("smtp.example.org", 587, "starttls", "mailer", "hunter2")
    assert (result.ok, result.stage) == (False, "auth")
    assert "login refused" in result.reason and "credentials rejected" in result.reason
    assert "hunter2" not in result.reason
    assert script.closed


def test_a_name_that_does_not_resolve_stops_at_resolve(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _Script(connect=socket.gaierror(-2, "Name or service not known")))
    result = probe_upstream("nope.invalid", 587, "starttls", "", "")
    assert (result.ok, result.stage) == (False, "resolve")
    assert "nope.invalid" in result.reason


def test_a_port_nobody_listens_on_stops_at_connect(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(monkeypatch, _Script(connect=ConnectionRefusedError(111, "Connection refused")))
    result = probe_upstream("smtp.example.org", 2525, "starttls", "", "")
    assert (result.ok, result.stage) == (False, "connect")
    assert "refused" in result.reason.lower()


def test_a_certificate_that_does_not_verify_stops_at_starttls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install(monkeypatch, _Script(starttls=ssl.SSLError("certificate verify failed")))
    result = probe_upstream("smtp.example.org", 587, "starttls", "mailer", "pw")
    assert (result.ok, result.stage) == (False, "starttls")
    assert "TLS handshake failed" in result.reason


def test_a_server_without_starttls_is_reported_as_such(monkeypatch: pytest.MonkeyPatch) -> None:
    _install(
        monkeypatch,
        _Script(starttls=smtplib.SMTPNotSupportedError("STARTTLS extension not supported")),
    )
    result = probe_upstream("smtp.example.org", 587, "starttls", "", "")
    assert not result.ok
    assert "does not offer it" in result.reason


# -- the relay -----------------------------------------------------------------------


class _Relay(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self, refuse_recipient: bool = False, refuse_sender: bool = False) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.refuse_recipient = refuse_recipient
        self.refuse_sender = refuse_sender
        self.messages: list[str] = []
        self.envelope: list[str] = []


class _Handler(socketserver.StreamRequestHandler):
    server: _Relay

    def _say(self, text: str) -> None:
        self.wfile.write(text.encode() + b"\r\n")

    def handle(self) -> None:
        self._say("220 relay.test ESMTP")
        body: list[str] | None = None
        for raw in self.rfile:
            line = raw.decode().rstrip("\r\n")
            if body is not None:
                if line == ".":
                    self.server.messages.append("\r\n".join(body))
                    body = None
                    self._say("250 2.0.0 queued")
                else:
                    body.append(line[1:] if line.startswith("..") else line)
                continue
            upper = line.upper()
            if upper.startswith(("EHLO", "HELO")):
                self._say("250-relay.test")
                self._say("250 8BITMIME")
            elif upper.startswith("MAIL FROM"):
                self.server.envelope.append(line)
                self._say(
                    "553 5.7.1 sender domain not allowed" if self.server.refuse_sender else "250 ok"
                )
            elif upper.startswith("RCPT TO"):
                self.server.envelope.append(line)
                if self.server.refuse_recipient:
                    self._say("550 5.1.2 recipient domain does not resolve")
                else:
                    self._say("250 ok")
            elif upper == "DATA":
                self._say("354 go ahead")
                body = []
            elif upper == "QUIT":
                self._say("221 bye")
                return
            else:
                self._say("250 ok")


@pytest.fixture
def relay() -> Iterator[_Relay]:
    server = _Relay()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server
    server.shutdown()
    server.server_close()


def _send(server: _Relay, **overrides: Any) -> mail_probe.SendResult:
    host, port = server.server_address[0], server.server_address[1]
    arguments: dict[str, Any] = {
        "to": "someone@example.org",
        "from_address": "noreply@example.org",
        "from_name": "Our Mail",
        "host": str(host),
        "port": int(port),
        "timeout": 5.0,
    }
    arguments.update(overrides)
    return send_test_message(**arguments)


def test_the_relay_gets_a_complete_message_from_the_configured_sender(relay: _Relay) -> None:
    result = _send(relay)
    assert result.ok and result.reason == ""
    (raw,) = relay.messages
    message = message_from_string(raw.replace("\r\n", "\n"))
    assert message["Subject"] == "papAIa test mail"
    assert message["To"] == "someone@example.org"
    assert "noreply@example.org" in message["From"] and "Our Mail" in message["From"]
    assert message["Message-ID"].endswith("@example.org>")
    assert message["Date"]
    assert any("<noreply@example.org>" in line for line in relay.envelope)


def test_a_recipient_the_relay_refuses_is_reported_in_the_relays_words() -> None:
    server = _Relay(refuse_recipient=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = _send(server)
    finally:
        server.shutdown()
        server.server_close()
    assert not result.ok
    assert "refused the recipient" in result.reason
    assert "550" in result.reason and "does not resolve" in result.reason


def test_a_sender_the_relay_refuses_is_reported_as_the_sender() -> None:
    server = _Relay(refuse_sender=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        result = _send(server)
    finally:
        server.shutdown()
        server.server_close()
    assert not result.ok
    assert "sender" in result.reason and "553" in result.reason


def test_a_relay_nobody_listens_on_is_a_reason_not_an_exception() -> None:
    free = socket.socket()
    free.bind(("127.0.0.1", 0))
    port = free.getsockname()[1]
    free.close()
    result = send_test_message(
        to="someone@example.org",
        from_address="noreply@example.org",
        from_name="x",
        host="127.0.0.1",
        port=port,
        timeout=2.0,
    )
    assert not result.ok and result.reason


def test_a_relay_that_is_not_on_this_network_says_so() -> None:
    result = send_test_message(
        to="someone@example.org",
        from_address="noreply@example.org",
        from_name="x",
        host="relay.invalid",
        port=587,
        timeout=2.0,
    )
    assert not result.ok
    assert "cannot resolve relay.invalid" in result.reason


# -- the address ---------------------------------------------------------------------


@pytest.mark.parametrize("address", ["a@example.org", "first.last+tag@mail.example.co.uk"])
def test_a_plain_address_is_accepted(address: str) -> None:
    assert validate_recipient(f" {address} ") == address


@pytest.mark.parametrize(
    "address",
    [
        "",
        "no-at-sign",
        "Name <a@example.org>",
        "a@example.org, b@example.org",
        "a@localhost",
        "a@example.org\nBcc: x@example.org",
        "a" * 250 + "@example.org",
    ],
)
def test_a_name_a_list_or_a_header_in_the_address_is_refused(address: str) -> None:
    with pytest.raises(ValueError, match="not a valid address"):
        validate_recipient(address)


def test_a_header_cannot_be_smuggled_through_the_sender_name() -> None:
    with pytest.raises(ValueError):  # noqa: PT011
        build_test_message(
            to="a@example.org",
            from_address="noreply@example.org",
            from_name="x\nBcc: y@example.org",
        )


# -- any message ---------------------------------------------------------------------


def test_any_message_can_be_handed_to_the_relay(relay: _Relay) -> None:
    message = build_test_message(
        to="someone@example.org", from_address="noreply@example.org", from_name="Our Mail"
    )
    message.replace_header("Subject", "[papAIa] Job failed: backup")
    host, port = relay.server_address[0], relay.server_address[1]
    result = mail_probe.send_message(message, host=str(host), port=int(port), timeout=5.0)
    assert result.ok
    (raw,) = relay.messages
    received = message_from_string(raw.replace("\r\n", "\n"))
    assert received["Subject"] == "[papAIa] Job failed: backup"


def test_the_test_mail_and_a_notification_are_refused_in_the_same_words() -> None:
    server = _Relay(refuse_recipient=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        message = build_test_message(
            to="someone@example.org", from_address="noreply@example.org", from_name="x"
        )
        host, port = server.server_address[0], server.server_address[1]
        generic = mail_probe.send_message(message, host=str(host), port=int(port), timeout=5.0)
        test_mail = _send(server)
    finally:
        server.shutdown()
        server.server_close()
    assert generic == test_mail and not generic.ok
