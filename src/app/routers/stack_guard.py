"""The one refusal the configuration routes (modules, overlay, mail) share."""

from __future__ import annotations

from fastapi import HTTPException, status

from app.config import Settings, get_settings
from app.core import nodes, runner
from app.i18n import N_
from app.i18n import gettext as _


def refuse_while_locked(settings: Settings) -> None:
    """Refuse what the core would refuse in a distributed installation whose nodes are not on
    one release, so that the page says why instead of queueing a job that fails."""
    reason = nodes.lock_reason(settings.papaia_config_dir, settings.papaia_workspace_dir)
    if reason:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=reason)


async def require_idle_stack() -> None:
    """Refuse while a restore, an upgrade or a stack action owns the configuration.

    They replace or migrate the directory a change would be written to, and a change made
    meanwhile would be undone with it. An ordinary job needs no such refusal: the queue runs
    one at a time. A locked installation is refused as well: every change ends in a `start`
    or a `setup`, and the core refuses both."""
    for kind, running in (
        (runner.RESTORE_KIND, N_("a restore is still running")),
        (runner.UPGRADE_KIND, N_("an upgrade is still running")),
        (runner.STACK_KIND, N_("a stack action is still running")),
    ):
        try:
            found = await runner.find_runner(kind)
        except runner.RunnerError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        if found is not None and found.is_running:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=_(running))
    refuse_while_locked(get_settings())
