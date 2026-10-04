"""REST API — vector database connections of the RAG system.

Every route is for administrators, on a deployment that runs the RAG system, and every
change is audited. No response and no audit entry carries an api-key or the stored token:
a connection is described by `has_key` and a state, and a change by what happened to the
key (`set`, `none`, `kept`, `replaced`, `removed`).
"""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from pydantic import BaseModel, Field

from app.auth.csrf import verify_csrf
from app.auth.oidc import OIDCClaims
from app.config import Settings, get_settings
from app.core.audit import redact_params, write_audit_entry
from app.core.vectordb import (
    DEFAULT_TYPE,
    ConnectionStoreError,
    InvalidConnectionError,
    ProbeEnv,
    all_types,
)
from app.core.vectordb.service import DEFAULT_NAME, ConnectionsState, audit_key_action
from app.routers.rag_deps import ConnectionServiceDep, RagAdmin, get_probe_env

router = APIRouter(prefix="/api/v1/rag/connections")


class ConnectionCreateBody(BaseModel):
    name: str
    type: str = DEFAULT_TYPE
    # The type's non-secret inputs, by field name (for Qdrant: `url`).
    fields: dict[str, str] = Field(default_factory=dict)
    api_key: str | None = Field(default=None, repr=False)


class ConnectionUpdateBody(BaseModel):
    fields: dict[str, str]
    # Empty keeps the stored key. A key and `clear_api_key` together are refused.
    api_key: str | None = Field(default=None, repr=False)
    clear_api_key: bool = False
    # The `etag` of the entry as the page loaded it.
    etag: str
    # The jobs that write to this connection are told about a changed address.
    confirm_jobs: bool = False


class ConnectionTestBody(BaseModel):
    # A stored connection, tested with its own key at its own address.
    name: str | None = None
    type: str = DEFAULT_TYPE
    fields: dict[str, str] = Field(default_factory=dict)
    api_key: str | None = Field(default=None, repr=False)


def _user_id(user: OIDCClaims) -> str:
    return user.preferred_username or user.sub


@contextmanager
def _translated() -> Iterator[None]:
    """Answer a refusal of the store with the status code it carries."""
    try:
        yield
    except ConnectionStoreError as exc:
        raise HTTPException(status_code=exc.status, detail=str(exc)) from exc


def state_payload(state: ConnectionsState) -> dict[str, Any]:
    """The listing, as JSON. The page renders from the same object."""
    return {
        **asdict(state),
        "types": [
            {
                "id": connection_type.id,
                "label": connection_type.label,
                "fields": [asdict(spec) for spec in connection_type.fields],
            }
            for connection_type in all_types()
        ],
    }


def _audit(
    settings: Settings,
    user: OIDCClaims,
    action: str,
    target: str,
    params: dict[str, Any] | None = None,
) -> None:
    write_audit_entry(
        settings.papaia_config_dir,
        user=_user_id(user),
        action=action,
        target=target,
        params=redact_params(params) if params is not None else None,
    )


@router.get("")
async def list_connections(user: RagAdmin, service: ConnectionServiceDep) -> dict[str, Any]:
    service.ensure_default()
    return state_payload(service.state())


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_connection(
    request: Request,
    body: ConnectionCreateBody,
    user: RagAdmin,
    settings: Annotated[Settings, Depends(get_settings)],
    service: ConnectionServiceDep,
) -> dict[str, Any]:
    verify_csrf(request)
    with _translated():
        view = service.create(
            name=body.name, type_id=body.type, values=body.fields, api_key=body.api_key
        )
    _audit(
        settings,
        user,
        "rag.connection.create",
        view.name,
        {
            "type": view.type,
            "fields": dict(view.fields),
            # A new connection has no key to keep: it was set, or there is none.
            "key_action": "set" if (body.api_key or "").strip() else "none",
        },
    )
    return asdict(view)


@router.post("/test")
async def test_connection(
    request: Request,
    body: ConnectionTestBody,
    user: RagAdmin,
    settings: Annotated[Settings, Depends(get_settings)],
    service: ConnectionServiceDep,
    env: Annotated[ProbeEnv, Depends(get_probe_env)],
) -> dict[str, Any]:
    """Reach a database with a stored connection, or with what was typed.

    A stored key is only used at the stored address; see `ConnectionService.test`.
    """
    verify_csrf(request)
    with _translated():
        result = await service.test(
            name=body.name,
            type_id=body.type,
            values=body.fields,
            api_key=body.api_key,
            env=env,
        )
    _audit(
        settings,
        user,
        "rag.connection.test",
        body.name or "(new)",
        {"fields": body.fields, "ok": result.ok},
    )
    return {"ok": result.ok, "detail": result.detail, "collections": result.collections}


@router.put("/{name}")
async def update_connection(
    name: str,
    request: Request,
    body: ConnectionUpdateBody,
    user: RagAdmin,
    settings: Annotated[Settings, Depends(get_settings)],
    service: ConnectionServiceDep,
) -> dict[str, Any]:
    verify_csrf(request)
    with _translated():
        view = service.update(
            name,
            values=body.fields,
            api_key=body.api_key,
            clear_api_key=body.clear_api_key,
            etag=body.etag,
            confirm_jobs=body.confirm_jobs,
        )
    _audit(
        settings,
        user,
        "rag.connection.update",
        name,
        {
            "fields": dict(view.fields),
            "key_action": audit_key_action(body.api_key, body.clear_api_key),
            "confirmed_jobs": body.confirm_jobs,
        },
    )
    return asdict(view)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_connection(
    name: str,
    request: Request,
    etag: Annotated[str, Query(min_length=1, max_length=64)],
    user: RagAdmin,
    settings: Annotated[Settings, Depends(get_settings)],
    service: ConnectionServiceDep,
) -> Response:
    verify_csrf(request)
    with _translated():
        service.delete(name, etag=etag)
    _audit(settings, user, "rag.connection.delete", name)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.post("/{name}/reset")
async def reset_connection(
    name: str,
    request: Request,
    user: RagAdmin,
    settings: Annotated[Settings, Depends(get_settings)],
    service: ConnectionServiceDep,
) -> dict[str, Any]:
    """Point the default connection at the integrated Qdrant again, with the stack's key."""
    verify_csrf(request)
    with _translated():
        if name != DEFAULT_NAME:
            raise InvalidConnectionError("Only the default connection can be reset.")
        view = service.reset_default()
    _audit(
        settings,
        user,
        "rag.connection.reset",
        name,
        {"fields": dict(view.fields), "key_action": "replaced"},
    )
    return asdict(view)
