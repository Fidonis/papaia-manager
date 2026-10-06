"""REST API — embedding files into a collection through the ingester.

Three groups, all for administrators of a deployment that runs the RAG system:

* the **upload area** (`/uploads`): staging folders for files that are removed after a run,
* the **tree** of a folder to pick from (`/tree`),
* the **runs** (`/runs`): start one, read it, abort it.

Runs work on the connection named by the `connection` query parameter, like the Collections
API, and the staging area does not need a connection at all.
"""
from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field
from starlette.formparsers import MultiPartException

from app.auth.csrf import verify_csrf
from app.auth.oidc import OIDCClaims
from app.config import Settings, get_settings
from app.core.audit import redact_params, write_audit_entry
from app.core.ingest.runs import SourceSpec, StartRequest, browse_root
from app.core.ingest.uploads import Batch, UploadStore, new_store
from app.core.rag import documents_dir
from app.routers.rag_deps import EmbeddingServiceDep, RagAdmin
from app.routers.rag_deps import translated as _translated

router = APIRouter(prefix="/api/v1/rag/ingest")

# What a multipart body adds to the file itself, generously.
_FORM_OVERHEAD = 1024 * 1024


class SourceBody(BaseModel):
    kind: Literal["upload", "folder"]
    batch: str | None = Field(default=None, max_length=64)
    paths: list[str] = Field(default_factory=list, max_length=500)


class RunBody(BaseModel):
    collection: str = Field(max_length=255)
    mode: Literal["add", "replace"]
    source: SourceBody
    # Empty means "the model the collection records".
    model: str | None = Field(default=None, max_length=255)
    confirm_replace: bool = False
    confirm_other_jobs: bool = False


class UploadBody(BaseModel):
    name: str = Field(default="", max_length=120)


def _user_id(user: OIDCClaims) -> str:
    return user.preferred_username or user.sub


def get_upload_store(
    settings: Annotated[Settings, Depends(get_settings)],
) -> UploadStore:
    """The staging area, or a 503 that says why the documents folder cannot be used."""
    docs = documents_dir(settings.papaia_config_dir, settings.papaia_workspace_dir)
    if docs.path is None:
        raise HTTPException(status_code=503, detail=docs.reason)
    return new_store(settings, docs.path)


UploadStoreDep = Annotated[UploadStore, Depends(get_upload_store)]


def _batch_payload(batch: Batch) -> dict[str, Any]:
    return {
        "id": batch.id,
        "owner": batch.owner,
        "owner_name": batch.owner_name,
        "name": batch.name,
        "created": batch.created,
        "updated": batch.updated,
        "state": batch.state,
        "files": batch.files,
        "bytes": batch.bytes,
        "collection": batch.collection,
        "run_id": batch.run_id,
        "note": batch.note,
    }


# ---------------------------------------------------------------------------
# What the page may offer
# ---------------------------------------------------------------------------


@router.get("/status")
async def ingest_status(user: RagAdmin, service: EmbeddingServiceDep) -> dict[str, Any]:
    """Whether the ingester can be used, whether it can update without deleting, and the
    state of the documents folder."""
    with _translated():
        state = await service.status()
    return {
        "ready": state.ready,
        "reason": state.reason,
        "supports_add": state.supports_add,
        "documents": {
            "available": state.documents.path is not None,
            "reason": state.documents.reason,
            "writable": state.documents.writable,
        },
    }


# ---------------------------------------------------------------------------
# The upload area
# ---------------------------------------------------------------------------


@router.get("/uploads")
async def list_uploads(user: RagAdmin, store: UploadStoreDep) -> dict[str, Any]:
    return {"uploads": [_batch_payload(batch) for batch in store.batches()]}


@router.post("/uploads", status_code=status.HTTP_201_CREATED)
async def create_upload(
    request: Request,
    body: UploadBody,
    user: RagAdmin,
    settings: Annotated[Settings, Depends(get_settings)],
    store: UploadStoreDep,
) -> dict[str, Any]:
    verify_csrf(request)
    with _translated():
        batch = store.create(
            owner_sub=user.sub, owner_name=_user_id(user), name=body.name
        )
    write_audit_entry(
        settings.papaia_config_dir,
        user=_user_id(user),
        action="rag.ingest.upload.create",
        target=batch.id,
        params=redact_params({"owner": batch.owner, "name": batch.name or None}),
    )
    return _batch_payload(batch)


@router.post("/uploads/{batch_id}/files", status_code=status.HTTP_201_CREATED)
async def upload_file(
    batch_id: str,
    request: Request,
    user: RagAdmin,
    settings: Annotated[Settings, Depends(get_settings)],
    store: UploadStoreDep,
) -> dict[str, Any]:
    """Store one file in an upload: the form carries `file` and, optionally, `path`.

    The body is read here, after the role and the CSRF token have been checked, and not as a
    declared parameter: FastAPI would parse a multipart body before it looks at who sent it.
    """
    verify_csrf(request)
    limit = settings.ingest_max_upload_mb * 2**20 + _FORM_OVERHEAD
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        raise HTTPException(
            status_code=413,
            detail=f"The file is larger than the limit of {settings.ingest_max_upload_mb} MiB "
            "(INGEST_MAX_UPLOAD_MB).",
        )
    try:
        async with request.form(max_files=1, max_fields=4, max_part_size=8192) as form:
            upload = form.get("file")
            raw_path = form.get("path")
            if upload is None or isinstance(upload, str):
                raise HTTPException(status_code=422, detail="The form needs a 'file' part.")
            name = raw_path if isinstance(raw_path, str) and raw_path else (upload.filename or "")
            with _translated(batch_id):
                saved = await store.save_file(batch_id, name, upload)
    except MultiPartException as exc:
        detail = f"The upload could not be read: {exc.message}"
        raise HTTPException(status_code=422, detail=detail) from exc
    return {"path": saved.path, "bytes": saved.bytes, "replaced": saved.replaced}


@router.delete("/uploads/{batch_id}", status_code=status.HTTP_204_NO_CONTENT)
async def discard_upload(
    batch_id: str,
    request: Request,
    user: RagAdmin,
    settings: Annotated[Settings, Depends(get_settings)],
    store: UploadStoreDep,
) -> Response:
    verify_csrf(request)
    with _translated(batch_id):
        batch = store.discard(batch_id)
    write_audit_entry(
        settings.papaia_config_dir,
        user=_user_id(user),
        action="rag.ingest.upload.discard",
        target=batch.id,
        params={"owner": batch.owner, "files": batch.files, "bytes": batch.bytes},
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# The tree
# ---------------------------------------------------------------------------


@router.get("/tree")
async def tree(
    user: RagAdmin,
    settings: Annotated[Settings, Depends(get_settings)],
    source: Annotated[Literal["folder", "upload"], Query()] = "folder",
    upload: Annotated[str | None, Query(max_length=64)] = None,
    path: Annotated[str, Query(max_length=1024)] = "",
) -> dict[str, Any]:
    with _translated(path or "the folder"):
        listing = browse_root(settings, source, upload).list_dir(path)
    return {
        "path": listing.path,
        "truncated": listing.truncated,
        "entries": [
            {"name": e.name, "path": e.rel, "dir": e.is_dir, "size": e.size}
            for e in listing.entries
        ],
    }


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


@router.post("/runs", status_code=status.HTTP_202_ACCEPTED)
async def start_run(
    request: Request,
    body: RunBody,
    user: RagAdmin,
    service: EmbeddingServiceDep,
) -> dict[str, Any]:
    verify_csrf(request)
    with _translated(body.collection):
        started = await service.start(
            StartRequest(
                collection=body.collection,
                mode=body.mode,
                source=SourceSpec(body.source.kind, body.source.batch, tuple(body.source.paths)),
                model=body.model,
                confirm_replace=body.confirm_replace,
                confirm_other_jobs=body.confirm_other_jobs,
            ),
            user=_user_id(user),
        )
    return {
        "run_id": started.run_id,
        "job_id": started.job_id,
        "files": started.files,
        "bytes": started.bytes,
    }


@router.get("/runs")
async def list_runs(
    user: RagAdmin,
    service: EmbeddingServiceDep,
    collection: Annotated[str, Query(max_length=255)],
    limit: Annotated[int, Query(ge=1, le=20)] = 5,
) -> dict[str, Any]:
    with _translated():
        views = await service.history(collection, limit=limit)
    return {"runs": [view.as_dict() for view in views]}


@router.get("/runs/{run_id}")
async def get_run(
    run_id: str,
    user: RagAdmin,
    service: EmbeddingServiceDep,
    collection: Annotated[str | None, Query(max_length=255)] = None,
) -> dict[str, Any]:
    with _translated(run_id):
        view = await service.run_view(run_id, collection=collection)
    return view.as_dict()


@router.delete("/runs/{run_id}", status_code=status.HTTP_204_NO_CONTENT)
async def abort_run(
    run_id: str,
    request: Request,
    user: RagAdmin,
    service: EmbeddingServiceDep,
) -> Response:
    verify_csrf(request)
    with _translated(run_id):
        await service.abort(run_id, user=_user_id(user))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
