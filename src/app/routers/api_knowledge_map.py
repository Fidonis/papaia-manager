"""REST API of the knowledge-map page. Administrators only.

Reading the map is a file read (`GET /`); everything the map server knows beyond the files is
passed through from its admin API (simulation, usage, gold-set runs, suggestions), answering
503 with the server's own reason when it can't be asked. A change to an operator file, by hand
or by accepting a suggestion, is a queued job (202 with the job id), see
`app.core.knowledge_map_jobs`.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field

from app.auth.csrf import verify_csrf
from app.auth.deps import AdminUser
from app.auth.oidc import OIDCClaims
from app.config import Settings, get_settings
from app.core import knowledge_map as km
from app.core.audit import write_audit_entry
from app.core.jobs import JobQueue
from app.core.knowledge_map_client import MapServerClient, MapServerError
from app.core.knowledge_map_jobs import ACTION, write_callback
from app.i18n import gettext
from app.routers.api_config import _body
from app.routers.knowledge_map_deps import KnowledgeMapTransport

router = APIRouter(prefix="/api/v1/knowledge-map")


class SimulateBody(BaseModel):
    need: str = Field(min_length=1, max_length=500)
    # None: the administrator's view (every entry). A list: exactly these realm roles.
    roles: list[str] | None = None


class FileBody(BaseModel):
    content: str
    revision: str = ""


class DecisionBody(BaseModel):
    decision: str = Field(pattern="^(accepted|rejected)$")
    days: int = Field(default=28, ge=1, le=90)


def _queue() -> JobQueue:
    from app.main import _job_queue  # noqa: PLC0415

    if _job_queue is None:
        raise HTTPException(status_code=503, detail=gettext("The job queue is not running."))
    return _job_queue


def _user_id(user: OIDCClaims) -> str:
    return user.preferred_username or user.sub


def _client(settings: Settings, transport: KnowledgeMapTransport) -> MapServerClient:
    return MapServerClient(
        settings.knowledge_map_url, km.admin_token(settings.papaia_config_dir), transport
    )


def _unavailable(exc: MapServerError) -> HTTPException:
    return HTTPException(status_code=503, detail=str(exc))


@router.get("")
async def overview(
    user: AdminUser, settings: Annotated[Settings, Depends(get_settings)]
) -> dict[str, Any]:
    """The rendered map, its findings and the operator files, from disk."""
    config_dir = settings.papaia_config_dir
    try:
        files = [km.read_file(config_dir, f.key).as_json() for f in km.OPERATOR_FILES]
    except km.KnowledgeMapError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "active": km.active(config_dir),
        "admin_api": bool(km.admin_token(config_dir)),
        **km.rendered(config_dir),
        "files": files,
    }


@router.get("/server")
async def server_state(
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    transport: KnowledgeMapTransport,
) -> dict[str, Any]:
    """The map server's own view: effective settings, embedder, entries it serves."""
    try:
        data = await _client(settings, transport).map()
    except MapServerError as exc:
        raise _unavailable(exc) from exc
    return {"settings": data.get("settings") or {}, "served": len(data.get("entries") or [])}


@router.post("/simulate")
async def simulate(
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    transport: KnowledgeMapTransport,
) -> dict[str, Any]:
    """Route a question as a role set. The server does not record it."""
    verify_csrf(request)
    body = await _body(request, SimulateBody)
    try:
        return await _client(settings, transport).simulate(body.need, body.roles)
    except MapServerError as exc:
        raise _unavailable(exc) from exc


@router.get("/stats")
async def stats(
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    transport: KnowledgeMapTransport,
    days: Annotated[int, Query(ge=1, le=90)] = 14,
) -> dict[str, Any]:
    try:
        return await _client(settings, transport).stats(days)
    except MapServerError as exc:
        raise _unavailable(exc) from exc


@router.get("/eval/history")
async def eval_history(
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    transport: KnowledgeMapTransport,
) -> dict[str, Any]:
    try:
        return await _client(settings, transport).eval_history()
    except MapServerError as exc:
        raise _unavailable(exc) from exc


@router.post("/eval")
async def run_eval(
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    transport: KnowledgeMapTransport,
) -> dict[str, Any]:
    verify_csrf(request)
    try:
        result = await _client(settings, transport).evaluate()
    except MapServerError as exc:
        raise _unavailable(exc) from exc
    write_audit_entry(
        settings.papaia_config_dir,
        user=_user_id(user),
        action="knowledge-map.eval",
        target="knowledge-map",
        params={"hit_at_1": (result.get("all") or {}).get("hit_at_1")},
    )
    return result


@router.get("/suggestions")
async def suggestions(
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    transport: KnowledgeMapTransport,
    days: Annotated[int, Query(ge=1, le=90)] = 28,
) -> dict[str, Any]:
    try:
        return await _client(settings, transport).suggestions(days)
    except MapServerError as exc:
        raise _unavailable(exc) from exc


@router.post("/suggestions/{suggestion_id}", status_code=status.HTTP_202_ACCEPTED)
async def decide(
    suggestion_id: str,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    transport: KnowledgeMapTransport,
) -> dict[str, Any]:
    """Accept (a job writes the change, renders, then tells the server) or reject (the
    server remembers it and never offers it again)."""
    verify_csrf(request)
    body = await _body(request, DecisionBody)
    client = _client(settings, transport)
    username = _user_id(user)
    if body.decision == "rejected":
        try:
            await client.decide(suggestion_id, "rejected")
        except MapServerError as exc:
            raise _unavailable(exc) from exc
        write_audit_entry(
            settings.papaia_config_dir,
            user=username,
            action="knowledge-map.suggestion.reject",
            target=f"suggestion:{suggestion_id}",
        )
        return {"status": "rejected"}

    # The suggestion is looked up again on the server, never taken from the request: the
    # browser names it, the server says what it is.
    try:
        offered = (await client.suggestions(body.days)).get("suggestions") or []
    except MapServerError as exc:
        raise _unavailable(exc) from exc
    suggestion = next((s for s in offered if s.get("id") == suggestion_id), None)
    if suggestion is None:
        raise HTTPException(status_code=404, detail=gettext("Unknown suggestion."))
    try:
        key, content, revision = km.suggestion_change(settings.papaia_config_dir, suggestion)
    except km.KnowledgeMapError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    async def _tell_server() -> None:
        await client.decide(suggestion_id, "accepted")

    job = await _queue().enqueue(
        action=ACTION,
        target=f"knowledge-map:{key}",
        user=username,
        params={"file": key, "suggestion": suggestion_id, "kind": suggestion.get("kind")},
        callback=write_callback(
            key=key,
            content=content,
            expected_revision=revision,
            user=username,
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
            reason=f"suggestion {suggestion_id} ({suggestion.get('kind')})",
            after=_tell_server,
        ),
    )
    return {"job_id": job.id, "status": "queued", "file": key}


@router.post("/files/{key}/check")
async def check_file(key: str, request: Request, user: AdminUser) -> dict[str, Any]:
    verify_csrf(request)
    body = await _body(request, FileBody)
    try:
        problems = km.check(key, body.content)
    except km.KnowledgeMapError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": not problems, "problems": problems}


@router.put("/files/{key}", status_code=status.HTTP_202_ACCEPTED)
async def write_file(
    key: str,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Save an operator file and render the map with it. Answers with the job."""
    verify_csrf(request)
    body = await _body(request, FileBody)
    try:
        problems = km.check(key, body.content)
        current = km.read_file(settings.papaia_config_dir, key)
    except km.KnowledgeMapError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if problems:
        raise HTTPException(status_code=422, detail={"message": problems[0], "problems": problems})
    if current.revision != body.revision:
        raise HTTPException(
            status_code=409,
            detail=gettext("The file changed after the page loaded it. Reload and try again."),
        )
    username = _user_id(user)
    job = await _queue().enqueue(
        action=ACTION,
        target=f"knowledge-map:{key}",
        user=username,
        params={"file": key},
        callback=write_callback(
            key=key,
            content=body.content.encode("utf-8"),
            expected_revision=body.revision,
            user=username,
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
            reason="edited",
        ),
    )
    return {"job_id": job.id, "status": "queued"}
