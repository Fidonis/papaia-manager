"""REST API — core stack lifecycle, scoped to Compose profiles.

Two execution models again, and the dividing line is whether the operation
removes the container serving the request:

* **group actions** name profiles explicitly, and never the manager's own. The
  containers being started or stopped are not this one, so these are ordinary
  queued jobs with streamed output, the same shape as every addon verb.
* **stack actions** cover every profile, manager included. They run in a
  detached container that outlives this process; see `app.core.runner`. The
  outcome is read back afterwards from `docker inspect` and `docker logs`,
  because there is no request left to return it to.

Which profile names are acceptable is not a matter of pattern-matching: the set
comes from `inventory.core_groups`, read out of the Compose fragments this
deployment actually ships and filtered to the profiles it has enabled. Naming
anything else is a 400, and so is naming `manager`.

In a distributed installation a group action names the node that owns each group. Without
it the core's `start` pushes to every node and brings up each link gateway first, so one
node that does not answer would stop an action that has nothing to do with it.

On a single host a group action goes through `papaia-docker-gate`, the manager's only way to
Docker: it stops the group's running containers and starts its stopped ones, one container at a
time, and the gate decides each call. What the gate does not allow is refused here before
anything is queued: `clean_up` removes containers, and a service without a container needs one
created; both are `papaia-ctl` on the host. A gate that does not answer is a 503 that says
running without it is not supported.

Add-ons are deliberately untouched by everything here. `papaia-ctl stop` would
take them along given `--addons`, and that flag is never passed -- an add-on is
started and stopped one at a time from `/api/v1/addons`.
"""
from __future__ import annotations

import asyncio
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.auth.csrf import verify_csrf
from app.auth.deps import AdminUser
from app.auth.oidc import OIDCClaims
from app.config import Settings, get_settings
from app.core import docker_gate, nodes, runner, services
from app.core.audit import write_audit_entry
from app.core.ctl import profiles_flag, run_core_verb
from app.core.inventory import active_profiles, core_groups
from app.core.jobs import JobContext, JobQueue
from app.i18n import gettext as _

router = APIRouter(prefix="/api/v1/stack")

# What a group action may be asked to do. `restart` is composed here rather than
# dispatched, because papaia-ctl has no restart verb.
GROUP_ACTIONS = frozenset({"start", "stop", "restart"})


class GroupBody(BaseModel):
    groups: list[str] = []
    # Turns `docker compose stop` into `docker compose down`: containers are
    # removed, volumes are kept. Opt-in per request rather than a preference,
    # because it changes what the services page reports afterwards -- a removed
    # container reads as "not deployed", not as "stopped".
    clean_up: bool = False


class StackBody(BaseModel):
    clean_up: bool = False


def _queue() -> JobQueue:
    from app.main import _job_queue  # noqa: PLC0415

    if _job_queue is None:
        raise HTTPException(status_code=503, detail=_("job queue not initialized"))
    return _job_queue


def _user_id(user: OIDCClaims) -> str:
    return user.preferred_username or user.sub


async def _known_groups(settings: Settings) -> dict[str, frozenset[str]]:
    """The deployment's service groups, read off the workspace in a thread.

    Blocking YAML parsing, so it goes to the executor the same way the services
    page loads its snapshot. Deliberately independent of Docker: a request has to
    be validated even when the socket is unreachable.
    """
    return await asyncio.get_running_loop().run_in_executor(
        None,
        core_groups,
        settings.papaia_workspace_dir,
        active_profiles(settings.papaia_config_dir),
    )


def _flag(names: list[str], allowed: dict[str, frozenset[str]]) -> str:
    """`--profiles=` for the request, or 400 with the reason it was refused."""
    try:
        return profiles_flag(names, allowed=allowed)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _runs(
    names: list[str], allowed: dict[str, frozenset[str]], settings: Settings
) -> list[list[str]]:
    """The flags of each `papaia-ctl` run a group action takes: one, except in a distributed
    installation whose selection spans several nodes."""
    config_dir = settings.papaia_config_dir
    if not nodes.is_distributed(config_dir):
        return [[_flag(names, allowed)]]
    snapshot = await asyncio.to_thread(
        services.load_snapshot, config_dir, settings.papaia_workspace_dir
    )
    return [
        [_flag(groups, allowed), *([f"--node={node}"] if node else [])]
        for node, groups in services.group_owners(snapshot, names).items()
    ]


def _check_clean_up(action: str, clean_up: bool) -> None:
    """Refuse `clean_up` where there is nothing for it to attach to.

    It maps onto `papaia-ctl stop --clean-up`, so it is meaningful for a stop and
    for the stop half of a restart -- which turns that restart into a full
    recreate. A start has no such flag, and silently ignoring the field there
    would confirm an operation that did not happen: the caller asked for the
    containers to be removed and would be told it worked.
    """
    if clean_up and action == "start":
        raise HTTPException(
            status_code=400,
            detail=_("clean_up needs something to stop, and start stops nothing"),
        )


def _refuse_while_locked(action: str, settings: Settings) -> None:
    """While nodes run different releases the core refuses `start`; `stop` stays possible."""
    if action == "stop":
        return
    reason = nodes.lock_reason(settings.papaia_config_dir, settings.papaia_workspace_dir)
    if reason:
        raise HTTPException(status_code=409, detail=reason)


# ---------------------------------------------------------------------------
# Service groups through papaia-docker-gate
# ---------------------------------------------------------------------------


class GroupTargets(BaseModel):
    """The containers of the selected groups, as the gate showed them when the action was
    asked for. The gate decides each call again when the job runs."""

    running: list[str] = []
    stopped: list[str] = []
    # Declared services with no container: only `papaia-ctl start` on the host creates one.
    missing: list[str] = []


async def _gate_targets(groups: list[str], settings: Settings) -> GroupTargets:
    """Read the stack through the gate and pick the containers of `groups`; 503 when the
    gate gives no reading."""
    services.invalidate_snapshot()
    snapshot = await asyncio.to_thread(
        services.load_snapshot, settings.papaia_config_dir, settings.papaia_workspace_dir
    )
    if snapshot.docker_problem:
        raise HTTPException(status_code=503, detail=snapshot.docker_problem)
    selected = set(groups)
    targets = GroupTargets()
    for module in snapshot.core:
        if not selected.intersection(module.profiles):
            continue
        for container in module.containers:
            if not container.name:
                targets.missing.append(container.service)
            elif container.state in ("running", "restarting"):
                targets.running.append(container.name)
            else:
                targets.stopped.append(container.name)
    return targets


async def _gate_group_run(ctx: JobContext, action: str, targets: GroupTargets) -> None:
    """Stop, start or restart the group's containers through the gate, in name order."""
    client = docker_gate.default_client()
    if action in ("stop", "restart"):
        for name in sorted(targets.running):
            ctx.log(f"[gate] stop {name}")
            changed = await asyncio.to_thread(client.stop_container, name)
            ctx.log(f"[gate] {name}: {'stopped' if changed else 'was not running'}")
    if action in ("start", "restart"):
        to_start = targets.stopped + (targets.running if action == "restart" else [])
        for name in sorted(to_start):
            ctx.log(f"[gate] start {name}")
            changed = await asyncio.to_thread(client.start_container, name)
            ctx.log(f"[gate] {name}: {'started' if changed else 'was running'}")
        if targets.missing:
            raise docker_gate.GateError(_(
                "no container for %(services)s, and papaia-docker-gate creates none: "
                "run `papaia-ctl start` on the host"
            ) % {"services": ", ".join(sorted(targets.missing))})


def _refuse_clean_up_through_gate(clean_up: bool) -> None:
    if clean_up:
        raise HTTPException(status_code=409, detail=_(
            "clean_up removes containers, which papaia-docker-gate does not allow: run "
            "`papaia-ctl stop --clean-up` on the host"
        ))


# ---------------------------------------------------------------------------
# Service groups -- queued jobs
# ---------------------------------------------------------------------------


@router.post("/groups/{action}", status_code=status.HTTP_202_ACCEPTED)
async def group_action(
    action: str,
    body: GroupBody,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    verify_csrf(request)
    if action not in GROUP_ACTIONS:
        raise HTTPException(
            status_code=404, detail=_("unknown action %(action)r") % {"action": action}
        )
    _check_clean_up(action, body.clean_up)
    _refuse_while_locked(action, settings)

    allowed = await _known_groups(settings)
    _flag(body.groups, allowed)
    queue = _queue()
    _username = _user_id(user)
    _clean = body.clean_up
    _target = ",".join(sorted(set(body.groups)))
    _workspace = settings.papaia_workspace_dir
    _config = settings.papaia_config_dir

    if not nodes.is_distributed(_config):
        _refuse_clean_up_through_gate(body.clean_up)
        targets = await _gate_targets(body.groups, settings)

        async def _gate_callback(ctx: JobContext) -> None:
            try:
                await _gate_group_run(ctx, action, targets)
            except Exception as exc:
                # Every outcome is one audit row, a failure of any kind included.
                ctx.log(f"[error] {exc}")
                write_audit_entry(
                    _config, user=_username, action=f"group-{action}", target=_target,
                    params={"clean_up": False}, job_id=ctx.job.id, result="failed",
                )
                raise
            finally:
                services.invalidate_snapshot()
            write_audit_entry(
                _config, user=_username, action=f"group-{action}", target=_target,
                params={"clean_up": False}, job_id=ctx.job.id,
            )
            ctx.log("[info] done")

        job = await queue.enqueue(
            action=f"group-{action}",
            target=_target,
            user=_username,
            params={"clean_up": False},
            callback=_gate_callback,
        )
        return {"job_id": job.id, "status": "queued"}

    runs = await _runs(body.groups, allowed, settings)

    async def _run(ctx: JobContext, verb: str, *extra: str) -> None:
        for selection in runs:
            ctx.log(f"[ctl] papaia-ctl {verb} {' '.join((*selection, *extra))}".rstrip())
            gen = await run_core_verb(
                verb=verb,
                workspace_dir=_workspace,
                config_dir=_config,
                extra_flags=[*selection, *extra],
            )
            async for line in gen:
                ctx.log(line)

    async def _callback(ctx: JobContext) -> None:
        if action in ("stop", "restart"):
            await _run(ctx, "stop", *(["--clean-up"] if _clean else []))
        if action in ("start", "restart"):
            await _run(ctx, "start")
        write_audit_entry(
            _config,
            user=_username,
            action=f"group-{action}",
            target=_target,
            params={"clean_up": _clean},
            job_id=ctx.job.id,
        )
        ctx.log("[info] done")

    job = await queue.enqueue(
        action=f"group-{action}",
        target=_target,
        user=_username,
        params={"clean_up": _clean},
        callback=_callback,
    )
    return {"job_id": job.id, "status": "queued"}


@router.get("/groups")
async def list_groups(
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """The service groups this deployment can be asked to act on.

    Read from the Compose fragments, not from the snapshot, so it answers the
    same way whether or not Docker is reachable.
    """
    groups = await _known_groups(settings)
    return {
        "groups": [
            {"name": name, "modules": sorted(modules)}
            for name, modules in sorted(groups.items())
        ]
    }


# ---------------------------------------------------------------------------
# Whole stack -- detached runner
# ---------------------------------------------------------------------------


@router.post("/{action}", status_code=status.HTTP_202_ACCEPTED)
async def stack_action(
    action: str,
    body: StackBody,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    verify_csrf(request)
    if action not in runner.STACK_ACTIONS:
        raise HTTPException(status_code=404, detail=_(
            "unknown action %(action)r"
        ) % {"action": action})
    _check_clean_up(action, body.clean_up)
    _refuse_while_locked(action, settings)

    # A job in flight would be killed together with the manager and left stuck in
    # `running` forever, with its own papaia-ctl half-finished. A second runner
    # would fight the first over the same Compose project. Both are refused
    # before anything is started.
    if _job_running():
        raise HTTPException(
            status_code=409, detail=_("a job is running; wait for it to finish")
        )
    # An upgrade already owns the whole stack: it takes it down between its two
    # phases and brings it back on the target release. A stack action started
    # against that would race the recreate, and neither side would win cleanly.
    upgrading = await _find(runner.UPGRADE_KIND)
    if upgrading is not None and upgrading.is_running:
        raise HTTPException(
            status_code=409, detail=_(
                "an upgrade to %(target)s is still running"
            ) % {"target": upgrading.target}
        )
    existing = await _find(runner.STACK_KIND)
    if existing is not None:
        if existing.is_running:
            raise HTTPException(
                status_code=409, detail=_(
                    "a stack %(target)s is still running"
                ) % {"target": existing.target}
            )
        # A finished runner holds the name the next one needs. Clearing it here
        # rather than making the operator do it keeps a one-click action one
        # click, and its log has already been shown by then.
        await runner.clear_runner(existing.name, runner.STACK_KIND)

    try:
        started = await runner.start_stack_action(
            action=action,
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
            clean_up=body.clean_up,
        )
    except runner.RunnerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    write_audit_entry(
        settings.papaia_config_dir,
        user=_user_id(user),
        action=f"stack-{action}",
        target="core",
        params={"clean_up": body.clean_up},
        result="started",
    )
    return runner.status_to_dict(started, target_key="action")


@router.get("/runner")
async def stack_runner_status(user: AdminUser) -> dict[str, Any]:
    active = await _find(runner.STACK_KIND)
    log = await runner.runner_log(active.name) if active is not None else ""
    return runner.status_to_dict(active, log, target_key="action")


@router.post("/runner/clear")
async def clear_stack_runner(
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    verify_csrf(request)
    active = await _find(runner.STACK_KIND)
    if active is None:
        return {"status": "nothing to clear"}
    try:
        await runner.clear_runner(active.name, runner.STACK_KIND)
    except runner.RunnerError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    write_audit_entry(
        settings.papaia_config_dir,
        user=_user_id(user),
        action="stack-runner-clear",
        target=active.target,
    )
    return {"status": "cleared"}


async def _find(kind: runner.RunnerKind) -> runner.RunnerStatus | None:
    """`find_runner`, with an unreachable Docker socket reported as a 503.

    The status endpoint is polled, so an exception here would surface as a
    stack trace on a page that is otherwise fine.
    """
    try:
        return await runner.find_runner(kind)
    except runner.RunnerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _job_running() -> bool:
    from app.main import _job_queue  # noqa: PLC0415

    if _job_queue is None:
        return False
    return _job_queue.active_job() is not None
