"""REST API of the knowledge map's governance view. Administrators only.

`GET /` assembles the view from disk (the rendered map, `governance.yaml`, the render's
governance findings and add-on proposals) and from LiteLLM's model list; LiteLLM that can't be
read is stated in the answer and the view is evaluated from the files alone. Every change
builds the new `governance.yaml` from the one the page read and goes through the same
compare-and-swap job as the operator files (202 with the job id). The fact export is a download
for the operator's own GRC, CMDB or wiki.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.auth.csrf import verify_csrf
from app.auth.deps import AdminUser
from app.auth.oidc import OIDCClaims
from app.config import Settings, get_settings
from app.core import knowledge_map as km
from app.core import knowledge_map_governance as gov
from app.core.audit import write_audit_entry
from app.core.knowledge_map_jobs import ACTION, write_callback
from app.i18n import gettext
from app.routers.api_config import _body
from app.routers.api_knowledge_map import _queue, _user_id
from app.routers.models_deps import LitellmTransport

router = APIRouter(prefix="/api/v1/knowledge-map/governance")

KEY = "governance"
_SEVERITY_ORDER = {s: i for i, s in enumerate(gov.SEVERITIES)}

Classification = Literal["public", "internal", "confidential", "strictly_confidential"]
PersonalData = Literal["none", "ordinary", "special", "criminal", "unknown"]
Subject = Literal["employees", "applicants", "customers", "suppliers", "patients", "minors",
                  "other"]
Tier = Literal["local", "eu_hosted", "third_country_contracted", "third_country"]


class EntryBody(BaseModel):
    revision: str = ""
    owner: str | None = Field(default=None, max_length=200)
    classification: Classification | None = None
    personal_data: PersonalData | None = None
    data_subjects: list[Subject] | None = None
    ropa_ref: str | None = Field(default=None, max_length=100)


class RevisionBody(BaseModel):
    revision: str = ""


class ModelBody(BaseModel):
    revision: str = ""
    # None removes the model from governance.yaml (it is then evaluated as third_country).
    tier: Tier | None = None
    role: str | None = Field(default=None, max_length=50)


class ExceptionItem(BaseModel):
    finding: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_]+$")
    subject: str = Field(min_length=1, max_length=200)
    expires: date
    reason: str | None = Field(default=None, max_length=500)
    risk_ref: str | None = Field(default=None, max_length=200)


class ExceptionsBody(BaseModel):
    revision: str = ""
    exceptions: list[ExceptionItem] = Field(default_factory=list, max_length=200)


def _today() -> date:
    return datetime.now(tz=UTC).date()


# ── the view ────────────────────────────────────────────────────────────────────────────────


def build_view(config_dir: str, live: gov.LiveModels, today: date) -> dict[str, Any]:
    """The whole governance view, from disk and one LiteLLM reading."""
    try:
        document, state, parse_error = gov.read_document(config_dir)
    except km.KnowledgeMapError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    rendered = km.rendered(config_dir)
    entries = rendered["entries"]
    agents = [e for e in entries if e.get("kind") == "agent"]
    models, model_findings, tiers = gov.model_view(document, live, agents)
    exceptions = gov.exceptions_view(document, today)
    valid = [x for x in exceptions if not x["expired"]]
    findings = gov.render_findings(rendered["findings"]) + gov.apply_exceptions(
        model_findings, valid
    )
    findings.sort(key=lambda f: (_SEVERITY_ORDER.get(str(f.get("severity")), 3),
                                 str(f.get("code")), str(f.get("target"))))
    return {
        "revision": state.revision,
        "exists": state.raw is not None,
        "parse_error": parse_error,
        "rendered_at": rendered["generated_at"],
        "litellm": {"available": live.available, "problem": live.problem},
        "matrix": gov.matrix(entries, document),
        "models": models,
        "agents": gov.agents_view(entries, document, tiers),
        "findings": findings,
        "exceptions": exceptions,
        "enums": {
            "tiers": list(gov.TIERS),
            "classes": list(gov.CLASSES),
            "personal_data": list(gov.PERSONAL_DATA),
            "subjects": list(gov.SUBJECTS),
        },
        "exports": list(gov.EXPORTS),
    }


async def _view(settings: Settings, transport: Any) -> dict[str, Any]:
    live = await gov.read_live_models(settings, transport)
    return build_view(settings.papaia_config_dir, live, _today())


@router.get("")
async def overview(
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    transport: LitellmTransport,
) -> dict[str, Any]:
    """Coverage matrix, model inventory, agents and findings with their fixes."""
    return await _view(settings, transport)


# ── changes ─────────────────────────────────────────────────────────────────────────────────


def _current(settings: Settings, revision: str) -> dict[str, Any]:
    """The document the page read, or 409 when it changed since or can't be parsed."""
    try:
        state = km.read_file(settings.papaia_config_dir, KEY)
    except km.KnowledgeMapError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if state.revision != revision:
        raise HTTPException(
            status_code=409,
            detail=gettext("The file changed after the page loaded it. Reload and try again."),
        )
    try:
        document = km._parsed(state)  # noqa: SLF001 - the one parser of operator files
    except km.KnowledgeMapError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return document


def _entry(settings: Settings, entry_id: str) -> dict[str, Any]:
    entries: list[dict[str, Any]] = km.rendered(settings.papaia_config_dir)["entries"]
    entry = next((e for e in entries if e["id"] == entry_id), None)
    if entry is None:
        raise HTTPException(status_code=404, detail=gettext("Unknown entry."))
    return entry


async def _write(
    settings: Settings, user: OIDCClaims, document: dict[str, Any], revision: str, reason: str
) -> dict[str, Any]:
    content = km._dump(document)  # noqa: SLF001 - the one writer of operator files
    problems = km.check(KEY, content.decode("utf-8"))
    if problems:
        raise HTTPException(status_code=422, detail={"message": problems[0], "problems": problems})
    username = _user_id(user)
    job = await _queue().enqueue(
        action=ACTION,
        target=f"knowledge-map:{KEY}",
        user=username,
        params={"file": KEY, "change": reason},
        callback=write_callback(
            key=KEY,
            content=content,
            expected_revision=revision,
            user=username,
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
            reason=reason,
        ),
    )
    return {"job_id": job.id, "status": "queued"}


@router.put("/entries/{entry_id}", status_code=status.HTTP_202_ACCEPTED)
async def edit_entry(
    entry_id: str,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Owner, classification, personal data, data subjects and the record-of-processing
    reference of one entry. A field left out stays; an empty one is removed."""
    verify_csrf(request)
    body = await _body(request, EntryBody)
    _entry(settings, entry_id)
    document = _current(settings, body.revision)
    changes = body.model_dump(exclude_unset=True, exclude={"revision"})
    updated = gov.set_entry(document, entry_id, changes)
    return await _write(settings, user, updated, body.revision, f"governance entry {entry_id}")


@router.post("/entries/{entry_id}/confirm-proposal", status_code=status.HTTP_202_ACCEPTED)
async def confirm_proposal(
    entry_id: str,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Take over the add-on's proposal (as the last render saw it) for every field not set."""
    verify_csrf(request)
    body = await _body(request, RevisionBody)
    entry = _entry(settings, entry_id)
    document = _current(settings, body.revision)
    try:
        updated = gov.confirm_proposal(document, entry_id, entry["governance_proposal"])
    except gov.GovernanceError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return await _write(settings, user, updated, body.revision, f"governance proposal {entry_id}")


@router.put("/models/{name:path}", status_code=status.HTTP_202_ACCEPTED)
async def edit_model(
    name: str,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """The tier of one model, by its LiteLLM name; no tier removes it."""
    verify_csrf(request)
    if not name or len(name) > 200 or any(c.isspace() or not c.isprintable() for c in name):
        raise HTTPException(status_code=404, detail=gettext("Unknown model."))
    body = await _body(request, ModelBody)
    document = _current(settings, body.revision)
    changes = body.model_dump(exclude_unset=True, exclude={"revision"})
    updated = gov.set_model(document, name, None if body.tier is None else changes)
    return await _write(settings, user, updated, body.revision, f"governance model {name}")


@router.put("/exceptions", status_code=status.HTTP_202_ACCEPTED)
async def edit_exceptions(
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """The accepted findings, each with an expiry; the list replaces the one in the file."""
    verify_csrf(request)
    body = await _body(request, ExceptionsBody)
    document = _current(settings, body.revision)
    updated = gov.set_exceptions(document, [item.model_dump() for item in body.exceptions])
    return await _write(settings, user, updated, body.revision, "governance exceptions")


# ── the fact export ─────────────────────────────────────────────────────────────────────────


def _generator() -> str:
    from importlib import metadata  # noqa: PLC0415

    try:
        return f"papaia-manager {metadata.version('papaia-manager')}"
    except metadata.PackageNotFoundError:
        return "papaia-manager"


@router.get("/export/{name}")
async def export(
    name: str,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    transport: LitellmTransport,
) -> Response:
    """The facts of the map as JSON or as CSV (entries only), for import elsewhere."""
    if name not in gov.EXPORTS:
        raise HTTPException(status_code=404, detail=gettext("Unknown export."))
    view = await _view(settings, transport)
    data = gov.facts(view, datetime.now(tz=UTC), _generator())
    if name == "facts.json":
        content, media = gov.facts_json(data), "application/json"
    else:
        content, media = gov.facts_csv(data), "text/csv; charset=utf-8"
    write_audit_entry(
        settings.papaia_config_dir,
        user=_user_id(user),
        action="knowledge-map.export",
        target=f"knowledge-map:{name}",
    )
    return Response(
        content=content.encode("utf-8"),
        media_type=media,
        headers={
            "Content-Disposition": f'attachment; filename="knowledge-map-{name}"',
            "Cache-Control": "no-store",
        },
    )
