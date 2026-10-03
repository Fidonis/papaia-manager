"""Starting a backup: the one path the button and the schedule share.

Both end up as an ordinary queued job running `papaia-ctl backup`, with the same
log, the same audit entry and the same refusal while a restore or an upgrade is
running. What differs is who asks (`user`) and why (`trigger`), and nothing about
the way the job runs.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import Settings
from app.core import runner
from app.core.audit import write_audit_entry
from app.core.ctl import run_core_verb
from app.core.jobs import Job, JobContext, JobQueue

MANUAL = "manual"
SCHEDULE = "schedule"


@dataclass(frozen=True)
class BlockingRunner:
    """A detached restore or upgrade that a backup must not run alongside."""

    label: str
    target: str


async def blocking_runner() -> BlockingRunner | None:
    """The restore or upgrade runner that is still running, or None.

    A restore rewrites $PAPAIA_CONFIG_DIR wholesale and an upgrade migrates and
    re-renders it while the stack is down: an archive taken across either one
    captures a state that never existed.

    Docker being unreachable is not a reason to refuse. That is the restore
    path's problem, not the backup path's -- a backup shells out to papaia-ctl,
    which reports its own docker failures in the job log.
    """
    try:
        candidates = [
            (await runner.find_runner(runner.RESTORE_KIND), "restore"),
            (await runner.find_runner(runner.UPGRADE_KIND), "upgrade"),
        ]
    except runner.RunnerError:
        return None
    for active, label in candidates:
        if active is not None and active.is_running:
            return BlockingRunner(label=label, target=active.target)
    return None


def backup_flags(backup_dir: Path, retention_days: int | None) -> list[str]:
    """The flags for `papaia-ctl backup`.

    A retention is only passed when there is one: papaia-ctl prunes only when
    `--retention-period-days` is given, so absence and 0 are different requests,
    and 0 (delete everything older than today) must stay expressible.
    """
    flags = [f"--backup-dir={backup_dir}"]
    if retention_days is not None:
        flags.append(f"--retention-period-days={retention_days}")
    return flags


async def enqueue_backup(
    queue: JobQueue,
    settings: Settings,
    backup_dir: Path,
    *,
    user: str,
    retention_days: int | None,
    trigger: str = MANUAL,
    note: str | None = None,
) -> Job:
    """Queue `papaia-ctl backup` and audit it when it has run.

    The caller has already decided that a backup may start: the queue is idle, no
    runner is active, the directory is reachable. Those refusals are not made here
    because the button answers them with a 409 and the schedule with a skip and a
    retry, and one function cannot do both.

    `note` is written to the top of the job log -- the schedule uses it to say why
    it held the retention back.
    """
    flags = backup_flags(backup_dir, retention_days)
    params: dict[str, Any] = {"retention_days": retention_days}
    if trigger != MANUAL:
        params["trigger"] = trigger

    async def _callback(ctx: JobContext) -> None:
        if note:
            ctx.log(f"[{trigger}] {note}")
        ctx.log(f"[ctl] papaia-ctl backup {' '.join(flags)}")
        gen = await run_core_verb(
            verb="backup",
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
            extra_flags=flags,
        )
        async for line in gen:
            ctx.log(line)
        write_audit_entry(
            settings.papaia_config_dir,
            user=user,
            action="backup",
            target=str(backup_dir),
            params=params,
            job_id=ctx.job.id,
        )
        ctx.log("[info] done")

    return await queue.enqueue(
        action="backup",
        target=str(backup_dir),
        user=user,
        params={"retention_days": retention_days},
        callback=_callback,
    )
