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
from app.core import backups, runner
from app.core.audit import write_audit_entry
from app.core.ctl import run_core_verb
from app.core.jobs import JobContext, JobQueue, JobStatus

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
    """Reject the request if a restore runner is active."""
    try:
        active = await runner.find_runner()
    except runner.RunnerError:
        # Docker unreachable is the restore path's problem, not the backup
        # path's -- a backup shells out to papaia-ctl, which reports its own
        # docker failures in the job log.
        return
    if active is not None and active.is_running:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"a restore of {active.restore_point} is still running",
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
            f"a restore of {existing.restore_point} is still running"
            if existing.is_running
            else (
                f"the result of the previous restore ({existing.restore_point}) has not "
                "been acknowledged yet"
            )
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=detail)

    queue = _queue()
    if any(j.status is JobStatus.RUNNING for j in queue.list_jobs()):
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
