"""REST API — catalog CRUD and refresh jobs."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, field_validator

from app.auth.csrf import verify_csrf
from app.auth.deps import AdminUser
from app.config import Settings, get_settings
from app.core.catalogs import (
    Catalog,
    load_registry,
    refresh_catalog_clone,
    save_registry,
    validate_catalog_name,
    validate_catalog_url,
    validate_local_path,
    validate_ref,
)
from app.core.jobs import JobContext

router = APIRouter(prefix="/api/v1/catalogs")


class CatalogCreateBody(BaseModel):
    name: str
    type: Literal["git", "local"]
    url: str | None = None
    path: str | None = None
    ref: str = "main"
    enabled: bool = True
    token: str | None = None
    token_env: str | None = None
    username: str = "x-access-token"

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        validate_catalog_name(v)
        return v


class CatalogUpdateBody(BaseModel):
    url: str | None = None
    ref: str | None = None
    enabled: bool | None = None
    token: str | None = None
    path: str | None = None


@router.get("")
async def list_catalogs(
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, Any]]:
    registry = load_registry(settings.papaia_config_dir)
    return [_catalog_summary(c) for c in registry.catalogs]


@router.post("", status_code=status.HTTP_201_CREATED)
async def create_catalog(
    request: Request,
    body: CatalogCreateBody,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    verify_csrf(request)
    if body.type == "git":
        if not body.url:
            raise HTTPException(status_code=400, detail="url is required for git catalogs")
        validate_catalog_url(body.url)
        validate_ref(body.ref)
    else:
        if not body.path:
            raise HTTPException(status_code=400, detail="path is required for local catalogs")
        validate_local_path(body.path, settings.papaia_workspace_dir)

    registry = load_registry(settings.papaia_config_dir)
    if any(c.name == body.name for c in registry.catalogs):
        raise HTTPException(status_code=409, detail=f"catalog {body.name!r} already exists")

    catalog = Catalog(
        name=body.name,
        type=body.type,
        url=body.url,
        ref=body.ref,
        path=body.path,
        enabled=body.enabled,
    )
    registry.catalogs.append(catalog)
    save_registry(settings.papaia_config_dir, registry)
    return _catalog_summary(catalog)


@router.put("/{name}")
async def update_catalog(
    name: str,
    request: Request,
    body: CatalogUpdateBody,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    verify_csrf(request)
    registry = load_registry(settings.papaia_config_dir)
    catalog = next((c for c in registry.catalogs if c.name == name), None)
    if catalog is None:
        raise HTTPException(status_code=404, detail=f"catalog {name!r} not found")

    if body.url is not None:
        validate_catalog_url(body.url)
        catalog.url = body.url
    if body.ref is not None:
        validate_ref(body.ref)
        catalog.ref = body.ref
    if body.enabled is not None:
        catalog.enabled = body.enabled
    if body.path is not None:
        validate_local_path(body.path, settings.papaia_workspace_dir)
        catalog.path = body.path

    save_registry(settings.papaia_config_dir, registry)
    return _catalog_summary(catalog)


@router.delete("/{name}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_catalog(
    name: str,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> None:
    verify_csrf(request)
    registry = load_registry(settings.papaia_config_dir)
    before = len(registry.catalogs)
    registry.catalogs = [c for c in registry.catalogs if c.name != name]
    if len(registry.catalogs) == before:
        raise HTTPException(status_code=404, detail=f"catalog {name!r} not found")
    save_registry(settings.papaia_config_dir, registry)


@router.post("/{name}/refresh", status_code=status.HTTP_202_ACCEPTED)
async def refresh_catalog(
    name: str,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    verify_csrf(request)
    registry = load_registry(settings.papaia_config_dir)
    catalog = next((c for c in registry.catalogs if c.name == name), None)
    if catalog is None:
        raise HTTPException(status_code=404, detail=f"catalog {name!r} not found")

    from app.main import _job_queue  # noqa: PLC0415

    if _job_queue is None:
        raise HTTPException(status_code=503, detail="job queue not initialized")

    _catalog = catalog
    _username = user.preferred_username or user.sub

    async def _callback(ctx: JobContext) -> None:
        if _catalog.type == "local":
            ctx.log("[info] local catalog — no git refresh needed")
            return
        ctx.log(f"[info] refreshing catalog {_catalog.name!r} from {_catalog.url}")
        lines = await refresh_catalog_clone(_catalog, settings.papaia_workspace_dir)
        for line in lines:
            ctx.log(line)
        ctx.log("[info] done")

    job = await _job_queue.enqueue(
        action="catalog:refresh",
        target=name,
        user=_username,
        callback=_callback,
    )
    return {"job_id": job.id, "status": "queued"}


def _catalog_summary(c: Catalog) -> dict[str, Any]:
    d: dict[str, Any] = {
        "name": c.name,
        "type": c.type,
        "enabled": c.enabled,
    }
    if c.type == "git":
        d["url"] = c.url
        d["ref"] = c.ref
        if c.auth:
            d["auth"] = {"token_env": c.auth.token_env, "username": c.auth.username}
    else:
        d["path"] = c.path
    return d
