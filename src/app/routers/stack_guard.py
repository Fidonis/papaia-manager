"""The one refusal the configuration routes (modules, overlay, mail) share."""

from __future__ import annotations

from fastapi import HTTPException, status

from app.core import runner


async def require_idle_stack() -> None:
    """Refuse while a restore, an upgrade or a stack action owns the configuration.

    They replace or migrate the directory a change would be written to, and a change made
    meanwhile would be undone with it. An ordinary job needs no such refusal: the queue runs
    one at a time."""
    for kind, what in (
        (runner.RESTORE_KIND, "a restore"),
        (runner.UPGRADE_KIND, "an upgrade"),
        (runner.STACK_KIND, "a stack action"),
    ):
        try:
            found = await runner.find_runner(kind)
        except runner.RunnerError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if found is not None and found.is_running:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT, detail=f"{what} is still running"
            )
