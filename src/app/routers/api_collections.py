"""REST API — Qdrant collections of the RAG system and their access roles."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.auth.csrf import verify_csrf
from app.auth.oidc import OIDCClaims
from app.config import Settings, get_settings
from app.core.audit import redact_params, write_audit_entry
from app.core.qdrant import QdrantError, QdrantUnavailable
from app.core.rag_collections import (
    CollectionExists,
    CollectionNotFound,
    CollectionStore,
    InvalidInput,
    RoleGrant,
)
from app.routers.rag_deps import RagAdmin, get_store

router = APIRouter(prefix="/api/v1/rag/collections")


class RoleBody(BaseModel):
    role: str
    access: Literal["r", "rw"] = "r"


class CollectionCreateBody(BaseModel):
    name: str
    vector_size: int
    # The model the ingester will embed with. Empty means "not known yet": the
    # ingester records it on its first run.
    embedding_model: str | None = None
    roles: list[RoleBody] = Field(default_factory=list)


class RolesBody(BaseModel):
    roles: list[RoleBody]


def _user_id(user: OIDCClaims) -> str:
    return user.preferred_username or user.sub


def _grants(roles: list[RoleBody]) -> list[RoleGrant]:
    return [RoleGrant(item.role, item.access) for item in roles]


def _roles_param(roles: list[RoleBody]) -> list[dict[str, str]]:
    return [{"role": item.role.strip(), "access": item.access} for item in roles]


@contextmanager
def _translated(name: str = "") -> Iterator[None]:
    """Answer a store failure with the status code the other admin routes use."""
    try:
        yield
    except InvalidInput as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    except CollectionExists as exc:
        raise HTTPException(
            status_code=409, detail=f"collection {name!r} already exists"
        ) from exc
    except CollectionNotFound as exc:
        raise HTTPException(status_code=404, detail=f"collection {name!r} not found") from exc
    except QdrantUnavailable as exc:
        raise HTTPException(status_code=503, detail=exc.detail) from exc
    except QdrantError as exc:
        raise HTTPException(status_code=502, detail=f"Qdrant: {exc.detail}") from exc


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_collection(
    request: Request,
    body: CollectionCreateBody,
    user: RagAdmin,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[CollectionStore, Depends(get_store)],
) -> dict[str, Any]:
    verify_csrf(request)
    with _translated(body.name):
        await store.create(
            body.name, body.vector_size, body.embedding_model, _grants(body.roles)
        )
    write_audit_entry(
        settings.papaia_config_dir,
        user=_user_id(user),
        action="rag.collection.create",
        target=body.name,
        params=redact_params(
            {
                "vector_size": body.vector_size,
                "embedding_model": (body.embedding_model or "").strip() or None,
                "roles": _roles_param(body.roles),
            }
        ),
    )
    return {"name": body.name, "vector_size": body.vector_size}


@router.put("/{name}/roles")
async def set_roles(
    name: str,
    request: Request,
    body: RolesBody,
    user: RagAdmin,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[CollectionStore, Depends(get_store)],
) -> dict[str, Any]:
    verify_csrf(request)
    with _translated(name):
        await store.set_roles(name, _grants(body.roles))
    write_audit_entry(
        settings.papaia_config_dir,
        user=_user_id(user),
        action="rag.collection.roles.update",
        target=name,
        params=redact_params({"roles": _roles_param(body.roles)}),
    )
    return {"name": name, "roles": _roles_param(body.roles)}


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_collection(
    name: str,
    request: Request,
    user: RagAdmin,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[CollectionStore, Depends(get_store)],
) -> Response:
    verify_csrf(request)
    with _translated(name):
        await store.delete(name)
    write_audit_entry(
        settings.papaia_config_dir,
        user=_user_id(user),
        action="rag.collection.delete",
        target=name,
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/operator-grant")
async def sync_operator_grant(
    request: Request,
    user: RagAdmin,
    settings: Annotated[Settings, Depends(get_settings)],
    store: Annotated[CollectionStore, Depends(get_store)],
) -> dict[str, bool]:
    """Write the ingest operator's global grant if it is missing."""
    verify_csrf(request)
    with _translated():
        written = await store.ensure_operator_grant()
    if written:
        write_audit_entry(
            settings.papaia_config_dir,
            user=_user_id(user),
            action="rag.collection.operator-grant",
            target="*",
        )
    return {"written": written}
