"""REST API — job status and log streaming."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query

from app.auth.deps import CurrentUser
from app.config import Settings, get_settings
from app.core.jobs import JobQueue

router = APIRouter(prefix="/api/v1/jobs")

# The queue is injected via dependency; main.py populates the app state.
def _queue(settings: Annotated[Settings, Depends(get_settings)]) -> JobQueue:
    from app.main import _job_queue  # noqa: PLC0415

    if _job_queue is None:
        raise RuntimeError("job queue not initialized")
    return _job_queue


@router.get("")
async def list_jobs(
    user: CurrentUser,
    queue: Annotated[JobQueue, Depends(_queue)],
    limit: int = Query(default=50, ge=1, le=500),
) -> list[dict[str, Any]]:
    jobs = queue.list_jobs(limit=limit)
    return [_job_dict(j) for j in jobs]


@router.get("/{job_id}")
async def get_job(
    job_id: str,
    user: CurrentUser,
    queue: Annotated[JobQueue, Depends(_queue)],
) -> dict[str, Any]:
    job = queue.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found")
    return _job_dict(job)


@router.get("/{job_id}/log")
async def get_job_log(
    job_id: str,
    user: CurrentUser,
    queue: Annotated[JobQueue, Depends(_queue)],
    offset: int = Query(default=0, ge=0),
) -> dict[str, str]:
    if queue.get_job(job_id) is None:
        raise HTTPException(status_code=404, detail=f"job {job_id!r} not found")
    return {"log": queue.read_log(job_id, offset=offset)}


def _job_dict(job: Any) -> dict[str, Any]:
    return {
        "id": job.id,
        "action": job.action,
        "target": job.target,
        "user": job.user,
        "status": job.status.value,
        "created_at": job.created_at.isoformat(),
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
        "exit_code": job.exit_code,
    }
