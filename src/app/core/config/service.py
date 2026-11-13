"""The jobs behind the Modules and Overlay tabs.

Every change is a queued job, so it is single-flight with everything else the manager does to
the stack, streams a log and leaves an audit entry.

* **Module on:** the core's `setup` stores the choice, then `start --profiles=<profile>`
  brings the module up. The RAG system also recreates LibreChat (its configuration lists the
  RAG system's MCP server and is read once, at start); the mail relay ends with the sync that
  puts its mail server into Keycloak's realm.
* **Module off:** the module is stopped and its containers removed while its profile is still
  active (with `--volumes` when its data is to go), then `setup` takes the profile out, then
  the core deletes what was asked for (`module-purge`): nothing, the data, or the data and
  the settings. The RAG system recreates LibreChat again; the mail relay's mail server is
  taken out of the realm.
* **Overlay apply, discard and roll back** share one sequence: check that the file is still
  the one the page showed, copy the old bytes into the history, write (or remove) the file,
  then recreate the containers that read the rendered target (`stop --clean-up` and `start`
  of its profile: render replaces the target file, and a running container keeps the old one).
  If the stop or the start fails, the previous file is put back and the group is started again.
  If the group starts but does not become healthy, the file stays and the job says so: the
  page then offers the roll back, because the previous version may be no better.
* **Nodes that only run hooks.** In a distributed installation a `start` also visits the
  nodes of the proxy and of Keycloak (`nodes.profile_runs`). A failure there does not undo
  the change, which is in place on the module's own node: the job goes on, and ends with
  the sentence that names the node and the `start` that catches it up.

Only the options of one optional module reach `setup` (`app.core.ctl.ModuleSetup`). Overlay
content is never written to the job log or the audit log.
"""

from __future__ import annotations

import asyncio
from typing import Any

from app.core import nodes, services
from app.core.audit import write_audit_entry
from app.core.config import history, modules, overlay
from app.core.ctl import (
    CtlError,
    ModuleSetup,
    SmtpSetup,
    run_core_verb,
    run_module_purge,
    run_module_setup,
    run_smtp_setup,
)
from app.core.inventory import active_profiles
from app.core.jobs import JobCallback, JobContext
from app.core.mail import service as mail_service
from app.core.mail.settings import MailInput, MailSettings
from app.i18n import gettext as _

# The profile of Keycloak, whose realm holds the mail server the relay provides.
_KEYCLOAK_PROFILE = "keycloak"

ENABLE_ACTION = "config-module-enable"
DISABLE_ACTION = "config-module-disable"
APPLY_ACTION = "config-overlay-apply"
DISCARD_ACTION = "config-overlay-discard"
ROLLBACK_ACTION = "config-overlay-rollback"

CONFIG_ACTIONS = (ENABLE_ACTION, DISABLE_ACTION, APPLY_ACTION, DISCARD_ACTION, ROLLBACK_ACTION)

# How long a recreated group gets to report healthy, and how often it is looked at.
HEALTH_TIMEOUT_SECONDS = 90.0
HEALTH_INTERVAL_SECONDS = 3.0

_GOOD = {services.ServiceHealth.HEALTHY, services.ServiceHealth.COMPLETED}


class SetupRefusedError(Exception):
    """The core's `setup` did not store the choice. The message is its own sentence."""


class ConflictError(Exception):
    """The overlay file changed after the page loaded it. Nothing was changed."""


class StepFailedError(Exception):
    """A papaia-ctl step exited non-zero. The message is the last thing it complained about."""


class RolledBackError(Exception):
    """The new overlay broke the start, and the previous one was put back."""


class NotHealthyError(Exception):
    """The change was made and the containers were recreated, but they are not all healthy."""


class PurgeFailedError(Exception):
    """The module is off, but the core could not delete what was asked for."""


class HookNodesFailedError(Exception):
    """The change is in place, but a node that only runs hooks for it did not take part."""


# The core's refusals are whole sentences (the RAG one names the add-on and the commands that
# remove it); the job strip shows them as they are.
SENTENCE_LIMIT = 700


def refusal(code: int, out: str, err: str) -> str:
    """The sentence to show when `setup` exited with `code`: its `ERROR:` line, else the
    first line it wrote, else the status."""
    for text in (err, out):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        errors = [line for line in lines if line.startswith("ERROR")]
        if errors:
            return errors[0].removeprefix("ERROR:").strip()[:SENTENCE_LIMIT]
    for text in (err, out):
        for line in text.splitlines():
            if line.strip():
                return line.strip()[:SENTENCE_LIMIT]
    return f"setup exited with status {code}"


def last_problem(lines: list[str]) -> str:
    """The line to blame for a failed step: the last `ERROR` line, else the last line."""
    cleaned = [ln.strip() for ln in lines if ln.strip()]
    errors = [ln for ln in cleaned if "ERROR" in ln]
    pick = errors[-1] if errors else (cleaned[-1] if cleaned else "the step failed")
    return pick.removeprefix("ERROR:").strip()[:SENTENCE_LIMIT]


async def _setup(
    ctx: JobContext, spec: ModuleSetup | SmtpSetup, *, workspace_dir: str, config_dir: str
) -> None:
    secret: str | None = None
    if isinstance(spec, SmtpSetup):
        secret = spec.password
        ctx.log("[ctl] papaia-ctl setup (options of mail only)")
        code, out, err = await run_smtp_setup(
            spec, workspace_dir=workspace_dir, config_dir=config_dir
        )
    else:
        ctx.log(f"[ctl] papaia-ctl setup (options of {spec.module} only)")
        code, out, err = await run_module_setup(
            spec, workspace_dir=workspace_dir, config_dir=config_dir
        )
    for line in (*out.splitlines(), *err.splitlines()):
        if line.strip():
            ctx.log(mail_service.scrub(line.rstrip(), secret))
    if code != 0:
        raise SetupRefusedError(mail_service.scrub(refusal(code, out, err), secret))


async def _verb(
    ctx: JobContext,
    verb: str,
    *flags: str,
    workspace_dir: str,
    config_dir: str,
    skipped: list[str] | None = None,
) -> None:
    """Run a verb and stream its output into the log. A failure becomes StepFailedError.

    In a distributed installation a profile's `start` and `stop` run once per node they
    concern (`nodes.profile_runs`), and the first that fails ends the step. With `skipped`,
    a failure on a node that only runs hooks (every run after the first) does not: the log
    says so, the command that catches the node up goes into `skipped`, and the next node
    follows."""
    for index, run in enumerate(
        nodes.profile_runs(verb, flags, config_dir=config_dir, workspace_dir=workspace_dir)
    ):
        command = f"papaia-ctl {verb} {' '.join(run)}".rstrip()
        ctx.log(f"[ctl] {command}")
        lines: list[str] = []
        try:
            gen = await run_core_verb(
                verb=verb,
                workspace_dir=workspace_dir,
                config_dir=config_dir,
                extra_flags=list(run),
            )
            async for line in gen:
                ctx.log(line)
                lines.append(line)
        except CtlError as exc:
            problem = last_problem(lines) or str(exc)
            if skipped is not None and index > 0:
                ctx.log(f"[warn] {command} failed ({problem}); the change stays")
                skipped.append(command)
                continue
            raise StepFailedError(_(
                "papaia-ctl %(verb)s: %(value)s"
            ) % {"verb": verb, "value": problem}) from exc


def _hook_nodes_failed(skipped: list[str]) -> HookNodesFailedError:
    """The error that ends a job whose change is in place but missed a node's hooks."""
    return HookNodesFailedError(
        _(
            "The change is in place, but a node that only runs its hooks did not take part. "
            "Once it answers, run: %(value)s"
        ) % {"value": "; ".join(skipped)}
    )


async def wait_healthy(
    ctx: JobContext,
    profile: str,
    *,
    config_dir: str,
    workspace_dir: str,
    limit: float | None = None,
    interval: float | None = None,
) -> str | None:
    """None once the profile's containers are healthy, else the sentence that says what isn't.

    An unreadable Docker (no groups reported at all) is not a verdict: the check is skipped
    and the log says so. With the native LocalAI runtime the `localai` group is the model
    download job alone: no container is expected for the process, and the process's own state
    is not waited for (`services.build_groups`), since nothing here starts it."""
    deadline = asyncio.get_running_loop().time() + (
        HEALTH_TIMEOUT_SECONDS if limit is None else limit
    )
    pause = HEALTH_INTERVAL_SECONDS if interval is None else interval
    while True:
        services.invalidate_snapshot()
        snapshot = await asyncio.get_running_loop().run_in_executor(
            None, services.load_snapshot, config_dir, workspace_dir
        )
        if not snapshot.groups:
            ctx.log("[info] health not checked: Docker did not report any containers")
            return None
        group = next((g for g in snapshot.groups if g.name == profile), None)
        if group is not None and group.health in _GOOD:
            ctx.log(f"[info] {profile}: healthy")
            return None
        state = group.health.value if group is not None else "not reported"
        if asyncio.get_running_loop().time() >= deadline:
            return f"The {profile} containers are {state}."
        ctx.log(f"[info] {profile}: {state}, waiting")
        await asyncio.sleep(pause)


# ---------------------------------------------------------------------------
# Modules
# ---------------------------------------------------------------------------


def _setup_spec(
    module: str,
    *,
    enable: bool,
    variant: str | None = None,
    public_url: str | None = None,
    mail: MailInput | None = None,
) -> ModuleSetup | SmtpSetup:
    """The `setup` options that switch `module`. The mail relay's come with its own spec: it is
    switched on with the settings the core stored, or with the mail server the request carried
    (every value named, as the Mail page does)."""
    if module == "mail":
        return mail_service.setup_spec(mail) if mail is not None else SmtpSetup(enable=enable)
    return ModuleSetup(module=module, enable=enable, variant=variant, public_url=public_url)


async def _recreate(
    ctx: JobContext,
    profiles: tuple[str, ...],
    *,
    workspace_dir: str,
    config_dir: str,
    skipped: list[str],
) -> None:
    """Recreate the containers of `profiles`: render replaces the files they read, and a
    running container keeps the old one (see the overlay jobs)."""
    for profile in profiles:
        flag = f"--profiles={profile}"
        await _verb(
            ctx, "stop", "--clean-up", flag, workspace_dir=workspace_dir, config_dir=config_dir
        )
        await _verb(
            ctx,
            "start",
            flag,
            workspace_dir=workspace_dir,
            config_dir=config_dir,
            skipped=skipped,
        )


async def _start(
    ctx: JobContext,
    module: str,
    flag: str,
    *,
    workspace_dir: str,
    config_dir: str,
    skipped: list[str],
) -> str | None:
    """`start` of the module's profile. For the mail relay, the sentence that says Keycloak
    was not given its mail server (the last step of `start` only warns), else None. The
    relay's run on Keycloak's node is the point of its start, so a failure there still ends
    the step."""
    if module != "mail":
        await _verb(
            ctx,
            "start",
            flag,
            workspace_dir=workspace_dir,
            config_dir=config_dir,
            skipped=skipped,
        )
        return None
    return await mail_service.run_verb(
        ctx, "start", flag, workspace_dir=workspace_dir, config_dir=config_dir
    )


def enable_callback(
    *,
    module: str,
    variant: str | None,
    user: str,
    workspace_dir: str,
    config_dir: str,
    public_url: str | None = None,
    mail: MailInput | None = None,
    current_mail: MailSettings | None = None,
) -> JobCallback:
    """The job that switches a module on.

    `public_url` and `mail` are the answers to what `setup` asks for the module (its public
    URL, the mail server); they are only given when the settings were missing."""
    definition = modules.SWITCHABLE[module]
    spec = _setup_spec(
        module, enable=True, variant=variant, public_url=public_url, mail=mail
    )
    flag = f"--profiles={definition.profile}"
    params: dict[str, Any] = {"module": module, "variant": variant}
    if public_url is not None:
        params["public_url"] = public_url
    if mail is not None:
        params.update(
            {
                "host": mail.host,
                "port": mail.port,
                "security": mail.security,
                "login": mail.login,
                "from_address": mail.from_address,
                # Not "password": the audit log masks a key that sounds like a credential.
                "login_change": mail.password_action(current_mail or MailSettings(False)),
            }
        )

    async def _callback(ctx: JobContext) -> None:
        skipped: list[str] = []
        await _setup(ctx, spec, workspace_dir=workspace_dir, config_dir=config_dir)
        try:
            realm = await _start(
                ctx,
                module,
                flag,
                workspace_dir=workspace_dir,
                config_dir=config_dir,
                skipped=skipped,
            )
            await _recreate(
                ctx,
                definition.recreate,
                workspace_dir=workspace_dir,
                config_dir=config_dir,
                skipped=skipped,
            )
        except StepFailedError as exc:
            write_audit_entry(
                config_dir,
                user=user,
                action="config.module.enable",
                target=f"module:{module}",
                params=params,
                job_id=ctx.job.id,
                result="partial",
            )
            raise StepFailedError(
                _(
                    "%(exc)s %(title)s is switched on in the configuration but did not start. "
                    "Fix the cause and start it from the Services page, or switch it off here."
                ) % {"exc": exc, "title": definition.title}
            ) from exc
        problem = await wait_healthy(
            ctx, definition.profile, config_dir=config_dir, workspace_dir=workspace_dir
        )
        write_audit_entry(
            config_dir,
            user=user,
            action="config.module.enable",
            target=f"module:{module}",
            params=params,
            job_id=ctx.job.id,
            result="partial" if problem or realm or skipped else "ok",
        )
        if realm:
            raise mail_service.RealmNotUpdatedError(
                _(
                    "The relay is running, but Keycloak was not given its mail server "
                    "(%(realm)s). Switch it off and on again once that is fixed."
                ) % {"realm": realm}
            )
        if problem:
            raise NotHealthyError(
                _(
                    "%(title)s is switched on, but %(value)s%(value2)s Check the service log, or "
                    "switch it off again."
                ) % {"title": definition.title, "value": problem[0].lower(), "value2": problem[1:]}
            )
        if skipped:
            raise _hook_nodes_failed(skipped)
        ctx.log("[info] done")

    return _callback


def disable_callback(
    *, module: str, scope: str, user: str, workspace_dir: str, config_dir: str
) -> JobCallback:
    """The job that switches a module off: containers removed, then what `scope` says goes.

    `keep` leaves the data and the settings, `data` deletes the module's volumes, `all` the
    volumes and the settings too."""
    definition = modules.SWITCHABLE[module]
    flag = f"--profiles={definition.profile}"
    delete_data = scope in (modules.SCOPE_DATA, modules.SCOPE_ALL)
    delete_config = scope == modules.SCOPE_ALL
    params: dict[str, Any] = {"module": module, "scope": scope}

    async def _callback(ctx: JobContext) -> None:
        skipped: list[str] = []
        # Stopped while the profile is still active: once `setup` has removed it, Compose no
        # longer knows what to stop. `--volumes` takes the anonymous volumes of the containers
        # with them, which only the `down` that removes the containers can do.
        if definition.profile in modules.active(config_dir):
            flags = ["--clean-up", *(["--volumes"] if delete_data else []), flag]
            await _verb(ctx, "stop", *flags, workspace_dir=workspace_dir, config_dir=config_dir)
        await _setup(
            ctx,
            _setup_spec(module, enable=False),
            workspace_dir=workspace_dir,
            config_dir=config_dir,
        )
        if module == "mail":
            await _sync_realm(ctx, workspace_dir=workspace_dir, config_dir=config_dir)
        await _recreate(
            ctx,
            definition.recreate,
            workspace_dir=workspace_dir,
            config_dir=config_dir,
            skipped=skipped,
        )
        if delete_data:
            await _purge(
                ctx,
                module,
                data=True,
                config=delete_config,
                user=user,
                params=params,
                workspace_dir=workspace_dir,
                config_dir=config_dir,
            )
        write_audit_entry(
            config_dir,
            user=user,
            action="config.module.disable",
            target=f"module:{module}",
            params=params,
            job_id=ctx.job.id,
            result="partial" if skipped else "ok",
        )
        if skipped:
            raise _hook_nodes_failed(skipped)
        ctx.log("[info] done")

    return _callback


async def _sync_realm(ctx: JobContext, *, workspace_dir: str, config_dir: str) -> None:
    """Bring the realm's mail server in line with the relay that was just switched off.

    In a distributed installation `keycloak-role-sync` runs on Keycloak's node with the bundle
    that node already has, which still names the relay, so the realm would keep it. A `start`
    of Keycloak's profile on its node hands the node its new bundle first and runs the sync
    after it; Keycloak itself keeps running. The start only warns when the sync fails, so its
    output is read for that, and the job fails rather than report the relay as gone."""
    node = nodes.profile_node(config_dir, _KEYCLOAK_PROFILE)
    if not node or _KEYCLOAK_PROFILE not in active_profiles(config_dir):
        await _verb(
            ctx, "keycloak-role-sync", workspace_dir=workspace_dir, config_dir=config_dir
        )
        return
    try:
        problem = await mail_service.run_verb(
            ctx,
            "start",
            f"--profiles={_KEYCLOAK_PROFILE}",
            f"--node={node}",
            workspace_dir=workspace_dir,
            config_dir=config_dir,
        )
    except CtlError as exc:
        raise StepFailedError(_(
            "papaia-ctl %(verb)s: %(value)s"
        ) % {"verb": "start", "value": exc}) from exc
    if problem is not None:
        raise StepFailedError(_(
            "The relay is off, but the realm still names it: %(value)s"
        ) % {"value": problem})


async def _purge(
    ctx: JobContext,
    module: str,
    *,
    data: bool,
    config: bool,
    user: str,
    params: dict[str, Any],
    workspace_dir: str,
    config_dir: str,
) -> None:
    """Delete what the operator asked for, now that the module is off. A failure leaves the
    module off with whatever was not deleted still there, and says so."""
    what = "data and settings" if config else "data"
    ctx.log(f"[ctl] module-purge {module}: deleting its {what}")
    code, out, err = await run_module_purge(
        module, data=data, config=config, workspace_dir=workspace_dir, config_dir=config_dir
    )
    for line in (*out.splitlines(), *err.splitlines()):
        if line.strip():
            ctx.log(line.rstrip())
    if code != 0:
        write_audit_entry(
            config_dir,
            user=user,
            action="config.module.disable",
            target=f"module:{module}",
            params=params,
            job_id=ctx.job.id,
            result="partial",
        )
        raise PurgeFailedError(
            _(
                "%(title)s is off, but its %(what)s could not be deleted: %(value)s"
            ) % {
                "title": modules.SWITCHABLE[module].title,
                "what": what,
                "value": refusal(code, out, err),
            }
        )


# ---------------------------------------------------------------------------
# Overlay
# ---------------------------------------------------------------------------

_EVENT_ACTION = {
    "apply": "config.overlay.apply",
    "discard": "config.overlay.discard",
    "rollback": "config.overlay.rollback",
}


def overlay_callback(
    *,
    key: str,
    content: bytes | None,
    expected_revision: str,
    event: str,
    user: str,
    workspace_dir: str,
    config_dir: str,
    version: str | None = None,
) -> JobCallback:
    """The job that replaces (`content`) or removes (`None`) an overlay file and recreates the
    containers that read it. `event` is `apply`, `discard` or `rollback`; `version` is the id a
    roll back restores, for the audit entry."""
    target = overlay.target(key)
    flag = f"--profiles={target.profile}"
    action = _EVENT_ACTION[event]
    subject = f"overlay:{key}"

    async def _callback(ctx: JobContext) -> None:
        current = overlay.read(config_dir, key)
        if current.revision != expected_revision:
            raise ConflictError(
                _("The overlay file changed after the page loaded it. Nothing was changed.")
            )
        saved: str | None = None
        if current.raw is not None:
            saved = history.save(config_dir, key, current.raw, event)
            ctx.log(f"[backup] previous version saved as {saved}")
        else:
            ctx.log("[backup] there was no overlay file; nothing to save")

        if content is None:
            written_ok = overlay.delete(config_dir, key, expected_revision=current.revision)
            new_revision = ""
        else:
            written = overlay.write(config_dir, key, content, expected_revision=current.revision)
            written_ok = written is not None
            new_revision = written.revision if written is not None else ""
        if not written_ok:
            raise ConflictError(_(
                "The overlay file changed while the job ran. Nothing was changed."
            ))
        ctx.log("[info] overlay file removed" if content is None else "[info] overlay file written")

        params: dict[str, Any] = {
            "bytes": 0 if content is None else len(content),
            "revision_before": current.revision[:7],
            "revision_after": new_revision[:7],
            "version_saved": saved,
        }
        if version is not None:
            params["version_restored"] = version

        if target.profile not in modules.active(config_dir):
            ctx.log(
                f"[info] {target.label} is not switched on; "
                "the file applies the next time it starts"
            )
            params["restarted"] = False
            write_audit_entry(
                config_dir,
                user=user,
                action=action,
                target=subject,
                params=params,
                job_id=ctx.job.id,
            )
            return

        params["restarted"] = True
        skipped: list[str] = []
        try:
            await _verb(
                ctx, "stop", "--clean-up", flag, workspace_dir=workspace_dir, config_dir=config_dir
            )
            await _verb(
                ctx,
                "start",
                flag,
                workspace_dir=workspace_dir,
                config_dir=config_dir,
                skipped=skipped,
            )
        except StepFailedError as exc:
            await _restore(
                ctx,
                key=key,
                previous=current.raw,
                written_revision=new_revision,
                flag=flag,
                workspace_dir=workspace_dir,
                config_dir=config_dir,
            )
            write_audit_entry(
                config_dir,
                user=user,
                action="config.overlay.rollback.auto",
                target=subject,
                params={**params, "failed_event": event},
                job_id=ctx.job.id,
                result="error",
            )
            raise RolledBackError(
                _(
                    "%(label)s did not start with the new overlay, so the previous version was "
                    "put back. (%(exc)s)"
                ) % {"label": target.label, "exc": exc}
            ) from exc

        problem = await wait_healthy(
            ctx, target.profile, config_dir=config_dir, workspace_dir=workspace_dir
        )
        write_audit_entry(
            config_dir,
            user=user,
            action=action,
            target=subject,
            params=params,
            job_id=ctx.job.id,
            result="partial" if problem or skipped else "ok",
        )
        if problem:
            raise NotHealthyError(
                _(
                    "The overlay is in place, but %(value)s%(value2)s The previous version is in "
                    "Versions if you want to go back."
                ) % {"value": problem[0].lower(), "value2": problem[1:]}
            )
        if skipped:
            raise _hook_nodes_failed(skipped)
        ctx.log("[info] done")

    return _callback


async def _restore(
    ctx: JobContext,
    *,
    key: str,
    previous: bytes | None,
    written_revision: str,
    flag: str,
    workspace_dir: str,
    config_dir: str,
) -> None:
    """Put the previous file back and start the group again, as far as that works.

    Never raises: it runs on the way out of a failure, and the failure is what gets reported.
    A file edited on the host since the job wrote it is left alone."""
    ctx.log("[rollback] putting the previous overlay back")
    try:
        if previous is None:
            restored = overlay.delete(config_dir, key, expected_revision=written_revision)
        else:
            restored = (
                overlay.write(config_dir, key, previous, expected_revision=written_revision)
                is not None
            )
    except overlay.OverlayError as exc:
        ctx.log(f"[rollback] the previous file could not be restored: {exc}")
        return
    if not restored:
        ctx.log("[rollback] the file changed meanwhile; left as it is")
        return
    try:
        await _verb(ctx, "start", flag, workspace_dir=workspace_dir, config_dir=config_dir)
        ctx.log("[rollback] the group was started with the previous overlay")
    except StepFailedError as exc:
        ctx.log(f"[rollback] the group did not start again either: {exc}")
