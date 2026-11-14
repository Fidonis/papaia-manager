"""The job behind every change the knowledge-map page makes to an operator file.

A queued job like the overlay's, so it is single-flight with everything else the manager does
to the stack, streams a log and leaves an audit entry. The sequence:

1. check that the file is still the one the page showed, write it (the old bytes go to the
   history first);
2. for a file the map is rendered from, run the core's `start` of the `knowledge-map`
   profile. `start` renders before it brings anything up and leaves a container whose
   configuration did not change alone; the map server notices the new files and reloads.
   A file the render refuses (a duplicate id, instruction-shaped text) fails the `start`:
   the previous file is put back and rendered again, and the job reports the refusal;
3. for an accepted suggestion, tell the map server, so it is not offered again.

File content is never written to the job log or the audit log.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from app.core import knowledge_map as km
from app.core.audit import write_audit_entry
from app.core.config import modules
from app.core.config.service import StepFailedError, _verb
from app.core.jobs import JobCallback, JobContext
from app.i18n import gettext as _

ACTION = "knowledge-map-write"
_FLAG = f"--profiles={km.PROFILE}"


class RenderRefusedError(Exception):
    """The core did not render with the new file; the previous one is back."""


def write_callback(
    *,
    key: str,
    content: bytes,
    expected_revision: str,
    user: str,
    workspace_dir: str,
    config_dir: str,
    reason: str,
    after: Callable[[], Awaitable[None]] | None = None,
) -> JobCallback:
    spec = km.operator_file(key)
    subject = f"knowledge-map:{key}"

    async def _callback(ctx: JobContext) -> None:
        previous = km.read_file(config_dir, key)
        written = km.write_file(config_dir, key, content, expected_revision=expected_revision)
        ctx.log(f"[info] {spec.filename} written ({reason})")
        params = {
            "reason": reason,
            "bytes": len(content),
            "revision_before": previous.revision[:7],
            "revision_after": written.revision[:7],
        }
        rendered = spec.renders and km.PROFILE in modules.active(config_dir)
        if spec.renders and not rendered:
            ctx.log("[info] the knowledge map is not switched on; the file applies at its start")
        if rendered:
            try:
                await _verb(ctx, "start", _FLAG, workspace_dir=workspace_dir, config_dir=config_dir)
            except StepFailedError as exc:
                ctx.log("[rollback] putting the previous file back")
                if km.restore_file(config_dir, key, previous.raw, written.revision):
                    try:
                        await _verb(
                            ctx, "start", _FLAG, workspace_dir=workspace_dir, config_dir=config_dir
                        )
                    except StepFailedError as again:
                        ctx.log(f"[rollback] render with the previous file failed too: {again}")
                else:
                    ctx.log("[rollback] the file was edited on the host meanwhile; left alone")
                write_audit_entry(
                    config_dir,
                    user=user,
                    action="knowledge-map.write",
                    target=subject,
                    params=params,
                    job_id=ctx.job.id,
                    result="error",
                )
                raise RenderRefusedError(
                    _("The map was not rendered with this change, so the previous file is back. "
                      "(%(exc)s)") % {"exc": exc}
                ) from exc
        if after is not None:
            try:
                await after()
            except Exception as exc:  # noqa: BLE001 - the change is in place; say so and go on
                ctx.log(f"[warn] the map server was not told about the decision: {exc}")
        write_audit_entry(
            config_dir,
            user=user,
            action="knowledge-map.write",
            target=subject,
            params={**params, "rendered": rendered},
            job_id=ctx.job.id,
        )
        ctx.log("[info] done")

    return _callback
