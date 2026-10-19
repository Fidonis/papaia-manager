"""The job that stores the mail server's settings and applies them to the relay.

`setup` and `papaia-ctl` are replaced by recorders, so what is checked is the order of the steps,
what is passed to each, what a refusal looks like, and that the password reaches nothing but the
environment of `setup`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any

import pytest

from app.core.audit import audit_path
from app.core.ctl import CtlError, SmtpSetup
from app.core.jobs import Job, JobContext
from app.core.mail import service as mail_service
from app.core.mail.service import (
    RealmNotUpdatedError,
    SetupRefusedError,
    realm_problem,
    refusal,
    setup_spec,
)
from app.core.mail.settings import MailInput, MailSettings

_SECRET = "s3cret-pass-word-value"


class _Run:
    """The two ways out of this module, recorded in order."""

    def __init__(self) -> None:
        self.steps: list[tuple[str, Any]] = []
        self.setup_result: tuple[int, str, str] = (0, "Setup complete.", "")
        self.setup_stdout_leak = ""
        self.verb_error: CtlError | None = None
        self.verb_lines: dict[str, list[str]] = {}

    async def setup(self, spec: SmtpSetup, **kwargs: Any) -> tuple[int, str, str]:
        self.steps.append(("setup", spec))
        code, out, err = self.setup_result
        return code, out + self.setup_stdout_leak, err

    async def verb(self, **kwargs: Any) -> AsyncGenerator[str, None]:
        self.steps.append((kwargs["verb"], list(kwargs["extra_flags"])))

        async def lines() -> AsyncGenerator[str, None]:
            yield f"running {kwargs['verb']}"
            for line in self.verb_lines.get(kwargs["verb"], []):
                yield line
            if self.verb_error is not None:
                raise self.verb_error

        return lines()


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> _Run:
    recorder = _Run()
    monkeypatch.setattr(mail_service, "run_smtp_setup", recorder.setup)
    monkeypatch.setattr(mail_service, "run_core_verb", recorder.verb)
    return recorder


def _context(tmp_path: Path) -> JobContext:
    log = tmp_path / "job.log"
    job = Job(id="job-1", action="mail-apply", target="smtp-relay", user="admin", created_at=None)  # type: ignore[arg-type]
    return JobContext(job=job, log_path=log)


def _log(ctx: JobContext) -> str:
    return ctx.log_path.read_text(encoding="utf-8") if ctx.log_path.exists() else ""


def _audit(tmp_path: Path) -> list[dict[str, Any]]:
    path = audit_path(str(tmp_path))
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _data(**changes: Any) -> MailInput:
    fields: dict[str, Any] = {
        "host": "smtp.example.org",
        "port": 587,
        "security": "starttls",
        "user": "mailer",
        "from_address": "noreply@example.org",
        "from_name": "Our Mail",
        "password": _SECRET,
    }
    fields.update(changes)
    return MailInput(**fields)


def _current(**changes: Any) -> MailSettings:
    fields: dict[str, Any] = {"profile_on": True, "has_password": False}
    fields.update(changes)
    return MailSettings(**fields)


def _apply(
    tmp_path: Path, data: MailInput | None = None, current: MailSettings | None = None
) -> Any:
    return mail_service.apply_callback(
        data=data or _data(),
        current=current or _current(),
        user="admin",
        workspace_dir="/w",
        config_dir=str(tmp_path),
    )


# -- the request becomes setup's options ---------------------------------------------


def test_every_value_is_named_so_the_result_does_not_depend_on_what_was_sticky() -> None:
    spec = setup_spec(_data())
    assert spec == SmtpSetup(
        enable=True,
        host="smtp.example.org",
        port=587,
        security="starttls",
        user="mailer",
        no_auth=False,
        sender="noreply@example.org",
        sender_name="Our Mail",
        password=_SECRET,
    )


def test_a_server_without_a_login_is_asked_for_by_name() -> None:
    spec = setup_spec(_data(user="", security="none", port=25, password=None))
    assert spec.no_auth and spec.user is None and spec.password is None


# -- what a refusal looks like -------------------------------------------------------


def test_the_cores_error_line_is_the_sentence() -> None:
    err = "warning: x\nERROR: --smtp-host is required: no mail server is configured yet.\n"
    assert refusal(3, "", err) == "--smtp-host is required: no mail server is configured yet."


def test_a_traceback_without_an_error_line_is_the_first_line_it_wrote() -> None:
    assert refusal(1, "", "Traceback (most recent call last):\n  File x\nOSError\n").startswith(
        "Traceback"
    )


def test_nothing_at_all_is_the_status() -> None:
    assert refusal(7, "", "") == "setup exited with status 7"


# -- applying ------------------------------------------------------------------------


async def test_applying_stores_the_values_then_starts_the_relays_profile_and_audits(
    tmp_path: Path, run: _Run
) -> None:
    ctx = _context(tmp_path)
    await _apply(tmp_path)(ctx)

    assert [name for name, _ in run.steps] == ["setup", "start"]
    assert run.steps[1][1] == ["--profiles=smtp"]
    (entry,) = _audit(tmp_path)
    assert entry["action"] == "mail.update"
    assert entry["target"] == "smtp-relay"
    assert entry["job_id"] == "job-1"
    assert entry["params"] == {
        "host": "smtp.example.org",
        "port": 587,
        "security": "starttls",
        "login": True,
        "from_address": "noreply@example.org",
        "login_change": "set",
    }


async def test_settings_saved_with_the_relay_off_are_stored_and_nothing_starts(
    tmp_path: Path, run: _Run
) -> None:
    ctx = _context(tmp_path)
    await _apply(tmp_path, current=_current(profile_on=False))(ctx)

    # Only `setup` runs, and without the switch: the profile stays as it is. No start, and no
    # realm sync, because the relay is not there to be started.
    assert run.steps == [("setup", setup_spec(_data(), relay_on=False))]
    assert run.steps[0][1].enable is None
    (entry,) = _audit(tmp_path)
    assert (entry["action"], entry["result"]) == ("mail.settings", "ok")
    assert "switched on" in _log(ctx)


async def test_a_refusal_with_the_relay_off_stores_nothing_either(
    tmp_path: Path, run: _Run
) -> None:
    run.setup_result = (3, "", "ERROR: --smtp-host is required: no mail server is configured yet.")
    with pytest.raises(SetupRefusedError, match="--smtp-host is required"):
        await _apply(tmp_path, current=_current(profile_on=False))(_context(tmp_path))
    assert _audit(tmp_path) == []


async def test_the_password_stays_out_of_the_log_with_the_relay_off_too(
    tmp_path: Path, run: _Run
) -> None:
    run.setup_stdout_leak = f"\nmail server password: {_SECRET}\n"
    ctx = _context(tmp_path)
    await _apply(tmp_path, current=_current(profile_on=False))(ctx)
    assert _SECRET not in _log(ctx)


def test_the_relay_is_switched_in_the_modules_tab_so_this_module_has_no_switch_off() -> None:
    assert not hasattr(mail_service, "disable_callback")
    assert not hasattr(mail_service, "DISABLE_ACTION")


async def test_a_change_to_a_relay_that_is_on_is_audited_as_an_update(
    tmp_path: Path, run: _Run
) -> None:
    await _apply(tmp_path, _data(password=None), _current(profile_on=True, has_password=True))(
        _context(tmp_path)
    )
    (entry,) = _audit(tmp_path)
    assert (entry["action"], entry["params"]["login_change"]) == ("mail.update", "kept")


async def test_the_audit_entry_has_no_key_the_viewer_would_mask(tmp_path: Path, run: _Run) -> None:
    # A key that sounds like a credential is shown as *** by the audit viewer, which would
    # hide the one word that says what happened to the password.
    from app.core.audit import redact_params  # noqa: PLC0415

    await _apply(tmp_path)(_context(tmp_path))
    (entry,) = _audit(tmp_path)
    assert redact_params(entry["params"]) == entry["params"]


async def test_a_refusal_by_the_core_fails_the_job_with_its_sentence_and_starts_nothing(
    tmp_path: Path, run: _Run
) -> None:
    run.setup_result = (
        3,
        "",
        "ERROR: 'smtp.example.org:587' is not a valid mail server host name.",
    )
    ctx = _context(tmp_path)
    with pytest.raises(SetupRefusedError, match="not a valid mail server host name"):
        await _apply(tmp_path)(ctx)
    assert [name for name, _ in run.steps] == ["setup"]
    assert _audit(tmp_path) == []


async def test_a_start_that_fails_after_a_successful_setup_fails_the_job_without_an_audit_entry(
    tmp_path: Path, run: _Run
) -> None:
    run.verb_error = CtlError("papaia-ctl exited with code 1", exit_code=1)
    with pytest.raises(CtlError):
        await _apply(tmp_path)(_context(tmp_path))
    assert [name for name, _ in run.steps] == ["setup", "start"]
    assert _audit(tmp_path) == []


async def test_the_password_is_in_no_line_of_the_job_log(tmp_path: Path, run: _Run) -> None:
    # Whatever setup prints, the password does not reach the log, even if it were echoed.
    run.setup_stdout_leak = f"\nmail server password: {_SECRET}\n"
    ctx = _context(tmp_path)
    await _apply(tmp_path)(ctx)
    log = _log(ctx)
    assert _SECRET not in log
    assert "***" in log
    assert "[ctl] papaia-ctl setup" in log


async def test_the_password_is_not_in_the_refusal_either(tmp_path: Path, run: _Run) -> None:
    run.setup_result = (3, "", f"ERROR: login failed for {_SECRET}")
    with pytest.raises(SetupRefusedError) as raised:
        await _apply(tmp_path)(_context(tmp_path))
    assert _SECRET not in str(raised.value)


# -- the realm step ------------------------------------------------------------------

_SKIPPED = (
    "Keycloak role sync skipped: Keycloak at https://localhost:8110 did not become ready "
    "within 90s: <urlopen error [Errno 111] Connection refused>"
)
_FAILED_HINT = (
    "[!] Keycloak role sync failed. Once Keycloak is healthy, run "
    "'papaia-ctl keycloak-role-sync' to retry."
)


def test_the_sentence_of_a_skipped_sync_is_taken_from_the_line() -> None:
    assert realm_problem(_SKIPPED) == _SKIPPED


def test_the_hint_the_warning_adds_is_left_out() -> None:
    assert realm_problem(_FAILED_HINT) == "Keycloak role sync failed"


@pytest.mark.parametrize(
    "line",
    [
        "Keycloak role sync complete: 0 role(s) created",
        "  mail server set: smtp-relay",
        "Waiting for Keycloak to become ready...",
        "Keycloak role sync skipped: AUTH_PROVIDER is not internal_keycloak.",
        "",
    ],
)
def test_a_line_that_is_not_a_failed_sync_is_not_one(line: str) -> None:
    assert realm_problem(line) is None


async def test_a_sync_that_was_skipped_fails_the_job_although_start_exited_cleanly(
    tmp_path: Path, run: _Run
) -> None:
    run.verb_lines["start"] = [_SKIPPED, _FAILED_HINT, "[ok] start complete."]
    with pytest.raises(RealmNotUpdatedError) as raised:
        await _apply(tmp_path)(_context(tmp_path))
    message = str(raised.value)
    assert "relay is running" in message
    assert "Keycloak was not given its mail server" in message
    assert "localhost:8110" in message
    assert "Apply again" in message
    assert [name for name, _ in run.steps] == ["setup", "start"]


async def test_a_sync_that_was_skipped_is_recorded_as_a_partial_change(
    tmp_path: Path, run: _Run
) -> None:
    run.verb_lines["start"] = [_SKIPPED]
    with pytest.raises(RealmNotUpdatedError):
        await _apply(tmp_path)(_context(tmp_path))
    (entry,) = _audit(tmp_path)
    assert (entry["action"], entry["result"]) == ("mail.update", "partial")
    assert entry["params"]["realm"] == "not updated"
    assert entry["params"]["host"] == "smtp.example.org"


async def test_a_deployment_that_is_not_using_the_bundled_keycloak_is_not_a_failure(
    tmp_path: Path, run: _Run
) -> None:
    # The realm is the operator's own there; skipping the sync is the right thing.
    run.verb_lines["start"] = [
        "Keycloak role sync skipped: AUTH_PROVIDER is not internal_keycloak."
    ]
    await _apply(tmp_path)(_context(tmp_path))
    (entry,) = _audit(tmp_path)
    assert entry["result"] == "ok"
