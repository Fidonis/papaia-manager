"""REST API — addon lifecycle verbs."""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel

from app.auth.csrf import verify_csrf
from app.auth.deps import CurrentUser
from app.config import Settings, get_settings

router = APIRouter(prefix="/api/v1/addons")


class InstallBody(BaseModel):
    catalog: str
    env: dict[str, str] = {}
    start: bool = True


class StopBody(BaseModel):
    clean_up: bool = False


class UninstallBody(BaseModel):
    clean_up: bool = False


class UpdateBody(BaseModel):
    env: dict[str, str] = {}


@router.get("")
async def list_addons(
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, Any]]:
    """Return merged addon list (catalog × deployment × Docker status)."""
    return []


@router.get("/{name}")
async def addon_detail(
    name: str,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    return {"name": name, "status": "unknown"}


@router.get("/{name}/env-form")
async def env_form(
    name: str,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, Any]]:
    return []


@router.post("/{name}/install", status_code=status.HTTP_202_ACCEPTED)
async def install(
    name: str,
    body: InstallBody,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    verify_csrf(request)
    return {"job_id": "not-implemented-yet", "status": "queued"}


@router.post("/{name}/start", status_code=status.HTTP_202_ACCEPTED)
async def start(
    name: str,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    verify_csrf(request)
    return {"job_id": "not-implemented-yet", "status": "queued"}


@router.post("/{name}/stop", status_code=status.HTTP_202_ACCEPTED)
async def stop(
    name: str,
    body: StopBody,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    verify_csrf(request)
    return {"job_id": "not-implemented-yet", "status": "queued"}


@router.post("/{name}/remove", status_code=status.HTTP_202_ACCEPTED)
async def remove(
    name: str,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    verify_csrf(request)
    return {"job_id": "not-implemented-yet", "status": "queued"}


@router.post("/{name}/uninstall", status_code=status.HTTP_202_ACCEPTED)
async def uninstall(
    name: str,
    body: UninstallBody,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    verify_csrf(request)
    return {"job_id": "not-implemented-yet", "status": "queued"}


@router.post("/{name}/update", status_code=status.HTTP_202_ACCEPTED)
async def update(
    name: str,
    body: UpdateBody,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    verify_csrf(request)
    return {"job_id": "not-implemented-yet", "status": "queued"}


@router.post("/{name}/check")
async def check_compat(
    name: str,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    return {"name": name, "status": "unknown", "reason": "not implemented"}
