"""The job that stores the mail server's settings and applies them to the relay.

It goes through the job queue, so it is single-flight with every other operation on the
stack, streams a log and leaves an audit entry. What it does depends on the relay:

* **The relay is on:** the core's `setup` stores the values, then `start --profiles=smtp`
  recreates the relay (a changed value or a raised password revision changes its Compose
  config) and, as the last step of `start`, applies the mail server to the Keycloak realm.
* **The relay is off:** `setup` only stores the values (the core keeps them whether the relay
  is on or off). Nothing starts and the realm is not touched; the relay is switched on in
  Settings > Modules (`app.core.config.service`), which also switches it off.

Only the options of the mail relay reach `setup` (`app.core.ctl.SmtpSetup`). A refusal
by the core is the job's failure with the core's own sentence, and nothing was written.
The password travels in the environment of the child and is scrubbed from every line
that reaches the job log.
"""

from __future__ import annotations

import re
from typing import Any

from app.core.audit import write_audit_entry
from app.core.ctl import SmtpSetup, run_core_verb, run_smtp_setup
from app.core.jobs import JobCallback, JobContext
from app.core.mail.settings import PROFILE, MailInput, MailSettings
from app.i18n import gettext as _

APPLY_ACTION = "mail-apply"
TARGET = "smtp-relay"

_PROFILES_FLAG = f"--profiles={PROFILE}"


# What `start` prints when its last step, the sync that puts the relay's mail server into
# Keycloak's realm, did not happen. `start` still exits 0 then (it only warns), so the lines
# are the only way to know. A core that is not using the bundled Keycloak skips on purpose:
# the realm is the operator's own there, which is not a failure.
_REALM_NOT_UPDATED = re.compile(r"Keycloak role sync (?:skipped|failed)")
_REALM_NOT_OURS = "AUTH_PROVIDER is not internal_keycloak"


class SetupRefusedError(Exception):
    """The core's `setup` did not store the values. The message is its own sentence."""


class RealmNotUpdatedError(Exception):
    """The relay runs, but Keycloak was not given its mail server."""


def setup_spec(data: MailInput, *, relay_on: bool = True) -> SmtpSetup:
    """The `setup` options for a validated request: every value named, so the result does
    not depend on what was sticky. A relay that is off stays off: only the values are stored."""
    return SmtpSetup(
        enable=True if relay_on else None,
        host=data.host,
        port=data.port,
        security=data.security,
        user=data.user or None,
        no_auth=not data.user,
        sender=data.from_address,
        sender_name=data.from_name,
        password=data.password,
    )


def refusal(code: int, out: str, err: str) -> str:
    """The sentence to show when `setup` exited with `code`: its `ERROR:` line, else the
    first line it wrote, else the status."""
    for text in (err, out):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        errors = [line for line in lines if line.startswith("ERROR")]
        if errors:
            return errors[0].removeprefix("ERROR:").strip()[:300]
    for text in (err, out):
        for line in text.splitlines():
            if line.strip():
                return line.strip()[:300]
    return f"setup exited with status {code}"


def scrub(line: str, secret: str | None) -> str:
    """`line` with the secret hidden."""
    return line.replace(secret, "***") if secret else line


async def _setup(ctx: JobContext, spec: SmtpSetup, *, workspace_dir: str, config_dir: str) -> None:
    ctx.log("[ctl] papaia-ctl setup (mail relay options only)")
    code, out, err = await run_smtp_setup(spec, workspace_dir=workspace_dir, config_dir=config_dir)
    for line in (*out.splitlines(), *err.splitlines()):
        if line.strip():
            ctx.log(scrub(line.rstrip(), spec.password))
    if code != 0:
        raise SetupRefusedError(scrub(refusal(code, out, err), spec.password))


def realm_problem(line: str) -> str | None:
    """The sentence of a `start` output line that says the realm was not updated, else None."""
    if not _REALM_NOT_UPDATED.search(line) or _REALM_NOT_OURS in line:
        return None
    # Only the sync's own sentence: `warn` adds a bracketed tag and a hint that is no use here.
    text = line.strip().removeprefix("[!]").strip()
    return text.split(". Once Keycloak is healthy", 1)[0][:300]


async def run_verb(
    ctx: JobContext, verb: str, *flags: str, workspace_dir: str, config_dir: str
) -> str | None:
    """Run a verb and stream its output into the log. Returns the first line that says the
    realm was not updated, for a verb that ends in the sync."""
    ctx.log(f"[ctl] papaia-ctl {verb} {' '.join(flags)}".rstrip())
    gen = await run_core_verb(
        verb=verb, workspace_dir=workspace_dir, config_dir=config_dir, extra_flags=list(flags)
    )
    problem: str | None = None
    async for line in gen:
        ctx.log(line)
        problem = problem or realm_problem(line)
    return problem


def apply_callback(
    *,
    data: MailInput,
    current: MailSettings,
    user: str,
    workspace_dir: str,
    config_dir: str,
) -> JobCallback:
    """The job that stores the settings and, when the relay is on, applies them to it."""
    spec = setup_spec(data, relay_on=current.profile_on)
    action = "mail.update" if current.profile_on else "mail.settings"
    params: dict[str, Any] = {
        "host": data.host,
        "port": data.port,
        "security": data.security,
        "login": data.login,
        "from_address": data.from_address,
        # Not "password": the audit log masks a key that sounds like a credential, and
        # this one is only ever the word set, kept, removed or none.
        "login_change": data.password_action(current),
    }

    async def _callback(ctx: JobContext) -> None:
        await _setup(ctx, spec, workspace_dir=workspace_dir, config_dir=config_dir)
        if not current.profile_on:
            ctx.log("[info] the relay is off; the settings apply when it is switched on")
            write_audit_entry(
                config_dir,
                user=user,
                action=action,
                target=TARGET,
                params=params,
                job_id=ctx.job.id,
            )
            ctx.log("[info] done")
            return
        problem = await run_verb(
            ctx, "start", _PROFILES_FLAG, workspace_dir=workspace_dir, config_dir=config_dir
        )
        if problem is not None:
            # Stored and started, but not what the operator asked for: Users cannot mail
            # links until the realm has the mail server. Recorded as what it is, and the job
            # fails so the page does not say "applied".
            write_audit_entry(
                config_dir,
                user=user,
                action=action,
                target=TARGET,
                params={**params, "realm": "not updated"},
                job_id=ctx.job.id,
                result="partial",
            )
            raise RealmNotUpdatedError(
                _(
                    "The relay is running, but Keycloak was not given its mail server "
                    "(%(problem)s). Apply again once that is fixed."
                ) % {"problem": problem}
            )
        write_audit_entry(
            config_dir,
            user=user,
            action=action,
            target=TARGET,
            params=params,
            job_id=ctx.job.id,
        )
        ctx.log("[info] done")

    return _callback
