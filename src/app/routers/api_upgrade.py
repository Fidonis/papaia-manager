"""REST API — moving the core to a newer release.

One operation, split across two halves that cost and risk entirely different
things:

* **check** resolves a target release, gates the active add-ons against it and
  lists the pending migrations. It changes nothing, and the answer is what the
  operator decides on. See `app.core.upgrade`.
* **upgrade** runs `papaia-ctl upgrade` in a detached container. It cannot be a
  queued job: the command runs `stop --clean-up --addons` between its phases,
  which removes the container serving this request. See `app.core.runner`.

The upgrade is mutually exclusive with everything else the manager can start. A
job would be killed halfway through with its own papaia-ctl half-finished; a
restore would replace the config directory the migrations are rewriting; a stack
runner would fight it over the same Compose project. All of them are refused
here, and the reverse guards live in `api_maintenance` and `api_stack`.

Nothing here decides *whether* an upgrade is safe -- that is `papaia-ctl`'s
judgement, reached by the same commands this module calls to preview it. What it
does decide is that the operator saw that judgement first: an upgrade is refused
unless a check for the same version has run in this process.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.auth.csrf import verify_csrf
from app.auth.deps import AdminUser
from app.auth.oidc import OIDCClaims
from app.config import Settings, get_settings
from app.core import backups, runner, upgrade
from app.core.audit import write_audit_entry
from app.core.jobs import JobQueue

router = APIRouter(prefix="/api/v1/upgrade")


class CheckBody(BaseModel):
    # None means "the newest release", which is what papaia-ctl resolves without
    # --version. A named version is only accepted if it exists as a tag, and the
    # core is what decides that.
    version: str | None = None


class UpgradeBody(BaseModel):
    version: str
    # Degrades an add-on incompatibility to a warning. Offered only where the
    # gate actually failed on one, and refused otherwise -- see `_check_force`.
    force: bool = False
    # Skips the pre-upgrade restore point. Opt-in per request and never a stored
    # preference: it removes the only thing the failure path can point at.
    no_backup: bool = False


def _queue() -> JobQueue | None:
    from app.main import _job_queue  # noqa: PLC0415

    return _job_queue


def _user_id(user: OIDCClaims) -> str:
    return user.preferred_username or user.sub


async def _find(kind: runner.RunnerKind) -> runner.RunnerStatus | None:
    """`find_runner`, with an unreachable Docker socket reported as a 503."""
    try:
        return await runner.find_runner(kind)
    except runner.RunnerError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


async def _require_idle() -> None:
    """Refuse the upgrade while anything else this manager can start is running.

    An upgrade runner that has *finished* counts too, and deliberately so: it
    holds the outcome of the last attempt, and starting the next one before
    anybody has read it would discard the only report of what happened. The
    stack page clears its finished runner automatically; this one does not.
    """
    existing = await _find(runner.UPGRADE_KIND)
    if existing is not None:
        detail = (
            f"an upgrade to {existing.target} is still running"
            if existing.is_running
            else (
                f"the result of the previous upgrade ({existing.target}) has not "
                "been acknowledged yet"
            )
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)

    for kind, label in ((runner.RESTORE_KIND, "restore"), (runner.STACK_KIND, "stack")):
        active = await _find(kind)
        if active is not None and active.is_running:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"a {label} of {active.target} is still running",
            )

    queue = _queue()
    active_job = queue.active_job() if queue is not None else None
    if active_job is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"a {active_job.action} job is running; it would be killed when the "
                "upgrade stops the stack"
            ),
        )


def _check_force(body: UpgradeBody, check: upgrade.UpgradeCheck) -> None:
    """Refuse `--force` where there is nothing for it to override.

    Same reasoning as `_check_clean_up` in api_stack: confirming an operation
    that did not happen is worse than refusing it. And where the gate failed on
    an ERROR rather than an incompatibility, `compat.gate` returns 2 whatever
    the flag says, so offering it there would promise something papaia-ctl
    cannot deliver.
    """
    if not body.force:
        return
    if check.gate.passed:
        raise HTTPException(
            status_code=400,
            detail="the add-on compatibility check passed; there is nothing for force to override",
        )
    if check.gate.has_error:
        raise HTTPException(
            status_code=400,
            detail=(
                "force cannot override a malformed add-on manifest -- papaia-ctl refuses "
                "an ERROR result regardless of the flag. Fix the add-on first."
            ),
        )


def _check_gate(body: UpgradeBody, check: upgrade.UpgradeCheck) -> None:
    if not check.gate.passed and not body.force:
        names = ", ".join(r.name for r in check.gate.blocking) or "an add-on"
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"the add-on compatibility check failed against {check.target} ({names}). "
                "Update the add-ons first, or re-run with force."
            ),
        )


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------


@router.get("/status")
async def upgrade_status(
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Everything about this installation that costs no network round trip.

    Rendered on page load, so it fails soft throughout: a workspace that is not
    a checkout, an unreadable deployment.yaml and an unreachable Docker socket
    are all states this reports rather than errors it raises.
    """
    version = upgrade.current_version(settings.papaia_config_dir, settings.papaia_workspace_dir)
    state = await upgrade.checkout_state(settings.papaia_workspace_dir)
    backup_dir = backups.resolve_backup_dir(settings.papaia_config_dir)
    return {
        "version": {
            "recorded": version.recorded,
            "checkout": version.checkout,
            "current": version.current,
            "mismatch": version.mismatch,
        },
        "checkout": {
            "is_git": state.is_git,
            "clean": state.clean,
            "dirty": state.dirty,
            "head": state.head,
            "tag": state.tag,
            "error": state.error,
            "upgradable": state.upgradable,
        },
        "backup": {
            "path": str(backup_dir) if backup_dir else None,
            "configured": backup_dir is not None,
            "reachable": backups.is_reachable(backup_dir),
        },
        "history": [_log_to_dict(entry) for entry in upgrade.read_upgrade_log(
            settings.papaia_config_dir
        )],
    }


@router.get("/check")
async def read_check(user: AdminUser) -> dict[str, Any]:
    """The last check's result, without running another one."""
    cached = upgrade.cached_check()
    if cached is None:
        return {"checked": False}
    return _check_to_dict(cached)


@router.post("/check")
async def run_check(
    body: CheckBody,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Fetch the release tags and evaluate a target.

    A POST despite only reading: it talks to the remote and materialises a git
    worktree of the target tag, and a GET that did either would be wrong on both
    counts.
    """
    verify_csrf(request)
    if body.version is not None and not upgrade.is_valid_target_version(body.version):
        raise HTTPException(
            status_code=400, detail=f"{body.version!r} is not a release version (X.Y.Z)"
        )
    try:
        result = await upgrade.run_check(
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
            version=body.version,
        )
    except upgrade.UpgradeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _check_to_dict(result)


@router.get("/runner")
async def upgrade_runner_status(user: AdminUser) -> dict[str, Any]:
    """State of the current or last upgrade runner.

    Reachable again as soon as the recreated manager container is up -- on a new
    image, and that is the point: the runner container is not part of the
    Compose project, so it survives the very restart that replaces this one.
    """
    try:
        active = await runner.find_runner(runner.UPGRADE_KIND)
    except runner.RunnerError as exc:
        return {"active": False, "runner": None, "error": str(exc)}
    log = (
        await runner.runner_log(active.name, tail=runner.UPGRADE_LOG_TAIL_LINES)
        if active is not None
        else ""
    )
    return runner.status_to_dict(active, log, target_key="target_version")


# ---------------------------------------------------------------------------
# The upgrade — detached runner
# ---------------------------------------------------------------------------


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def start_upgrade(
    body: UpgradeBody,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    verify_csrf(request)
    if not upgrade.is_valid_target_version(body.version):
        raise HTTPException(
            status_code=400, detail=f"{body.version!r} is not a release version (X.Y.Z)"
        )

    # The check is a precondition, not a convenience. It is where the add-on
    # gate ran and where the migration list came from, and both are what the
    # operator is confirming -- accepting a version nobody evaluated would make
    # the confirmation dialog a formality.
    check = upgrade.cached_check()
    if check is None or check.target != body.version:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"no completed check for {body.version}; run the update check again "
                "before starting the upgrade"
            ),
        )
    if check.up_to_date:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"already at {check.current} -- there is no newer release to move to",
        )

    # The checkout comes before the gate, in that order, because `cmd_upgrade`
    # answers in that order too -- and because the two refusals send an operator
    # to different places. A dirty tree has no override at all and is fixed in a
    # shell; the gate can be forced from here. Reporting the forceable one first
    # to somebody who also has to clean their checkout starts them on the half
    # that will not unblock them.
    state = await upgrade.checkout_state(settings.papaia_workspace_dir)
    if not state.is_git:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "the papaia workspace is not a git checkout, so it cannot be moved to a "
                "release tag"
            ),
        )
    if state.error:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=state.error)
    if not state.clean:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "the checkout has uncommitted changes to tracked files. Commit, stash or "
                "discard them on the host first -- the upgrade moves the checkout to a "
                "release tag and would otherwise take those edits with it."
            ),
        )

    _check_force(body, check)
    _check_gate(body, check)

    if not body.no_backup:
        backup_dir = backups.resolve_backup_dir(settings.papaia_config_dir)
        if backup_dir is None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    "PAPAIA_BACKUP_DIR is not set in the stack configuration, so no restore "
                    "point can be written. Set it on the host, or start the upgrade without "
                    "a restore point."
                ),
            )
        if not backups.is_reachable(backup_dir):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"the backup directory {backup_dir} is not reachable from the manager "
                    "container, so the restore point would fail after the stack is already "
                    "down"
                ),
            )

    await _require_idle()

    # Written before the runner starts. Unlike a restore's, this entry survives:
    # an upgrade re-renders the config directory but never replaces it.
    write_audit_entry(
        settings.papaia_config_dir,
        user=_user_id(user),
        action="upgrade",
        target=body.version,
        params={"from": check.current, "force": body.force, "no_backup": body.no_backup},
    )

    try:
        started = await runner.start_upgrade(
            target_version=body.version,
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
            force=body.force,
            no_backup=body.no_backup,
        )
    except runner.RunnerError as exc:
        # Includes the duplicate-name refusal, which is the real mutual
        # exclusion behind `_require_idle`'s check: two administrators clicking
        # at once produce one runner and one 409, not two upgrades.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    # The check described a deployment that is now moving. Whatever it said about
    # the gate and the migrations belongs to the version being installed, not to
    # the one that will be current afterwards.
    upgrade.reset_cache()
    return runner.status_to_dict(started, target_key="target_version")


@router.post("/runner/clear")
async def clear_upgrade_runner(request: Request, user: AdminUser) -> dict[str, str]:
    """Acknowledge a finished upgrade by removing its runner container.

    Safe in a way the restore equivalent is not: `$CONFIG_DIR/upgrade.log`
    records every attempt and survives the operation, so the outcome stays on the
    page after the container is gone.
    """
    verify_csrf(request)
    active = await _find(runner.UPGRADE_KIND)
    if active is None:
        return {"status": "nothing to clear"}
    try:
        await runner.clear_runner(active.name, runner.UPGRADE_KIND)
    except runner.RunnerError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return {"status": "cleared"}


# ---------------------------------------------------------------------------
# Serialisation
# ---------------------------------------------------------------------------


def _check_to_dict(check: upgrade.UpgradeCheck) -> dict[str, Any]:
    return {
        "checked": True,
        "checked_at": check.checked_at,
        "current": check.current,
        "target": check.target,
        "tag": check.tag,
        "status": check.status,
        "up_to_date": check.up_to_date,
        "available": check.available,
        "fetch_error": check.fetch_error,
        "migrations": [
            {"id": m.id, "version": m.version, "kind": m.kind} for m in check.migrations
        ],
        "gate": {
            "passed": check.gate.passed,
            "forceable": check.gate.forceable,
            "has_error": check.gate.has_error,
            "results": [
                {
                    "name": r.name,
                    "status": r.status,
                    "axis": r.axis,
                    "requirement": r.requirement,
                    "core_value": r.core_value,
                    "reason": r.reason,
                }
                for r in check.gate.results
            ],
        },
    }


def _log_to_dict(entry: upgrade.LogEntry) -> dict[str, Any]:
    return {
        "at": entry.at,
        "from_version": entry.from_version,
        "to_version": entry.to_version,
        "result": entry.result,
        "stage": entry.stage,
        "restore_point": entry.restore_point,
        "details": entry.details,
    }
