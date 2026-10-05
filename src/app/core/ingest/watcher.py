"""The background task that cleans up after embedding runs when nobody is looking.

A run is the ingester's, and the manager learns that it ended only by asking. The page asks
while it is open; this asks the rest of the time, so an upload is removed shortly after its
run succeeded even if the browser was closed, and a forgotten one is removed when its time
limit passes (see `app.core.ingest.runs.reconcile`).

It looks every few seconds while an upload is being embedded and once a minute otherwise.
Everything it does is idempotent and recorded in the upload's manifest, so a restart of the
manager, or two passes at once, lose nothing. It assumes one process, like the scheduler.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from app.config import Settings
from app.core.ingest.runs import reconcile

log = logging.getLogger(__name__)

BUSY_INTERVAL = 5.0
IDLE_INTERVAL = 60.0


class IngestWatcher:
    def __init__(
        self,
        settings: Settings,
        *,
        transport: Any = None,
        busy_interval: float = BUSY_INTERVAL,
        idle_interval: float = IDLE_INTERVAL,
    ) -> None:
        self._settings = settings
        self._transport = transport
        self._busy = busy_interval
        self._idle = idle_interval
        self._task: asyncio.Task[None] | None = None
        self._stopping: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Begin watching. Needs the running event loop, so it is called from the startup hook."""
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run(), name="ingest-watcher")

    def shutdown(self) -> None:
        if self._task is not None:
            self._task.cancel()
            self._stopping, self._task = self._task, None

    async def stopped(self) -> None:
        """Wait until a cancelled task is gone (used by the tests)."""
        task = self._stopping
        if task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._stopping = None

    async def _run(self) -> None:
        while True:
            interval = self._idle
            try:
                result = await reconcile(self._settings, transport=self._transport)
                if result.waiting:
                    interval = self._busy
            except asyncio.CancelledError:
                raise
            except Exception:
                # A failed pass costs the clean-up of this round, never the manager.
                log.exception("the embedding clean-up failed; it will be tried again")
            await asyncio.sleep(interval)
