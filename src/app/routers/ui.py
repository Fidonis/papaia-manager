"""Server-rendered HTML pages (Jinja2 + HTMX)."""
from __future__ import annotations

import asyncio
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates

from app.auth.csrf import get_csrf_token
from app.auth.deps import CurrentUser
from app.auth.oidc import OIDCClaims
from app.config import Settings, get_settings
from app.core.catalogs import load_registry, scan_catalog_addons
from app.core.snapshots import catalog_clone_path, load_installed, managed_snapshot_path
from app.core.state import (
    compute_status,
    deployment_addons_by_name,
    load_deployment_yaml,
    load_running_compose_projects,
)

router = APIRouter()

_templates = Jinja2Templates(directory="app/templates")


def _ctx(request: Request, user: OIDCClaims, **extra: Any) -> dict[str, Any]:
    return {
        "request": request,
        "user": user,
        "csrf_token": get_csrf_token(request),
        **extra,
    }


# ---------------------------------------------------------------------------
# Full pages
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: CurrentUser,
) -> HTMLResponse:
    return _templates.TemplateResponse(request, "dashboard.html", _ctx(request, user))


@router.get("/addons/{name}", response_class=HTMLResponse)
async def addon_detail(
    name: str,
    request: Request,
    user: CurrentUser,
) -> HTMLResponse:
    return _templates.TemplateResponse(
        request, "addon_detail.html", _ctx(request, user, addon_name=name)
    )


@router.get("/catalogs", response_class=HTMLResponse)
async def catalogs_page(
    request: Request,
    user: CurrentUser,
) -> HTMLResponse:
    return _templates.TemplateResponse(request, "catalogs.html", _ctx(request, user))


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_log_page(
    job_id: str,
    request: Request,
    user: CurrentUser,
) -> HTMLResponse:
    return _templates.TemplateResponse(
        request, "job_log.html", _ctx(request, user, job_id=job_id)
    )


# ---------------------------------------------------------------------------
# HTMX partials
# ---------------------------------------------------------------------------

@router.get("/partials/addons", response_class=HTMLResponse)
async def partial_addons(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    addons = await _gather_addons(settings)
    return _templates.TemplateResponse(
        request, "partials/addon_gallery.html", _ctx(request, user, addons=addons)
    )


@router.get("/partials/addons/{name}", response_class=HTMLResponse)
async def partial_addon_detail(
    name: str,
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    addon = await _get_addon(name, settings)
    return _templates.TemplateResponse(
        request, "partials/addon_detail_content.html", _ctx(request, user, addon=addon)
    )


@router.get("/partials/jobs/{job_id}", response_class=HTMLResponse)
async def partial_job_status(
    job_id: str,
    request: Request,
    user: CurrentUser,
) -> HTMLResponse:
    from app.main import _job_queue  # noqa: PLC0415

    job = _job_queue.get_job(job_id) if _job_queue else None
    terminal = job is not None and job.status.value in ("succeeded", "failed")
    return _templates.TemplateResponse(
        request,
        "partials/job_status.html",
        _ctx(request, user, job=job, terminal=terminal),
    )


@router.get("/partials/catalogs", response_class=HTMLResponse)
async def partial_catalogs(
    request: Request,
    user: CurrentUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    registry = load_registry(settings.papaia_config_dir)
    return _templates.TemplateResponse(
        request,
        "partials/catalog_list.html",
        _ctx(request, user, catalogs=registry.catalogs),
    )


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

async def _gather_addons(settings: Settings) -> list[dict[str, Any]]:
    registry = load_registry(settings.papaia_config_dir)
    deployment = load_deployment_yaml(settings.papaia_config_dir)
    installed_map = load_installed(settings.papaia_config_dir)
    running = await asyncio.get_running_loop().run_in_executor(
        None, load_running_compose_projects
    )

    deployment_addons = deployment_addons_by_name(deployment)

    addons: dict[str, dict[str, Any]] = {}
    for catalog in registry.catalogs:
        if not catalog.enabled:
            continue
        clone = catalog_clone_path(settings.papaia_workspace_dir, catalog.name)
        for addon_name, manifest in scan_catalog_addons(clone):
            if addon_name in addons:
                continue
            deploy_entry = deployment_addons.get(addon_name)
            inst = installed_map.get(addon_name)
            st = compute_status(
                name=addon_name,
                deployment_entry=deploy_entry,
                installed=inst,
                catalog_version=manifest.get("version"),
                running_projects=running,
                workspace_dir=settings.papaia_workspace_dir,
            )
            addons[addon_name] = {
                "name": addon_name,
                "status": st,
                "description": manifest.get("description", ""),
                "catalog": catalog.name,
                "catalog_version": manifest.get("version"),
                "installed_version": inst.manifest_version if inst else None,
                "update_available": (
                    inst is not None
                    and inst.managed
                    and manifest.get("version") is not None
                    and inst.manifest_version != manifest.get("version")
                ),
                "managed": inst.managed if inst else True,
            }

    for addon_name, deploy_entry in deployment_addons.items():
        if addon_name in addons:
            continue
        inst = installed_map.get(addon_name)
        st = compute_status(
            name=addon_name,
            deployment_entry=deploy_entry,
            installed=inst,
            catalog_version=None,
            running_projects=running,
            workspace_dir=settings.papaia_workspace_dir,
        )
        addons[addon_name] = {
            "name": addon_name,
            "status": st,
            "description": "",
            "catalog": inst.catalog if inst else None,
            "catalog_version": None,
            "installed_version": inst.manifest_version if inst else None,
            "update_available": False,
            "managed": inst.managed if inst else False,
        }

    return list(addons.values())


async def _get_addon(name: str, settings: Settings) -> dict[str, Any]:
    registry = load_registry(settings.papaia_config_dir)
    deployment = load_deployment_yaml(settings.papaia_config_dir)
    installed_map = load_installed(settings.papaia_config_dir)
    running = await asyncio.get_running_loop().run_in_executor(
        None, load_running_compose_projects
    )

    manifest: dict[str, Any] = {}
    catalog_name: str | None = None
    for catalog in registry.catalogs:
        if not catalog.enabled:
            continue
        clone = catalog_clone_path(settings.papaia_workspace_dir, catalog.name)
        addon_dir = clone / name
        mf = addon_dir / "papaia-app.yaml"
        if addon_dir.exists() and mf.exists():
            manifest = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
            catalog_name = catalog.name
            break

    inst = installed_map.get(name)
    deploy_entry = deployment_addons_by_name(deployment).get(name)
    st = compute_status(
        name=name,
        deployment_entry=deploy_entry,
        installed=inst,
        catalog_version=manifest.get("version"),
        running_projects=running,
        workspace_dir=settings.papaia_workspace_dir,
    )

    addon_path = None
    if inst:
        addon_path = managed_snapshot_path(settings.papaia_workspace_dir, inst.catalog, name)
    elif catalog_name:
        addon_path = catalog_clone_path(settings.papaia_workspace_dir, catalog_name) / name

    env_fields: list[dict[str, Any]] = []
    if addon_path and addon_path.exists():
        from app.core.envforms import build_form  # noqa: PLC0415

        bundle_env: dict[str, str] | None = None
        env_file = addon_path / ".env"
        if env_file.exists():
            bundle_env = _quick_parse_env(env_file.read_text(encoding="utf-8"))
        fields = build_form(addon_path, bundle_env=bundle_env)
        env_fields = [
            {
                "key": f.key,
                "label": f.label,
                "default": f.default,
                "required": f.required,
                "is_secret": f.is_secret,
                "current_set": f.current_set,
                "hint": f.hint,
                "auto_handled": f.auto_handled,
            }
            for f in fields
        ]

    return {
        "name": name,
        "status": st,
        "description": manifest.get("description", ""),
        "catalog": catalog_name or (inst.catalog if inst else None),
        "catalog_version": manifest.get("version"),
        "installed_version": inst.manifest_version if inst else None,
        "update_available": (
            inst is not None
            and inst.managed
            and manifest.get("version") is not None
            and inst.manifest_version != manifest.get("version")
        ),
        "managed": inst.managed if inst else True,
        "manifest": manifest,
        "env_fields": env_fields,
    }


def _quick_parse_env(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        result[key.strip()] = val.strip()
    return result
