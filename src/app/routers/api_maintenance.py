"""REST API — stack maintenance: backup and restore.

Two very different execution models sit behind these routes, and the split is
deliberate rather than incidental:

* **backup** runs hot. papaia-ctl pauses only the containers using the volume it
  is archiving, and the manager mounts no named volume, so it is never paused
  itself. That makes a backup an ordinary queued job with streamed output, the
  same shape as every addon verb.
* **restore** tears the core stack down, manager container included. It runs in a
  detached container that outlives this process; see app.core.runner.

Both are mutually exclusive: a backup taken while a restore is unpacking archives
would capture a half-restored stack, and a restore started while a job runs would
pull the config directory out from under it.
"""
from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.auth.csrf import verify_csrf
from app.auth.deps import AdminUser
from app.auth.oidc import OIDCClaims
from app.config import Settings, get_settings
from app.core import backups, ctl, inventory, restore_scope, runner
from app.core.audit import write_audit_entry
from app.core.ctl import run_core_verb
from app.core.jobs import JobContext, JobQueue

router = APIRouter(prefix="/api/v1/maintenance")


class BackupBody(BaseModel):
    # None means "keep every restore point" -- papaia-ctl only prunes when
    # --retention-period-days is passed, so absence and 0 are different requests
    # and 0 (delete everything older than today) must stay expressible.
    retention_days: int | None = Field(default=None, ge=0)


class RestoreBody(BaseModel):
    restore_point: str
    # Maps to papaia-ctl's --restart-clean: named volumes are deleted before the
    # archives are unpacked. Anything not in the restore point loses its data, so
    # it is opt-in on every single request rather than a stored preference.
    restart_clean: bool = False


class ScopedRestoreBody(BaseModel):
    """A restore of part of a snapshot.

    Deliberately a separate model from RestoreBody rather than an `only` field
    on it. `restart_clean` and a selection are mutually exclusive -- clearing
    volumes the selection will not repopulate is data loss -- and a single body
    carrying two mutually exclusive fields is the shape that eventually ships a
    request with both set.
    """

    restore_point: str
    only: list[str] = Field(min_length=1, max_length=ctl.MAX_SELECTORS)


class DeleteRestorePointsBody(BaseModel):
    """One or more restore points to delete.

    A list rather than a path parameter so a multi-select in the UI is one
    request and one `papaia-ctl backup-delete` call -- which is one atomic
    rewrite of `backup.yaml` rather than N racing ones. The single-row action
    sends a one-element list. Capped at the same ceiling as a scoped selection.
    """

    restore_points: list[str] = Field(min_length=1, max_length=ctl.MAX_SELECTORS)


def _queue() -> JobQueue:
    from app.main import _job_queue  # noqa: PLC0415

    if _job_queue is None:
        raise HTTPException(status_code=503, detail="job queue not initialized")
    return _job_queue


def _user_id(user: OIDCClaims) -> str:
    return user.preferred_username or user.sub


def _backup_dir(settings: Settings) -> Path:
    """Resolved backup directory, or 409 when it is unusable.

    A 409 rather than a 500: nothing is broken in the manager, the deployment
    simply has no backup location it can reach, and the message says which of the
    two cases it is.
    """
    backup_dir = backups.resolve_backup_dir(settings.papaia_config_dir)
    if backup_dir is None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "PAPAIA_BACKUP_DIR is not set in the stack configuration. "
                "Run 'papaia-ctl setup --backup-dir=PATH' on the host."
            ),
        )
    if not backups.is_reachable(backup_dir):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"The backup directory {backup_dir} is not reachable from the manager "
                "container. It must be mounted at its host path."
            ),
        )
    return backup_dir


async def _require_no_runner() -> None:
    """Reject the request if a restore or upgrade runner is active.

    Both replace what a backup would archive and what a scoped restore would
    unpack into: a restore rewrites $PAPAIA_CONFIG_DIR wholesale, an upgrade
    migrates and re-renders it while the stack is down. An archive taken across
    either one captures a state that never existed.
    """
    try:
        candidates = [
            (await runner.find_runner(runner.RESTORE_KIND), "restore"),
            (await runner.find_runner(runner.UPGRADE_KIND), "upgrade"),
        ]
    except runner.RunnerError:
        # Docker unreachable is the restore path's problem, not the backup
        # path's -- a backup shells out to papaia-ctl, which reports its own
        # docker failures in the job log.
        return
    for active, label in candidates:
        if active is not None and active.is_running:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"a {label} of {active.target} is still running",
            )


# ---------------------------------------------------------------------------
# Read-only
# ---------------------------------------------------------------------------


@router.get("/backup-dir")
async def backup_dir_info(
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Where backups live, and whether the manager can actually see it."""
    backup_dir = backups.resolve_backup_dir(settings.papaia_config_dir)
    return {
        "path": str(backup_dir) if backup_dir else None,
        "configured": backup_dir is not None,
        "reachable": backups.is_reachable(backup_dir),
    }


@router.get("/restore-points")
async def list_restore_points(
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, Any]]:
    """Catalogued restore points, newest first. Empty when none exist yet."""
    backup_dir = backups.resolve_backup_dir(settings.papaia_config_dir)
    return [
        backups.restore_point_to_dict(p) for p in backups.load_restore_points(backup_dir)
    ]


@router.get("/restore-points/{restore_point_id}")
async def restore_point_detail(
    restore_point_id: str,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """One restore point with the manifest of what it would put back."""
    backup_dir = backups.resolve_backup_dir(settings.papaia_config_dir)
    point = backups.find_restore_point(backup_dir, restore_point_id)
    if point is None:
        raise HTTPException(
            status_code=404, detail=f"restore point {restore_point_id!r} not found"
        )
    manifest = backups.snapshot_manifest(backup_dir, restore_point_id)
    return {
        **backups.restore_point_to_dict(point),
        "manifest": manifest,
        "artifact_list": (manifest or {}).get("artifacts") or [],
    }


@router.get("/restore-points/{restore_point_id}/selectors")
async def restore_point_selectors(
    restore_point_id: str,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """What this restore point can be asked to restore, and what that would stop.

    `supported: false` is the honest answer for a snapshot written before the
    manifest carried its grouping: it is restorable, but only as a whole.

    `services` is the target state read from the shipped Compose fragments, not
    from Docker. The wizard needs it to say which containers a selection stops
    and which keep serving, and it has to answer that while checkboxes are being
    ticked -- so the raw mapping is handed over once instead of a round trip per
    click.
    """
    backup_dir = backups.resolve_backup_dir(settings.papaia_config_dir)
    point = backups.find_restore_point(backup_dir, restore_point_id)
    if point is None:
        raise HTTPException(
            status_code=404, detail=f"restore point {restore_point_id!r} not found"
        )
    manifest = backups.snapshot_manifest(backup_dir, restore_point_id)
    groups = restore_scope.build_groups(manifest)
    expected = inventory.core_inventory(
        settings.papaia_workspace_dir,
        inventory.active_profiles(settings.papaia_config_dir),
    )
    return {
        # The configuration on its own does not make a snapshot selectable: it
        # restores the whole point. Only module and add-on groups do.
        "supported": restore_scope.has_selectable_groups(groups),
        "groups": [restore_scope.group_to_dict(g) for g in groups],
        "notes": list(restore_scope.NOTES),
        "config_selector": restore_scope.CONFIG_SELECTOR,
        "services": [
            {"service": s.service, "module": s.module, "profiles": sorted(s.profiles)}
            for s in expected
        ],
    }


# ---------------------------------------------------------------------------
# Backup — queued job
# ---------------------------------------------------------------------------


@router.post("/backup", status_code=status.HTTP_202_ACCEPTED)
async def create_backup(
    body: BackupBody,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    verify_csrf(request)
    backup_dir = _backup_dir(settings)
    await _require_no_runner()

    queue = _queue()
    # Refused rather than queued. The worker is single-flight, so a second backup
    # accepted here would sit invisible behind the first and start minutes later,
    # against a stack that has moved on -- and the operator who clicked twice
    # because the button looked idle gets told why instead of a second archive.
    active = queue.active_job()
    if active is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a {active.action} job is already running; wait for it to finish",
        )

    _username = _user_id(user)
    _retention = body.retention_days
    _flags = [f"--backup-dir={backup_dir}"]
    if _retention is not None:
        _flags.append(f"--retention-period-days={_retention}")

    async def _callback(ctx: JobContext) -> None:
        ctx.log(f"[ctl] papaia-ctl backup {' '.join(_flags)}")
        gen = await run_core_verb(
            verb="backup",
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
            extra_flags=_flags,
        )
        async for line in gen:
            ctx.log(line)
        write_audit_entry(
            settings.papaia_config_dir,
            user=_username,
            action="backup",
            target=str(backup_dir),
            params={"retention_days": _retention},
            job_id=ctx.job.id,
        )
        ctx.log("[info] done")

    job = await queue.enqueue(
        action="backup",
        target=str(backup_dir),
        user=_username,
        params={"retention_days": _retention},
        callback=_callback,
    )
    return {"job_id": job.id, "status": "queued"}


@router.post("/restore-points/delete", status_code=status.HTTP_202_ACCEPTED)
async def delete_restore_points(
    body: DeleteRestorePointsBody,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    """Delete one or more restore points via `papaia-ctl backup-delete`.

    A queued job, the same shape as a backup: `backup-delete` is not a hot path
    -- it removes a directory and rewrites `backup.yaml` -- but routing it
    through the single-flight worker is what serialises it against a `backup`
    that is writing the same catalogue.
    """
    verify_csrf(request)

    ids = list(dict.fromkeys(body.restore_points))
    malformed = [rp for rp in ids if not backups.is_valid_restore_point_id(rp)]
    if malformed:
        raise HTTPException(
            status_code=400,
            detail=f"not restore point ids: {', '.join(malformed)}",
        )

    backup_dir = _backup_dir(settings)
    missing = [rp for rp in ids if backups.find_restore_point(backup_dir, rp) is None]
    if missing:
        raise HTTPException(
            status_code=404,
            detail=f"restore point(s) not found: {', '.join(missing)}",
        )

    await _require_no_runner()

    queue = _queue()
    active = queue.active_job()
    if active is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a {active.action} job is already running; wait for it to finish",
        )

    _username = _user_id(user)
    _flags = [
        f"--backup-dir={backup_dir}",
        *(f"--restore-point={rp}" for rp in ids),
        "-y",
    ]

    async def _callback(ctx: JobContext) -> None:
        ctx.log(f"[ctl] papaia-ctl backup-delete {' '.join(_flags)}")
        gen = await run_core_verb(
            verb="backup-delete",
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
            extra_flags=_flags,
        )
        async for line in gen:
            ctx.log(line)
        write_audit_entry(
            settings.papaia_config_dir,
            user=_username,
            action="backup-delete",
            target=",".join(ids),
            params={"restore_points": ids},
            job_id=ctx.job.id,
        )
        ctx.log("[info] done")

    job = await queue.enqueue(
        action="backup-delete",
        target=",".join(ids),
        user=_username,
        params={"restore_points": ids},
        callback=_callback,
    )
    return {"job_id": job.id, "status": "queued"}


# ---------------------------------------------------------------------------
# Restore — detached runner
# ---------------------------------------------------------------------------


@router.post("/restore", status_code=status.HTTP_202_ACCEPTED)
async def start_restore(
    body: RestoreBody,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    verify_csrf(request)
    backup_dir = _backup_dir(settings)

    point = backups.find_restore_point(backup_dir, body.restore_point)
    if point is None:
        raise HTTPException(
            status_code=404, detail=f"restore point {body.restore_point!r} not found"
        )
    if not point.is_usable:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"restore point {point.id} is marked 'failed' -- no archive could be "
                "written during that backup, so there is nothing to restore from"
            ),
        )

    existing = await runner.find_runner()
    if existing is not None:
        detail = (
            f"a restore of {existing.target} is still running"
            if existing.is_running
            else (
                f"the result of the previous restore ({existing.target}) has not "
                "been acknowledged yet"
            )
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)

    # An upgrade in flight is mid-migration on the very config directory this
    # would unpack over, and it is holding the stack down while it works.
    upgrading = await runner.find_runner(runner.UPGRADE_KIND)
    if upgrading is not None and upgrading.is_running:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"an upgrade to {upgrading.target} is still running",
        )

    queue = _queue()
    # A queued job counts as much as a running one: the restore replaces the
    # config directory the worker would pick it up from moments later.
    if queue.active_job() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="a job is currently running; wait for it to finish before restoring",
        )

    # Written before the runner starts, and knowingly so: the restore replaces
    # the config directory this log lives in, so the entry describes the
    # pre-restore state and disappears with it. The record that survives is
    # papaia-ctl's backup.log in the backup directory.
    write_audit_entry(
        settings.papaia_config_dir,
        user=_user_id(user),
        action="restore",
        target=point.id,
        params={"restart_clean": body.restart_clean},
    )

    try:
        started = await runner.start_restore(
            restore_point=point.id,
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
            backup_dir=str(backup_dir),
            restart_clean=body.restart_clean,
        )
    except runner.RunnerError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    return runner.status_to_dict(started)


@router.post("/restore/scoped", status_code=status.HTTP_202_ACCEPTED)
async def start_scoped_restore(
    body: ScopedRestoreBody,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    """Restore part of a snapshot as an ordinary queued job.

    This is the one restore that can run in-process, and the reason is narrow: a
    selection carries no configdir artifact, so `$PAPAIA_CONFIG_DIR` is never
    replaced -- and the job log lives at `$PAPAIA_CONFIG_DIR/manager/jobs`, which
    is exactly the fact that forces the whole-snapshot restore into a detached
    container (see app.core.runner). The manager's own profile declares no named
    volume, so no selection can resolve to it either.

    Both properties are enforced by `papaia-ctl restore-scoped` itself, not by
    the flags built here.
    """
    verify_csrf(request)
    backup_dir = _backup_dir(settings)

    point = backups.find_restore_point(backup_dir, body.restore_point)
    if point is None:
        raise HTTPException(
            status_code=404, detail=f"restore point {body.restore_point!r} not found"
        )
    if not point.is_usable:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"restore point {point.id} is marked 'failed' -- no archive could be "
                "written during that backup, so there is nothing to restore from"
            ),
        )

    manifest = backups.snapshot_manifest(backup_dir, point.id)
    groups = restore_scope.build_groups(manifest)
    if not restore_scope.has_selectable_groups(groups):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"restore point {point.id} was written by an older core and carries no "
                "grouping, so it can only be restored as a whole"
            ),
        )
    if restore_scope.requires_full_restore(groups, body.only):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                "this selection needs the stack configuration and must run as a full "
                "restore; use POST /api/v1/maintenance/restore instead"
            ),
        )
    try:
        only_flag = ctl.selection_flag(
            body.only, allowed=restore_scope.allowed_selectors(groups)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    await _require_no_runner()
    queue = _queue()
    active = queue.active_job()
    if active is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a {active.action} job is already running; wait for it to finish",
        )

    _username = _user_id(user)
    _selection = sorted(set(body.only))
    _flags = [
        f"--backup-dir={backup_dir}",
        f"--restore-point={point.id}",
        only_flag,
        "-y",
    ]

    async def _callback(ctx: JobContext) -> None:
        ctx.log(f"[ctl] papaia-ctl restore-scoped {' '.join(_flags)}")
        gen = await run_core_verb(
            verb="restore-scoped",
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
            extra_flags=_flags,
        )
        async for line in gen:
            ctx.log(line)
        # Unlike the whole-snapshot restore, this entry survives: the config
        # directory it lives in is exactly what a scoped restore cannot replace.
        write_audit_entry(
            settings.papaia_config_dir,
            user=_username,
            action="restore-scoped",
            target=point.id,
            params={"only": _selection},
            job_id=ctx.job.id,
        )
        ctx.log("[info] done")

    job = await queue.enqueue(
        action="restore-scoped",
        target=point.id,
        user=_username,
        params={"only": _selection},
        callback=_callback,
    )
    return {"job_id": job.id, "status": "queued"}


@router.get("/restore/status")
async def restore_status(user: AdminUser) -> dict[str, Any]:
    """State of the current or last restore runner.

    Reachable again as soon as the recreated manager container is up, which is
    how the UI picks the operation back up after losing its connection.
    """
    try:
        active = await runner.find_runner()
    except runner.RunnerError as exc:
        return {"active": False, "runner": None, "error": str(exc)}
    log = await runner.runner_log(active.name) if active is not None else ""
    return runner.status_to_dict(active, log)


@router.delete("/restore", status_code=status.HTTP_204_NO_CONTENT)
async def clear_restore(
    request: Request,
    user: AdminUser,
) -> None:
    """Acknowledge a finished restore by removing its runner container."""
    verify_csrf(request)
    active = await runner.find_runner()
    if active is None:
        raise HTTPException(status_code=404, detail="no restore runner to clear")
    try:
        await runner.clear_runner(active.name)
    except runner.RunnerError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
