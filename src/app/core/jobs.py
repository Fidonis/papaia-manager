"""Single-flight job queue with FIFO ordering and persistent log store."""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"


@dataclass
class Job:
    id: str
    action: str
    target: str
    user: str
    created_at: datetime
    status: JobStatus = JobStatus.QUEUED
    started_at: datetime | None = None
    finished_at: datetime | None = None
    exit_code: int | None = None
    params: dict[str, Any] = field(default_factory=dict)


@dataclass
class JobContext:
    job: Job
    log_path: Path

    def log(self, line: str) -> None:
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
        logger.debug("[job %s] %s", self.job.id, line)


JobCallback = Callable[[JobContext], Coroutine[Any, Any, None]]


class JobQueue:
    """Single-flight FIFO queue: at most one job runs at a time.

    Instantiate once at startup and call ``start()`` to begin processing.
    """

    def __init__(self, config_dir: str) -> None:
        self._config_dir = config_dir
        self._jobs: dict[str, Job] = {}
        self._callbacks: dict[str, JobCallback] = {}
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._lock = asyncio.Lock()
        self._worker_task: asyncio.Task[None] | None = None

    @property
    def jobs_dir(self) -> Path:
        return Path(self._config_dir) / "manager" / "jobs"

    def start(self) -> None:
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker(), name="job-worker")

    def stop(self) -> None:
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()

    async def enqueue(
        self,
        *,
        action: str,
        target: str,
        user: str,
        params: dict[str, Any] | None = None,
        callback: JobCallback,
    ) -> Job:
        job = Job(
            id=str(uuid.uuid4()),
            action=action,
            target=target,
            user=user,
            created_at=datetime.now(tz=UTC),
            params=params or {},
        )
        self._jobs[job.id] = job
        self._callbacks[job.id] = callback
        self.jobs_dir.mkdir(parents=True, exist_ok=True)
        self._append_index(job)
        await self._queue.put(job.id)
        return job

    def get_job(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list_jobs(self, limit: int = 50) -> list[Job]:
        all_jobs = sorted(
            self._jobs.values(), key=lambda j: j.created_at, reverse=True
        )
        return all_jobs[:limit]

    def read_log(self, job_id: str, offset: int = 0) -> str:
        log_path = self.jobs_dir / f"{job_id}.log"
        if not log_path.exists():
            return ""
        lines = log_path.read_text(encoding="utf-8").splitlines()
        return "\n".join(lines[offset:])

    def _append_index(self, job: Job) -> None:
        index_path = self.jobs_dir / "index.jsonl"
        record: dict[str, Any] = {
            "id": job.id,
            "action": job.action,
            "target": job.target,
            "user": job.user,
            "status": job.status.value,
            "created_at": job.created_at.isoformat(),
        }
        with index_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record) + "\n")

    async def _worker(self) -> None:
        logger.info("job worker started")
        while True:
            job_id = await self._queue.get()
            job = self._jobs.get(job_id)
            callback = self._callbacks.pop(job_id, None)
            if job is None or callback is None:
                self._queue.task_done()
                continue

            async with self._lock:
                job.status = JobStatus.RUNNING
                job.started_at = datetime.now(tz=UTC)
                self._append_index(job)

                log_path = self.jobs_dir / f"{job_id}.log"
                ctx = JobContext(job=job, log_path=log_path)
                try:
                    await callback(ctx)
                    job.status = JobStatus.SUCCEEDED
                    job.exit_code = 0
                except Exception as exc:
                    logger.exception("job %s failed", job_id)
                    ctx.log(f"--- ERROR: {exc} ---")
                    job.status = JobStatus.FAILED
                    job.exit_code = 1
                finally:
                    job.finished_at = datetime.now(tz=UTC)
                    self._append_index(job)

            self._queue.task_done()
