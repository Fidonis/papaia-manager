"""REST API -- reading, exporting and pruning the audit log.

Every route is admin-only and read side effects stop at that: `GET` and its
export never touch the file, only `POST /prune` does, and it goes through the
same CSRF check as every other mutating route in this app.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.auth.csrf import verify_csrf
from app.auth.deps import AdminUser
from app.auth.oidc import OIDCClaims
from app.config import Settings, get_settings
from app.core.audit import (
    AuditFilter,
    build_filter,
    csv_safe,
    iter_entries,
    parse_cutoff,
    prune_before,
    query_entries,
    write_audit_entry,
)

router = APIRouter(prefix="/api/v1/audit")


class PruneBody(BaseModel):
    before: str
    dry_run: bool = False


def _user_id(user: OIDCClaims) -> str:
    return user.preferred_username or user.sub


def _filter_from_query(
    *,
    user: str | None,
    action: str | None,
    result: str | None,
    target: str | None,
    since: str | None,
    before: str | None,
) -> AuditFilter:
    try:
        return build_filter(
            user=user, action=action, result=result, target=target, since=since, before=before
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("")
async def list_entries(
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    user_filter: Annotated[str | None, Query(alias="user")] = None,
    action: str | None = None,
    result: str | None = None,
    target: str | None = None,
    since: str | None = None,
    before: str | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> dict[str, Any]:
    flt = _filter_from_query(
        user=user_filter, action=action, result=result, target=target, since=since, before=before
    )
    page = query_entries(settings.papaia_config_dir, flt, limit=limit, offset=offset)
    return {
        "entries": [asdict(e) for e in page.entries],
        "total": page.total,
        "limit": limit,
        "offset": offset,
        "corrupt_lines": page.corrupt_lines,
        "facets": page.facets,
    }


@router.get("/export")
async def export_entries(
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    format: Literal["csv", "jsonl"] = "csv",
    user_filter: Annotated[str | None, Query(alias="user")] = None,
    action: str | None = None,
    result: str | None = None,
    target: str | None = None,
    since: str | None = None,
    before: str | None = None,
) -> StreamingResponse:
    flt = _filter_from_query(
        user=user_filter, action=action, result=result, target=target, since=since, before=before
    )
    config_dir = settings.papaia_config_dir

    def _csv_row(*cells: str) -> str:
        # A minimal quoter rather than the `csv` module: every cell is a single
        # log field with no embedded newline, so the one thing worth handling
        # by hand is a comma or a quote inside it.
        return ",".join(
            '"' + cell.replace('"', '""') + '"' if ("," in cell or '"' in cell) else cell
            for cell in cells
        ) + "\r\n"

    def _rows() -> Iterator[str]:
        if format == "jsonl":
            for entry in iter_entries(config_dir, flt):
                yield json.dumps(asdict(entry)) + "\n"
            return
        yield _csv_row("ts", "user", "action", "target", "result", "job_id", "params")
        for entry in iter_entries(config_dir, flt):
            yield _csv_row(
                csv_safe(entry.ts),
                csv_safe(entry.user),
                csv_safe(entry.action),
                csv_safe(entry.target),
                csv_safe(entry.result),
                entry.job_id or "",
                json.dumps(entry.params) if entry.params is not None else "",
            )

    ext = "jsonl" if format == "jsonl" else "csv"
    media_type = "application/x-ndjson" if format == "jsonl" else "text/csv"
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")
    response = StreamingResponse(_rows(), media_type=media_type)
    response.headers["Content-Disposition"] = f'attachment; filename="audit-{stamp}.{ext}"'
    response.headers["Cache-Control"] = "no-store"
    return response


@router.post("/prune")
async def prune(
    body: PruneBody,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    verify_csrf(request)
    try:
        cutoff = parse_cutoff(body.before)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if cutoff > datetime.now(tz=UTC):
        raise HTTPException(status_code=422, detail="cutoff date is in the future")

    result = prune_before(settings.papaia_config_dir, cutoff, dry_run=body.dry_run)

    # The prune documents its own gap in the record it just made a gap in --
    # but only when it actually changed the file; a dry run or a no-op cutoff
    # must not itself become a reason to write to it.
    if result.removed > 0 and not body.dry_run:
        write_audit_entry(
            settings.papaia_config_dir,
            user=_user_id(user),
            action="audit-prune",
            target="audit.log",
            params={
                "before": body.before,
                "removed": result.removed,
                "kept": result.kept,
                "oldest_removed": result.oldest_removed,
                "newest_removed": result.newest_removed,
            },
        )

    return {
        "before": body.before,
        "dry_run": body.dry_run,
        "removed": result.removed,
        "kept": result.kept,
        "oldest_removed": result.oldest_removed,
        "newest_removed": result.newest_removed,
    }
