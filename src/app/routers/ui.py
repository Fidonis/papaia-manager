"""Server-rendered HTML pages (Jinja2 + HTMX)."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth.csrf import get_csrf_token
from app.auth.deps import AdminUser, AnyUser
from app.auth.oidc import OIDCClaims
from app.auth.roles import is_admin
from app.config import Settings, get_settings
from app.core import backups, runner
from app.core.catalogs import catalog_scan_path, load_registry, scan_catalog_addons
from app.core.envfile import load_env_file
from app.core.resolve import resolve_catalog_addons
from app.core.services import (
    ServiceHealth,
    ServiceModule,
    compose_project,
    count_by_health,
    load_modules,
    overall_health,
)
from app.core.snapshots import load_installed, managed_snapshot_path
from app.core.state import (
    AddonStatus,
    compute_status,
    deployment_addons_by_name,
    load_deployment_yaml,
    load_running_compose_projects,
)
from app.core.tiles import TileGroup, load_tiles, visible_groups
from app.templating import templates as _templates

router = APIRouter()


def _ctx(request: Request, user: OIDCClaims, **extra: Any) -> dict[str, Any]:
    # `is_admin` drives which navigation entries render. It is presentation
    # only -- every restricted route enforces its own tier via AdminUser.
    return {
        "request": request,
        "user": user,
        "csrf_token": get_csrf_token(request),
        "is_admin": is_admin(user, get_settings()),
        **extra,
    }


# ---------------------------------------------------------------------------
# Full pages
# ---------------------------------------------------------------------------

@router.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    user: AnyUser,
) -> HTMLResponse:
    """Application tiles -- the landing page for every authenticated role."""
    return _templates.TemplateResponse(request, "dashboard.html", _ctx(request, user))


@router.get("/addons", response_class=HTMLResponse)
async def addons_page(
    request: Request,
    user: AdminUser,
) -> HTMLResponse:
    return _templates.TemplateResponse(request, "addons.html", _ctx(request, user))


@router.get("/addons/{name}", response_class=HTMLResponse)
async def addon_detail(
    name: str,
    request: Request,
    user: AdminUser,
    catalog: str | None = None,
) -> HTMLResponse:
    return _templates.TemplateResponse(
        request, "addon_detail.html", _ctx(request, user, addon_name=name, catalog=catalog)
    )


@router.get("/services", response_class=HTMLResponse)
async def services_page(
    request: Request,
    user: AdminUser,
) -> HTMLResponse:
    """Live status of the core stack's containers, grouped by module."""
    return _templates.TemplateResponse(request, "services.html", _ctx(request, user))


@router.get("/catalogs", response_class=HTMLResponse)
async def catalogs_page(
    request: Request,
    user: AdminUser,
) -> HTMLResponse:
    return _templates.TemplateResponse(request, "catalogs.html", _ctx(request, user))


@router.get("/backup", response_class=HTMLResponse)
async def backup_page(
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """Stack-level operations: backup and restore."""
    backup_dir = backups.resolve_backup_dir(settings.papaia_config_dir)
    return _templates.TemplateResponse(
        request,
        "backup.html",
        _ctx(
            request,
            user,
            backup_dir=str(backup_dir) if backup_dir else None,
            backup_dir_reachable=backups.is_reachable(backup_dir),
        ),
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_log_page(
    job_id: str,
    request: Request,
    user: AdminUser,
) -> HTMLResponse:
    return _templates.TemplateResponse(
        request, "job_log.html", _ctx(request, user, job_id=job_id)
    )


# ---------------------------------------------------------------------------
# HTMX partials
# ---------------------------------------------------------------------------

@router.get("/partials/tiles", response_class=HTMLResponse)
async def partial_tiles(
    request: Request,
    user: AnyUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    groups = await asyncio.get_running_loop().run_in_executor(
        None, _gather_tiles, settings, is_admin(user, settings)
    )
    resp = _templates.TemplateResponse(
        request, "partials/tile_gallery.html", _ctx(request, user, groups=groups)
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/partials/service-status", response_class=HTMLResponse)
async def partial_service_status(
    request: Request,
    user: AnyUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    """The header pill: one aggregate value, for every authenticated role.

    Deliberately not filtered by visibility. The pill carries no service
    names, only the worst state in the stack, so a non-admin learns that
    something is wrong without learning what -- which is the point of putting
    it in front of them at all.
    """
    modules = await _load_service_modules(settings)
    overall = overall_health(modules)
    resp = _templates.TemplateResponse(
        request,
        "partials/service_status_pill.html",
        _ctx(
            request,
            user,
            overall=overall,
            affected=count_by_health(modules)[overall],
        ),
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/partials/services", response_class=HTMLResponse)
async def partial_services(
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    modules = await _load_service_modules(settings)
    counts = count_by_health(modules)
    resp = _templates.TemplateResponse(
        request,
        "partials/service_list.html",
        _ctx(
            request,
            user,
            modules=modules,
            total=len(modules),
            cnt_running=counts[ServiceHealth.HEALTHY] + counts[ServiceHealth.COMPLETED],
            cnt_degraded=counts[ServiceHealth.UNHEALTHY] + counts[ServiceHealth.STARTING],
            cnt_stopped=counts[ServiceHealth.STOPPED],
        ),
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/partials/addons", response_class=HTMLResponse)
async def partial_addons(
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    addons = await _gather_addons(settings)
    resp = _templates.TemplateResponse(
        request, "partials/addon_gallery.html", _ctx(request, user, addons=addons)
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/partials/addons/{name}", response_class=HTMLResponse)
async def partial_addon_detail(
    name: str,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    catalog: str | None = None,
) -> HTMLResponse:
    addon = await _get_addon(name, settings, catalog=catalog)
    resp = _templates.TemplateResponse(
        request, "partials/addon_detail_content.html", _ctx(request, user, addon=addon)
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/partials/jobs/{job_id}", response_class=HTMLResponse)
async def partial_job_status(
    job_id: str,
    request: Request,
    user: AdminUser,
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
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    registry = load_registry(settings.papaia_config_dir)
    installed_map = load_installed(settings.papaia_config_dir)
    # Scanned on every render so the count reflects the catalog directory as it
    # is right now — this is what makes Re-scan meaningful for local catalogs.
    addon_counts = {
        c.name: len(
            scan_catalog_addons(catalog_scan_path(c, settings.papaia_workspace_dir))
        )
        for c in registry.catalogs
    }
    # How many of this catalog's add-ons are shadowed by an identical-version
    # entry from another catalog earlier in registry order (dashboard-hidden).
    shadowed_counts: dict[str, int] = {c.name: 0 for c in registry.catalogs}
    for resolved in resolve_catalog_addons(registry, settings.papaia_workspace_dir, installed_map):
        for shadowed_catalog in resolved.shadowed_by:
            shadowed_counts[shadowed_catalog] = shadowed_counts.get(shadowed_catalog, 0) + 1
    return _templates.TemplateResponse(
        request,
        "partials/catalog_list.html",
        _ctx(
            request,
            user,
            catalogs=registry.catalogs,
            addon_counts=addon_counts,
            shadowed_counts=shadowed_counts,
        ),
    )


@router.get("/partials/backup/restore-points", response_class=HTMLResponse)
async def partial_restore_points(
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> HTMLResponse:
    backup_dir = backups.resolve_backup_dir(settings.papaia_config_dir)
    points = await asyncio.get_running_loop().run_in_executor(
        None, backups.load_restore_points, backup_dir
    )
    resp = _templates.TemplateResponse(
        request,
        "partials/restore_point_list.html",
        _ctx(
            request,
            user,
            restore_points=points,
            backup_dir=str(backup_dir) if backup_dir else None,
            backup_dir_reachable=backups.is_reachable(backup_dir),
        ),
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/partials/backup/restore-status", response_class=HTMLResponse)
async def partial_restore_status(
    request: Request,
    user: AdminUser,
) -> HTMLResponse:
    """Polled while a restore runs, and once more after the manager is back.

    A Docker error is rendered as the absence of a runner plus a message: the
    manager container is recreated during a restore, so a transient failure to
    reach the daemon is an expected state here, not a fault.
    """
    error = ""
    try:
        status_obj = await runner.find_runner()
    except runner.RunnerError as exc:
        status_obj, error = None, str(exc)
    log = await runner.runner_log(status_obj.name) if status_obj is not None else ""
    resp = _templates.TemplateResponse(
        request,
        "partials/restore_status.html",
        _ctx(request, user, restore=status_obj, restore_log=log, restore_error=error),
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------------------------------------------------------------------------
# Legacy paths -- the section was called "Maintenance" up to 0.2.0
# ---------------------------------------------------------------------------
#
# The page redirect is for bookmarks. The two partial redirects matter for one
# specific case: `partials/restore_status.html` renders its polling path into
# the markup and keeps polling across the manager's own restart. Upgrading the
# manager while a restore is in flight would otherwise leave that open page
# polling a path that no longer exists, stuck on "reconnecting" until someone
# reloads by hand. Droppable once 0.2.0 is out of circulation.

@router.get("/maintenance", include_in_schema=False)
async def legacy_maintenance_page() -> RedirectResponse:
    return RedirectResponse("/backup", status_code=308)


@router.get("/partials/maintenance/restore-points", include_in_schema=False)
async def legacy_partial_restore_points() -> RedirectResponse:
    return RedirectResponse("/partials/backup/restore-points", status_code=308)


@router.get("/partials/maintenance/restore-status", include_in_schema=False)
async def legacy_partial_restore_status() -> RedirectResponse:
    return RedirectResponse("/partials/backup/restore-status", status_code=308)


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

async def _load_service_modules(settings: Settings) -> list[ServiceModule]:
    """Core-stack modules from Docker. Blocking `docker ps`, so off-thread."""
    return await asyncio.get_running_loop().run_in_executor(
        None, load_modules, compose_project(settings.papaia_config_dir)
    )


def _gather_tiles(settings: Settings, is_admin_user: bool) -> list[TileGroup]:
    """Load the dashboard tiles a caller may see. Synchronous file I/O."""
    config = load_tiles(settings.papaia_config_dir)
    core_env = load_env_file(Path(settings.papaia_config_dir) / ".env")
    return visible_groups(config, is_admin=is_admin_user, env=core_env)


async def _gather_addons(settings: Settings) -> list[dict[str, Any]]:
    registry = load_registry(settings.papaia_config_dir)
    deployment = load_deployment_yaml(settings.papaia_config_dir)
    installed_map = load_installed(settings.papaia_config_dir)
    running = await asyncio.get_running_loop().run_in_executor(
        None, load_running_compose_projects
    )

    deployment_addons = deployment_addons_by_name(deployment)

    addons: dict[str, dict[str, Any]] = {}
    seen_names: set[str] = set()
    for resolved in resolve_catalog_addons(
        registry, settings.papaia_workspace_dir, installed_map
    ):
        seen_names.add(resolved.name)
        inst = installed_map.get(resolved.name)
        # installed.yaml/deployment.yaml are keyed by name only; a version
        # variant that isn't the installed catalog carries no install state.
        applies_here = (
            not resolved.is_variant or inst is None or inst.catalog == resolved.catalog
        )
        variant_inst = inst if applies_here else None
        deploy_entry = deployment_addons.get(resolved.name) if applies_here else None

        st = compute_status(
            name=resolved.name,
            deployment_entry=deploy_entry,
            installed=variant_inst,
            catalog_version=resolved.manifest.get("version"),
            running_projects=running,
            workspace_dir=settings.papaia_workspace_dir,
        )
        addons[resolved.key] = {
            "key": resolved.key,
            "name": resolved.name,
            "status": st,
            "description": resolved.manifest.get("description", ""),
            "catalog": resolved.catalog,
            "catalog_version": resolved.manifest.get("version"),
            "installed_version": variant_inst.manifest_version if variant_inst else None,
            "shadowed_by": resolved.shadowed_by,
            "is_variant": resolved.is_variant,
            "update_available": (
                variant_inst is not None
                and variant_inst.managed
                and resolved.manifest.get("version") is not None
                and variant_inst.manifest_version != resolved.manifest.get("version")
            ),
            "managed": variant_inst.managed if variant_inst else True,
            "env_fields": _build_env_fields(
                resolved.name,
                managed_snapshot_path(
                    settings.papaia_workspace_dir, variant_inst.catalog, resolved.name
                )
                if variant_inst
                else resolved.clone / resolved.name,
                settings,
            )
            if st in (AddonStatus.AVAILABLE, AddonStatus.INACTIVE)
            else [],
        }

    for addon_name, deploy_entry in deployment_addons.items():
        if addon_name in seen_names:
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
            "key": addon_name,
            "name": addon_name,
            "status": st,
            "description": "",
            "catalog": inst.catalog if inst else None,
            "catalog_version": None,
            "installed_version": inst.manifest_version if inst else None,
            "shadowed_by": [],
            "is_variant": False,
            "update_available": False,
            "managed": inst.managed if inst else False,
            "env_fields": _build_env_fields(
                addon_name,
                managed_snapshot_path(settings.papaia_workspace_dir, inst.catalog, addon_name)
                if inst
                else None,
                settings,
            )
            if st == AddonStatus.INACTIVE
            else [],
        }

    return list(addons.values())


async def _get_addon(
    name: str, settings: Settings, catalog: str | None = None
) -> dict[str, Any]:
    registry = load_registry(settings.papaia_config_dir)
    deployment = load_deployment_yaml(settings.papaia_config_dir)
    installed_map = load_installed(settings.papaia_config_dir)
    running = await asyncio.get_running_loop().run_in_executor(
        None, load_running_compose_projects
    )

    matches = [
        r
        for r in resolve_catalog_addons(
            registry, settings.papaia_workspace_dir, installed_map
        )
        if r.name == name
    ]
    resolved = None
    if catalog is not None:
        resolved = next((r for r in matches if r.catalog == catalog), None)
    if resolved is None:
        resolved = next((r for r in matches if not r.is_variant), None)
        if resolved is None and matches:
            resolved = matches[0]

    manifest: dict[str, Any] = resolved.manifest if resolved else {}
    catalog_name = resolved.catalog if resolved else None
    catalog_addon_dir: Path | None = (resolved.clone / name) if resolved else None

    inst = installed_map.get(name)
    applies_here = (
        resolved is None
        or not resolved.is_variant
        or inst is None
        or inst.catalog == resolved.catalog
    )
    variant_inst = inst if applies_here else None
    deploy_entry = (
        deployment_addons_by_name(deployment).get(name) if applies_here else None
    )
    st = compute_status(
        name=name,
        deployment_entry=deploy_entry,
        installed=variant_inst,
        catalog_version=manifest.get("version"),
        running_projects=running,
        workspace_dir=settings.papaia_workspace_dir,
    )

    addon_path = None
    if variant_inst:
        addon_path = managed_snapshot_path(
            settings.papaia_workspace_dir, variant_inst.catalog, name
        )
    elif catalog_addon_dir:
        addon_path = catalog_addon_dir

    env_fields = _build_env_fields(name, addon_path, settings)

    return {
        "key": resolved.key if resolved else name,
        "name": name,
        "status": st,
        "description": manifest.get("description", ""),
        "catalog": catalog_name or (inst.catalog if inst else None),
        "catalog_version": manifest.get("version"),
        "installed_version": variant_inst.manifest_version if variant_inst else None,
        "shadowed_by": resolved.shadowed_by if resolved else [],
        "is_variant": resolved.is_variant if resolved else False,
        "update_available": (
            variant_inst is not None
            and variant_inst.managed
            and manifest.get("version") is not None
            and variant_inst.manifest_version != manifest.get("version")
        ),
        "managed": variant_inst.managed if variant_inst else True,
        "manifest": manifest,
        "env_fields": env_fields,
    }


def _build_env_fields(
    name: str, addon_path: Path | None, settings: Settings
) -> list[dict[str, Any]]:
    if addon_path is None or not addon_path.exists():
        return []
    from app.core.envforms import build_form, field_to_dict  # noqa: PLC0415

    # build_form distinguishes "file absent" (None) from "file empty" ({}),
    # so keep the None when the bundle or core env has not been written yet.
    bundle_env_file = Path(settings.papaia_config_dir) / "addons" / name / ".env"
    bundle_env = load_env_file(bundle_env_file) if bundle_env_file.exists() else None
    core_env_file = Path(settings.papaia_config_dir) / ".env"
    core_env = load_env_file(core_env_file) if core_env_file.exists() else None
    fields = build_form(addon_path, bundle_env=bundle_env, core_env=core_env)
    return [field_to_dict(f) for f in fields]
