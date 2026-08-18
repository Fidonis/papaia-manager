"""Server-rendered HTML pages (Jinja2 + HTMX)."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
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
from app.core.inventory import SELF_PROFILE
from app.core.jobs import Job, JobQueue
from app.core.resolve import resolve_catalog_addons
from app.core.services import (
    ServiceHealth,
    StackSnapshot,
    count_by_health,
    load_snapshot,
    overall_health,
    worst,
)
from app.core.snapshots import load_installed, managed_snapshot_path
from app.core.state import (
    AddonStatus,
    compute_status,
    deployment_addons_by_name,
    load_deployment_yaml,
)
from app.core.tiles import TileGroup, load_tiles, visible_groups
from app.templating import templates as _templates

router = APIRouter()


# How long a finished backup keeps its panel on the backup page. The panel has no
# acknowledge endpoint behind it on purpose -- an operator who was on another page
# while it ran still gets the outcome, and it clears itself instead of leaving a
# dismissible strip on a page nobody has open.
_BACKUP_OUTCOME_GRACE = timedelta(minutes=5)


def _queue() -> JobQueue | None:
    from app.main import _job_queue  # noqa: PLC0415

    return _job_queue


def _backup_panel_ctx(queue: JobQueue | None) -> dict[str, Any]:
    """Context for the backup page's status strip.

    An active job of any action, because any of them blocks a backup from
    starting and the strip is what explains the disabled button. Once the queue
    is idle only a *backup* outcome is worth showing here -- an add-on install
    that just finished belongs on the jobs page, not on this one.

    One helper for the page and its partial, so the state rendered on load and
    the state polled two seconds later are produced by the same code.
    """
    job: Job | None = None
    active = False
    if queue is not None:
        job = queue.active_job()
        active = job is not None
        if job is None:
            cutoff = datetime.now(tz=UTC) - _BACKUP_OUTCOME_GRACE
            job = next(
                (
                    j
                    for j in queue.list_jobs()
                    if j.action == "backup" and j.finished_at and j.finished_at > cutoff
                ),
                None,
            )
    return {
        "job": job,
        "job_active": active,
        # Only the tail: the full log is one click away on the job page, and this
        # strip sits above the restore points rather than replacing them.
        "job_log_tail": (
            "\n".join(queue.read_log(job.id).splitlines()[-8:])
            if queue is not None and job is not None and active
            else ""
        ),
    }


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
    """What this deployment is configured to run, against what is up."""
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
    # Rendered into the page rather than fetched after load: the primary action
    # has to come up already disabled when a job is in flight. Discovering that a
    # moment later would show an enabled button first, which is the state this
    # page is being fixed for.
    return _templates.TemplateResponse(
        request,
        "backup.html",
        _ctx(
            request,
            user,
            backup_dir=str(backup_dir) if backup_dir else None,
            backup_dir_reachable=backups.is_reachable(backup_dir),
            **_backup_panel_ctx(_queue()),
        ),
    )


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_page(
    request: Request,
    user: AdminUser,
) -> HTMLResponse:
    """Every job this manager process has run, newest first."""
    return _templates.TemplateResponse(request, "jobs.html", _ctx(request, user))


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
    """The header status chip and its popover, for every authenticated role.

    Deliberately not filtered by visibility. Neither the chip nor the popover
    names a service, only how many are in which state, so a non-admin learns
    that something is wrong without learning what -- which is the point of
    putting it in front of them at all.

    Core and add-ons stay separate rather than being folded into one value: a
    broken add-on out of a customer catalogue would otherwise repaint the stack
    verdict for everyone who is logged in. The chip aggregates the two for its
    one-line summary; the popover keeps them apart.

    `*_running` counts COMPLETED alongside HEALTHY, the same way the services
    page does: a one-shot init container that exited 0 did its job.
    """
    snapshot = await _load_stack_snapshot(settings)
    core_counts = count_by_health(snapshot.core)
    addon_counts = count_by_health(snapshot.addons)
    core_running = core_counts[ServiceHealth.HEALTHY] + core_counts[ServiceHealth.COMPLETED]
    addon_running = addon_counts[ServiceHealth.HEALTHY] + addon_counts[ServiceHealth.COMPLETED]

    # The chip's one-line verdict. `worst()` is reused rather than reimplemented
    # in the template: the severity order it walks is the single place that
    # decides what wins when two things are wrong at once, and a second copy of
    # that order in Jinja would drift.
    core_overall = overall_health(snapshot.core)
    addon_overall = overall_health(snapshot.addons)
    # An empty add-on section reports UNKNOWN, which must not drag the chip down
    # to "status unknown" on a deployment that simply has no add-ons.
    sections = [core_overall] + ([addon_overall] if snapshot.addons else [])

    resp = _templates.TemplateResponse(
        request,
        "partials/service_status_chip.html",
        _ctx(
            request,
            user,
            overall=worst(sections),
            issues=(len(snapshot.core) - core_running) + (len(snapshot.addons) - addon_running),
            core_overall=core_overall,
            core_running=core_running,
            core_total=len(snapshot.core),
            addon_overall=addon_overall,
            addon_running=addon_running,
            addon_total=len(snapshot.addons),
            # Rendered as a local wall-clock time in the popover. Absolute
            # rather than relative on purpose: if the poll dies -- backgrounded
            # tab, manager restarted underneath -- a frozen clock says so,
            # where "a moment ago" would keep claiming to be fresh.
            checked_at=datetime.now(UTC).isoformat(),
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
    snapshot = await _load_stack_snapshot(settings)
    # The tiles count the page as a whole -- an operator wants to know how many
    # modules are in trouble, not how the trouble splits across two sections.
    counts = count_by_health(snapshot.core + snapshot.addons)
    resp = _templates.TemplateResponse(
        request,
        "partials/service_list.html",
        _ctx(
            request,
            user,
            core_modules=snapshot.core,
            addon_modules=snapshot.addons,
            total=len(snapshot.core) + len(snapshot.addons),
            cnt_running=counts[ServiceHealth.HEALTHY] + counts[ServiceHealth.COMPLETED],
            cnt_degraded=counts[ServiceHealth.UNHEALTHY] + counts[ServiceHealth.STARTING],
            cnt_stopped=counts[ServiceHealth.STOPPED],
            cnt_missing=counts[ServiceHealth.MISSING],
            group_count=len(snapshot.groups),
            # Only the selectable groups: the page pushes this into its Alpine
            # scope and prunes the selection against it, so a locked group in
            # here would make `manager` selectable by the back door.
            group_map={
                g.name: list(g.modules) for g in snapshot.groups if g.selectable
            },
            self_profile=SELF_PROFILE,
        ),
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/partials/stack-runner", response_class=HTMLResponse)
async def partial_stack_runner(
    request: Request,
    user: AdminUser,
) -> HTMLResponse:
    """State of the detached stack runner, or nothing at all when there is none.

    Polled from the services page. An unreachable Docker socket renders as empty
    rather than as an error: the page around it is still perfectly informative,
    and the operator has a bigger problem than this strip.
    """
    try:
        status_obj = await runner.find_runner(runner.STACK_KIND)
    except runner.RunnerError:
        status_obj = None
    log = await runner.runner_log(status_obj.name) if status_obj is not None else ""
    resp = _templates.TemplateResponse(
        request,
        "partials/stack_runner.html",
        _ctx(request, user, stack=runner.status_to_dict(status_obj, log, target_key="action")),
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


@router.get("/partials/jobs", response_class=HTMLResponse)
async def partial_jobs(
    request: Request,
    user: AdminUser,
) -> HTMLResponse:
    queue = _queue()
    resp = _templates.TemplateResponse(
        request,
        "partials/job_list.html",
        _ctx(request, user, jobs=queue.list_jobs() if queue else []),
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/partials/nav/job-indicator", response_class=HTMLResponse)
async def partial_nav_job_indicator(
    request: Request,
    user: AdminUser,
) -> HTMLResponse:
    """The dot on the Jobs nav entry -- the only cross-page sign of a running job.

    Rendered from the sidebar of every admin page, which is what makes a backup
    started on one page still visible from another.
    """
    queue = _queue()
    resp = _templates.TemplateResponse(
        request,
        "partials/nav_job_indicator.html",
        _ctx(request, user, active=queue.active_job() if queue else None),
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


@router.get("/partials/jobs/{job_id}", response_class=HTMLResponse)
async def partial_job_status(
    job_id: str,
    request: Request,
    user: AdminUser,
) -> HTMLResponse:
    queue = _queue()
    job = queue.get_job(job_id) if queue else None
    terminal = job is not None and job.status.value in ("succeeded", "failed")
    return _templates.TemplateResponse(
        request,
        "partials/job_status.html",
        _ctx(request, user, job=job, terminal=terminal),
    )


@router.get("/partials/jobs/{job_id}/log", response_class=HTMLResponse)
async def partial_job_log(
    job_id: str,
    request: Request,
    user: AdminUser,
) -> HTMLResponse:
    """The log as text.

    The JSON route this replaces on the log page answers ``{"log": ...}``, and
    htmx swaps a response body verbatim -- the envelope was being rendered along
    with the log. Jinja escapes the text on the way in; it is command output, not
    markup.
    """
    queue = _queue()
    job = queue.get_job(job_id) if queue else None
    terminal = job is not None and job.status.value in ("succeeded", "failed")
    resp = _templates.TemplateResponse(
        request,
        "partials/job_log_text.html",
        _ctx(
            request,
            user,
            job_id=job_id,
            log=queue.read_log(job_id) if queue else "",
            terminal=terminal,
        ),
    )
    resp.headers["Cache-Control"] = "no-store"
    return resp


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


@router.get("/partials/backup/job-status", response_class=HTMLResponse)
async def partial_backup_job_status(
    request: Request,
    user: AdminUser,
) -> HTMLResponse:
    """The running/last-outcome strip above the restore points.

    Also the page's only source of truth for whether the primary action is
    disabled -- the strip announces its state and the header button follows, so
    the two cannot disagree.
    """
    resp = _templates.TemplateResponse(
        request,
        "partials/backup_job_status.html",
        _ctx(request, user, **_backup_panel_ctx(_queue())),
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

async def _load_stack_snapshot(settings: Settings) -> StackSnapshot:
    """Core, add-ons and running projects in one reading.

    Blocking (`docker ps` plus a YAML scan of the Compose files), so off-thread.
    Cheap to call more than once per request: `load_snapshot` caches for five
    seconds, which is what lets the add-on views share this reading instead of
    running a second query.
    """
    return await asyncio.get_running_loop().run_in_executor(
        None,
        load_snapshot,
        settings.papaia_config_dir,
        settings.papaia_workspace_dir,
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
    running = (await _load_stack_snapshot(settings)).running_projects

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
    running = (await _load_stack_snapshot(settings)).running_projects

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
