"""REST API — addon lifecycle verbs."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Any

import yaml
from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel

from app.auth.csrf import verify_csrf
from app.auth.deps import AdminUser
from app.auth.oidc import OIDCClaims
from app.config import Settings, get_settings
from app.core.addon_updates import (
    UpdateError,
    answerable_keys,
    changelog_between,
    core_requirement_message,
    core_supports_updates,
    deployment_entry,
    fetch_plan,
    invalidate_outdated_count,
    next_history,
    previous_version,
    prune_after,
    stage_revision,
    sync_installed_after_rollback,
    sync_installed_after_update,
    write_answers_file,
)
from app.core.audit import write_audit_entry
from app.core.catalogs import (
    catalog_scan_path,
    load_registry,
)
from app.core.components import components_for
from app.core.ctl import CtlError, run_addon_verb
from app.core.envforms import EnvField, build_form, field_to_dict
from app.core.envvalidate import EnvValidationError, coerce_env_values
from app.core.jobs import JobContext, JobQueue
from app.core.resolve import outdated_addons, resolve_catalog_addons, update_available
from app.core.services import load_snapshot
from app.core.snapshots import (
    installed_addon_dir,
    load_installed,
    prune_snapshots,
    record_installed,
    remove_installed,
)
from app.core.state import (
    compute_status,
    deployment_addons_by_name,
    load_deployment_yaml,
)
from app.i18n import gettext

router = APIRouter(prefix="/api/v1/addons")


class InstallBody(BaseModel):
    catalog: str
    env: dict[str, str] = {}
    start: bool = True


class StopBody(BaseModel):
    clean_up: bool = False


class UninstallBody(BaseModel):
    clean_up: bool = False


class RestartBody(BaseModel):
    # Removes the containers between the stop and the start instead of merely
    # stopping them -- a full recreate. Volumes are untouched either way.
    clean_up: bool = False


class UpdateBody(BaseModel):
    # Values for the keys the new version asks about; nothing else is accepted.
    env: dict[str, str] = {}
    # The version the operator reviewed (the `revision` of the plan). If the catalog has moved on
    # the update is refused instead of applying something that was not shown.
    revision: str | None = None
    # A restore point of the add-on is taken first. Turning it off is for an operator who has a
    # backup of their own.
    backup: bool = True


class UpdatePlanBody(BaseModel):
    revision: str | None = None


class RollbackBody(BaseModel):
    # False: the add-on goes back to the old code and keeps today's data. True: the data of the
    # restore point taken before the update is restored too, and what was written since is lost.
    restore_data: bool = False


class SaveConfigBody(BaseModel):
    env: dict[str, str] = {}
    restart: bool = False


def _queue() -> JobQueue:
    from app.main import _job_queue  # noqa: PLC0415

    if _job_queue is None:
        raise HTTPException(status_code=503, detail=gettext("job queue not initialized"))
    return _job_queue


def _require_compose_file(name: str, settings: Settings) -> None:
    """Refuse a teardown that papaia-ctl would aim at the wrong project.

    papaia-ctl runs `docker compose -p <add-on> -f <path>/docker-compose.yml
    --env-file <core>/.env stop|down`. With that file missing Compose does not
    stop at the error: `COMPOSE_PROJECT_NAME` from the core `.env` wins over
    `-p`, and the command takes down the core stack, this manager included.
    `path` is resolved the way `services.addon_projects` does it.
    """
    entry = deployment_addons_by_name(load_deployment_yaml(settings.papaia_config_dir)).get(name)
    if entry is None:
        raise HTTPException(status_code=404, detail=gettext(
            "addon %(name)r is not installed"
        ) % {"name": name})
    path = Path(str(entry.get("path", "")))
    if not path.is_absolute():
        path = Path(settings.papaia_workspace_dir) / path
    if not (path / "docker-compose.yml").is_file():
        raise HTTPException(status_code=409, detail=gettext(
            "docker-compose.yml of addon %(name)r is missing in %(path)s, so its containers "
            "cannot be taken down safely; restore the add-on's files first"
        ) % {"name": name, "path": str(path)})


def _parse_env_file(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            result[key.strip()] = val.strip()
    return result


def _load_core_env(papaia_config_dir: str) -> dict[str, str] | None:
    p = Path(papaia_config_dir) / ".env"
    return _parse_env_file(p.read_text(encoding="utf-8")) if p.exists() else None


def _manifest_version(addon_path: Path) -> str:
    mf = addon_path / "papaia-app.yaml"
    if not mf.exists():
        return "unknown"
    raw: dict[str, Any] = yaml.safe_load(mf.read_text(encoding="utf-8")) or {}
    return str(raw.get("version", "unknown"))


def _addon_summary(
    name: str,
    manifest: dict[str, Any],
    catalog_name: str | None,
    inst: Any,
    deploy_entry: Any,
    running: set[str],
    workspace_dir: str,
    *,
    config_dir: str,
    key: str | None = None,
    shadowed_by: list[str] | None = None,
    is_variant: bool = False,
) -> dict[str, Any]:
    st = compute_status(
        name=name,
        deployment_entry=deploy_entry,
        installed=inst,
        catalog_version=manifest.get("version"),
        running_projects=running,
        workspace_dir=workspace_dir,
    )
    return {
        "key": key or name,
        "name": name,
        "status": st,
        "description": manifest.get("description", ""),
        "catalog": catalog_name or (inst.catalog if inst else None),
        "catalog_version": manifest.get("version"),
        "installed_version": inst.manifest_version if inst else None,
        "shadowed_by": shadowed_by or [],
        "is_variant": is_variant,
        "update_available": update_available(inst, manifest.get("version")),
        "managed": inst.managed if inst else True,
        "components": components_for(config_dir, workspace_dir, name, manifest, inst, deploy_entry),
    }


@router.get("")
async def list_addons(
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, Any]]:
    """Return merged addon list (catalog × deployment × Docker status)."""
    registry = load_registry(settings.papaia_config_dir)
    deployment = load_deployment_yaml(settings.papaia_config_dir)
    installed_map = load_installed(settings.papaia_config_dir)
    running = await _running_projects(settings)

    deployment_addons = deployment_addons_by_name(deployment)

    addons: dict[str, dict[str, Any]] = {}
    seen_names: set[str] = set()

    for resolved in resolve_catalog_addons(
        registry, settings.papaia_workspace_dir, installed_map
    ):
        seen_names.add(resolved.name)
        inst = installed_map.get(resolved.name)
        applies_here = (
            not resolved.is_variant or inst is None or inst.catalog == resolved.catalog
        )
        variant_inst = inst if applies_here else None
        deploy_entry = deployment_addons.get(resolved.name) if applies_here else None
        addons[resolved.key] = _addon_summary(
            resolved.name, resolved.manifest, resolved.catalog, variant_inst, deploy_entry,
            running, settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
            key=resolved.key, shadowed_by=resolved.shadowed_by, is_variant=resolved.is_variant,
        )

    for addon_name, deploy_entry in deployment_addons.items():
        if addon_name in seen_names:
            continue
        inst = installed_map.get(addon_name)
        addons[addon_name] = _addon_summary(
            addon_name, {}, inst.catalog if inst else None, inst, deploy_entry,
            running, settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
        )

    return list(addons.values())


@router.get("/updates")
async def list_updates(
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """The installed add-ons for which a catalog on disk offers a higher version.

    Reads the catalogs as they are; a catalog is refreshed by hand (the catalogs API), never
    by this. Whether the compatibility gate accepts a version is not decided here: that is
    `update-plan`, which asks papaia-ctl."""
    found = outdated_addons(
        load_registry(settings.papaia_config_dir),
        settings.papaia_workspace_dir,
        load_installed(settings.papaia_config_dir),
    )
    return {
        "supported": core_supports_updates(settings.papaia_workspace_dir),
        "updates": [
            {
                "name": u.name,
                "installed": u.installed,
                "available": u.available,
                "catalog": u.catalog,
            }
            for u in found
        ],
    }


@router.get("/{name}")
async def addon_detail(
    name: str,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
    catalog: str | None = None,
) -> dict[str, Any]:
    registry = load_registry(settings.papaia_config_dir)
    deployment = load_deployment_yaml(settings.papaia_config_dir)
    installed_map = load_installed(settings.papaia_config_dir)
    running = await _running_projects(settings)

    matches = [
        r
        for r in resolve_catalog_addons(registry, settings.papaia_workspace_dir, installed_map)
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
    summary = _addon_summary(
        name, manifest, catalog_name, variant_inst, deploy_entry,
        running, settings.papaia_workspace_dir,
        config_dir=settings.papaia_config_dir,
        key=resolved.key if resolved else name,
        shadowed_by=resolved.shadowed_by if resolved else [],
        is_variant=resolved.is_variant if resolved else False,
    )
    return {**summary, "manifest": manifest}


@router.get("/{name}/env-form")
async def env_form(
    name: str,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, Any]]:
    installed_map = load_installed(settings.papaia_config_dir)
    inst = installed_map.get(name)

    if inst:
        addon_path: Path | None = installed_addon_dir(
            deployment_entry(settings.papaia_config_dir, name),
            settings.papaia_workspace_dir,
            inst.catalog,
            name,
        )
    else:
        registry = load_registry(settings.papaia_config_dir)
        addon_path = None
        for catalog in registry.catalogs:
            if not catalog.enabled:
                continue
            clone = catalog_scan_path(catalog, settings.papaia_workspace_dir)
            candidate = clone / name
            if (candidate / "papaia-app.yaml").exists():
                addon_path = candidate
                break

    if addon_path is None or not addon_path.exists():
        return []

    bundle_env: dict[str, str] | None = None
    if inst:
        bundle_env_file = Path(settings.papaia_config_dir) / "addons" / name / ".env"
        if bundle_env_file.exists():
            bundle_env = _parse_env_file(bundle_env_file.read_text(encoding="utf-8"))

    fields = build_form(
        addon_path, bundle_env=bundle_env, core_env=_load_core_env(settings.papaia_config_dir)
    )
    return [field_to_dict(f) for f in fields]


@router.post("/{name}/install", status_code=status.HTTP_202_ACCEPTED)
async def install(
    name: str,
    body: InstallBody,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    verify_csrf(request)
    registry = load_registry(settings.papaia_config_dir)
    catalog = next(
        (c for c in registry.catalogs if c.name == body.catalog and c.enabled),
        None,
    )
    if catalog is None:
        raise HTTPException(
            status_code=404,
            detail=gettext("catalog %(catalog)r not found or disabled") % {"catalog": body.catalog},
        )
    clone = catalog_scan_path(catalog, settings.papaia_workspace_dir)
    if not (clone / name).exists():
        raise HTTPException(
            status_code=404,
            detail=gettext(
                "addon %(name)r not found in catalog %(name2)r"
            ) % {"name": name, "name2": catalog.name},
        )

    queue = _queue()
    _cat = catalog.name
    _start = body.start
    _username = _user_id(user)

    _core_env = _load_core_env(settings.papaia_config_dir)
    _env = (
        _validate_env(build_form(clone / name, core_env=_core_env), dict(body.env))
        if body.env
        else {}
    )

    async def _callback(ctx: JobContext) -> None:
        ctx.log(f"[info] materializing snapshot for {name!r} from {_cat!r}")
        try:
            staged = await stage_revision(
                catalog=catalog, workspace_dir=settings.papaia_workspace_dir, name=name
            )
        except UpdateError as exc:
            raise RuntimeError(str(exc)) from exc
        dest, sha, ver = staged.path, staged.commit, staged.version
        ctx.log(f"[info] snapshot at {dest} (revision {staged.revision})")

        if _env:
            # papaia-ctl treats the config bundle's .env as canonical: seed_addon_env
            # only fills keys not already present there (sticky), and every start
            # copies bundle -> checkout via materialize_addon_env, overwriting
            # anything written to dest/.env directly. Write overrides to the bundle
            # so they survive both seeding and the copy-back on start.
            bundle_env_dir = Path(settings.papaia_config_dir) / "addons" / name
            bundle_env_dir.mkdir(parents=True, exist_ok=True)
            env_file = bundle_env_dir / ".env"
            existing = (
                _parse_env_file(env_file.read_text(encoding="utf-8"))
                if env_file.exists()
                else {}
            )
            existing.update(_env)
            env_file.write_text(
                "\n".join(f"{k}={v}" for k, v in existing.items()) + "\n",
                encoding="utf-8",
            )
            ctx.log(f"[info] wrote {len(_env)} env value(s) to config bundle .env")

        ctx.log(f"[ctl] papaia-ctl addon install {name}")
        gen = await run_addon_verb(
            verb="install",
            name=name,
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
            extra_flags=[f"--path={dest}"],
        )
        async for line in gen:
            ctx.log(line)

        before = load_installed(settings.papaia_config_dir).get(name)
        record_installed(
            settings.papaia_config_dir,
            name=name,
            catalog=_cat,
            commit=sha,
            manifest_version=ver,
            history=next_history(before, sha),
        )
        ctx.log(f"[info] recorded in installed.yaml (version {ver})")
        prune_after(
            settings.papaia_config_dir, settings.papaia_workspace_dir, name=name, catalog=_cat
        )

        if _start:
            ctx.log(f"[ctl] papaia-ctl addon start {name}")
            gen = await run_addon_verb(
                verb="start",
                name=name,
                workspace_dir=settings.papaia_workspace_dir,
                config_dir=settings.papaia_config_dir,
            )
            async for line in gen:
                ctx.log(line)

        write_audit_entry(
            settings.papaia_config_dir,
            user=_username,
            action="install",
            target=name,
            params={"catalog": _cat, "start": _start},
            job_id=ctx.job.id,
        )
        ctx.log("[info] done")

    job = await queue.enqueue(
        action="install",
        target=name,
        user=_username,
        params={"catalog": _cat, "start": _start},
        callback=_callback,
    )
    return {"job_id": job.id, "status": "queued"}


@router.post("/{name}/start", status_code=status.HTTP_202_ACCEPTED)
async def start(
    name: str,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    verify_csrf(request)
    queue = _queue()
    _username = _user_id(user)

    async def _callback(ctx: JobContext) -> None:
        ctx.log(f"[ctl] papaia-ctl addon start {name}")
        gen = await run_addon_verb(
            verb="start",
            name=name,
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
        )
        async for line in gen:
            ctx.log(line)
        write_audit_entry(
            settings.papaia_config_dir,
            user=_username,
            action="start",
            target=name,
            job_id=ctx.job.id,
        )

    job = await queue.enqueue(action="start", target=name, user=_username, callback=_callback)
    return {"job_id": job.id, "status": "queued"}


@router.post("/{name}/stop", status_code=status.HTTP_202_ACCEPTED)
async def stop(
    name: str,
    body: StopBody,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    verify_csrf(request)
    queue = _queue()
    _username = _user_id(user)
    _clean = body.clean_up

    async def _callback(ctx: JobContext) -> None:
        ctx.log(f"[ctl] papaia-ctl addon stop {name}")
        flags = ["--clean-up"] if _clean else []
        gen = await run_addon_verb(
            verb="stop",
            name=name,
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
            extra_flags=flags,
        )
        async for line in gen:
            ctx.log(line)
        write_audit_entry(
            settings.papaia_config_dir,
            user=_username,
            action="stop",
            target=name,
            params={"clean_up": _clean},
            job_id=ctx.job.id,
        )

    job = await queue.enqueue(
        action="stop", target=name, user=_username,
        params={"clean_up": _clean}, callback=_callback,
    )
    return {"job_id": job.id, "status": "queued"}


async def _restart_addon(
    ctx: JobContext, name: str, settings: Settings, *, clean_up: bool = True
) -> None:
    """Stop then start one add-on, logging both into the same job.

    papaia-ctl has no restart verb, so a restart is this composition. `clean_up`
    defaults to true for `save-config`, whose whole point is to pick up a changed
    environment: `docker compose stop` alone would leave the old container -- with
    the old values baked in -- in place. A plain restart passes what the operator
    chose.
    """
    flags = ["--clean-up"] if clean_up else []
    ctx.log(f"[ctl] papaia-ctl addon stop {name}{' --clean-up' if clean_up else ''}")
    gen = await run_addon_verb(
        verb="stop",
        name=name,
        workspace_dir=settings.papaia_workspace_dir,
        config_dir=settings.papaia_config_dir,
        extra_flags=flags,
    )
    async for line in gen:
        ctx.log(line)

    ctx.log(f"[ctl] papaia-ctl addon start {name}")
    gen = await run_addon_verb(
        verb="start",
        name=name,
        workspace_dir=settings.papaia_workspace_dir,
        config_dir=settings.papaia_config_dir,
    )
    async for line in gen:
        ctx.log(line)


@router.post("/{name}/restart", status_code=status.HTTP_202_ACCEPTED)
async def restart(
    name: str,
    body: RestartBody,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    verify_csrf(request)
    queue = _queue()
    _username = _user_id(user)
    _clean = body.clean_up

    async def _callback(ctx: JobContext) -> None:
        await _restart_addon(ctx, name, settings, clean_up=_clean)
        write_audit_entry(
            settings.papaia_config_dir,
            user=_username,
            action="restart",
            target=name,
            params={"clean_up": _clean},
            job_id=ctx.job.id,
        )
        ctx.log("[info] done")

    job = await queue.enqueue(
        action="restart",
        target=name,
        user=_username,
        params={"clean_up": _clean},
        callback=_callback,
    )
    return {"job_id": job.id, "status": "queued"}


@router.post("/{name}/remove", status_code=status.HTTP_202_ACCEPTED)
async def remove(
    name: str,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    verify_csrf(request)
    _require_compose_file(name, settings)
    queue = _queue()
    _username = _user_id(user)

    async def _callback(ctx: JobContext) -> None:
        # Checked again here: an install queued ahead of this job can fail and
        # leave the add-on without its files after the request was accepted.
        _require_compose_file(name, settings)

        # `papaia-ctl addon remove` only deactivates the add-on; its containers
        # keep running, and once it is inactive the page offers neither Stop nor
        # Remove for them. The dialog promises they are gone, so take them down
        # first. Volumes stay (`down` without `-v`), and a failure raises before
        # the add-on is marked inactive. It has to be this order: `remove` drops
        # the add-on's generated overrides, and `down` should see the same
        # compose files as the `up` that created the containers.
        ctx.log(f"[ctl] papaia-ctl addon stop {name} --clean-up")
        gen = await run_addon_verb(
            verb="stop",
            name=name,
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
            extra_flags=["--clean-up"],
        )
        async for line in gen:
            ctx.log(line)

        ctx.log(f"[ctl] papaia-ctl addon remove {name}")
        gen = await run_addon_verb(
            verb="remove",
            name=name,
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
        )
        async for line in gen:
            ctx.log(line)
        write_audit_entry(
            settings.papaia_config_dir,
            user=_username,
            action="remove",
            target=name,
            job_id=ctx.job.id,
        )

    job = await queue.enqueue(action="remove", target=name, user=_username, callback=_callback)
    return {"job_id": job.id, "status": "queued"}


@router.post("/{name}/uninstall", status_code=status.HTTP_202_ACCEPTED)
async def uninstall(
    name: str,
    body: UninstallBody,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    verify_csrf(request)
    queue = _queue()
    _username = _user_id(user)
    _clean = body.clean_up
    _recorded = load_installed(settings.papaia_config_dir).get(name)
    _catalog = _recorded.catalog if _recorded is not None else None

    async def _callback(ctx: JobContext) -> None:
        ctx.log(f"[ctl] papaia-ctl addon uninstall {name}")
        flags = ["--clean-up"] if _clean else []
        gen = await run_addon_verb(
            verb="uninstall",
            name=name,
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
            extra_flags=flags,
        )
        async for line in gen:
            ctx.log(line)
        remove_installed(settings.papaia_config_dir, name)
        ctx.log("[info] removed from installed.yaml")
        if _catalog is not None:
            prune_snapshots(
                settings.papaia_workspace_dir, _catalog, name, keep=[]
            )
            ctx.log("[info] snapshots deleted")
        write_audit_entry(
            settings.papaia_config_dir,
            user=_username,
            action="uninstall",
            target=name,
            params={"clean_up": _clean},
            job_id=ctx.job.id,
        )

    job = await queue.enqueue(
        action="uninstall", target=name, user=_username,
        params={"clean_up": _clean}, callback=_callback,
    )
    return {"job_id": job.id, "status": "queued"}


def _require_core_updates(settings: Settings) -> None:
    """Updating and rolling back are verbs of papaia-ctl; an older core does not have them."""
    if not core_supports_updates(settings.papaia_workspace_dir):
        raise HTTPException(status_code=409, detail=core_requirement_message())


def _updatable(name: str, settings: Settings) -> tuple[Any, dict[str, Any], Any]:
    """The installed record, the registration and the catalog an update of `name` comes from.

    Refuses what papaia-ctl would refuse later, so the operator hears it before anything is
    staged: an add-on this manager did not install, one that is not active, a catalog that is
    gone or disabled."""
    inst = load_installed(settings.papaia_config_dir).get(name)
    if inst is None:
        raise HTTPException(status_code=404, detail=gettext(
            "addon %(name)r is not installed"
        ) % {"name": name})
    if not inst.managed:
        raise HTTPException(status_code=409, detail=gettext(
            "addon %(name)r is not managed by this panel"
        ) % {"name": name})
    entry = deployment_entry(settings.papaia_config_dir, name)
    if entry is None or not entry.get("active", False):
        raise HTTPException(status_code=409, detail=gettext(
            "addon %(name)r is not active; reactivate it before updating"
        ) % {"name": name})
    catalog = next(
        (
            c
            for c in load_registry(settings.papaia_config_dir).catalogs
            if c.name == inst.catalog and c.enabled
        ),
        None,
    )
    if catalog is None:
        raise HTTPException(status_code=409, detail=gettext(
            "catalog %(catalog)r not found or disabled"
        ) % {"catalog": inst.catalog})
    return inst, entry, catalog


async def _stage_and_plan(
    name: str, revision: str | None, settings: Settings
) -> tuple[Any, Any, Any, dict[str, Any]]:
    """Stage the catalog's version of `name` and ask papaia-ctl what updating to it would do."""
    inst, entry, catalog = _updatable(name, settings)
    try:
        staged = await stage_revision(
            catalog=catalog,
            workspace_dir=settings.papaia_workspace_dir,
            name=name,
            revision=revision,
        )
        plan = await fetch_plan(
            name=name,
            candidate=staged.path,
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
        )
    except UpdateError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return inst, entry, staged, plan


@router.post("/{name}/update-plan")
async def update_plan(
    name: str,
    body: UpdatePlanBody,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """What updating `name` to the catalog's version would change, without changing it.

    A POST because it writes: the version is staged to a directory of its own (the one the
    update then uses, so what is reviewed is what is applied) and the older ones are pruned.
    The plan is papaia-ctl's (`addon update --dry-run --json`) with the manager's view of the
    components added."""
    verify_csrf(request)
    _require_core_updates(settings)
    inst, entry, staged, plan = await _stage_and_plan(name, body.revision, settings)
    prune_after(
        settings.papaia_config_dir,
        settings.papaia_workspace_dir,
        name=name,
        catalog=inst.catalog,
        extra=staged.path,
    )
    fields = _addon_fields(name, staged.path, settings)
    asked = answerable_keys(plan)
    current = str((plan.get("current") or {}).get("version", inst.manifest_version))
    candidate = str((plan.get("candidate") or {}).get("version", staged.version))
    return {
        "revision": staged.revision,
        "plan": plan,
        "installed_version": inst.manifest_version,
        "form": [field_to_dict(f) for f in fields if f.key in asked],
        "changelog": [
            {"version": c.version, "date": c.date, "body": c.body}
            for c in changelog_between(staged.path, current, candidate)
        ],
    }


@router.post("/{name}/update", status_code=status.HTTP_202_ACCEPTED)
async def update(
    name: str,
    body: UpdateBody,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    """Move `name` to the catalog's version with `papaia-ctl addon update`.

    The version is staged first and the plan checked here, so a refusal (the compatibility gate,
    a value the new version needs and was not given) is an error of this request and not a failed
    job. The job then runs the update and records the result in installed.yaml."""
    verify_csrf(request)
    _require_core_updates(settings)
    inst, entry, staged, plan = await _stage_and_plan(name, body.revision, settings)

    if plan.get("blocked"):
        reason = (plan.get("compat") or {}).get("reason") or gettext(
            "this core does not accept the new version"
        )
        raise HTTPException(status_code=409, detail=reason)
    if plan.get("in_place"):
        raise HTTPException(status_code=409, detail=gettext(
            "addon %(name)r already runs from this directory"
        ) % {"name": name})

    asked = answerable_keys(plan)
    unknown = sorted(set(body.env) - asked)
    if unknown:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={
                "field": unknown[0],
                "message": gettext("this update does not ask for a value for this key"),
            },
        )
    fields = [f for f in _addon_fields(name, staged.path, settings) if f.key in asked]
    answers = _validate_env(fields, dict(body.env)) if body.env else {}
    missing = [key for key in plan.get("needs_input", []) if not answers.get(key)]
    if missing:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"field": missing[0], "message": gettext("a value is required")},
        )

    queue = _queue()
    _username = _user_id(user)
    _backup = body.backup
    _from = inst.manifest_version
    _params = {
        "catalog": inst.catalog,
        "from": _from,
        "to": staged.version,
        "revision": staged.revision,
        "backup": _backup,
        "answered": sorted(answers),
    }

    async def _callback(ctx: JobContext) -> None:
        answers_file = write_answers_file(answers) if answers else None
        flags = [f"--path={staged.path}", "-y"]
        if not _backup:
            flags.append("--no-backup")
        if answers_file is not None:
            flags.append(f"--answers-file={answers_file}")
        ctx.log(f"[info] updating {name!r} from {_from} to {staged.version} ({staged.revision})")
        ctx.log(f"[ctl] papaia-ctl addon update {name}")
        try:
            gen = await run_addon_verb(
                verb="update",
                name=name,
                workspace_dir=settings.papaia_workspace_dir,
                config_dir=settings.papaia_config_dir,
                extra_flags=flags,
            )
            async for line in gen:
                ctx.log(line)
        finally:
            if answers_file is not None:
                answers_file.unlink(missing_ok=True)
            # The registration may have moved even when the command failed (the new version
            # could not be started), so installed.yaml follows deployment.yaml, not the exit.
            if sync_installed_after_update(
                settings.papaia_config_dir,
                settings.papaia_workspace_dir,
                name=name,
                inst=inst,
                staged=staged,
            ):
                ctx.log(f"[info] recorded in installed.yaml (version {staged.version})")
            prune_after(
                settings.papaia_config_dir,
                settings.papaia_workspace_dir,
                name=name,
                catalog=inst.catalog,
            )
            invalidate_outdated_count()

        write_audit_entry(
            settings.papaia_config_dir,
            user=_username,
            action="update",
            target=name,
            params=_params,
            job_id=ctx.job.id,
        )
        ctx.log("[info] done")

    job = await queue.enqueue(
        action="update", target=name, user=_username, params=_params, callback=_callback,
    )
    return {"job_id": job.id, "status": "queued", "revision": staged.revision}


@router.post("/{name}/rollback", status_code=status.HTTP_202_ACCEPTED)
async def rollback(
    name: str,
    body: RollbackBody,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    """One step back, with `papaia-ctl addon rollback`.

    The code and configuration of the version before the last update are registered again;
    the data is left as it is unless `restore_data` asks for the restore point taken before
    the update, which discards what the add-on wrote since."""
    verify_csrf(request)
    _require_core_updates(settings)
    inst = load_installed(settings.papaia_config_dir).get(name)
    entry = deployment_entry(settings.papaia_config_dir, name)
    if inst is None or entry is None:
        raise HTTPException(status_code=404, detail=gettext(
            "addon %(name)r is not installed"
        ) % {"name": name})
    previous = previous_version(settings.papaia_config_dir, settings.papaia_workspace_dir, entry)
    if previous is None:
        raise HTTPException(status_code=409, detail=gettext(
            "addon %(name)r has no earlier version to go back to"
        ) % {"name": name})
    if not previous.available:
        raise HTTPException(status_code=409, detail=gettext(
            "the files of the earlier version of %(name)r are gone"
        ) % {"name": name})
    if body.restore_data and not previous.restore_point_available:
        raise HTTPException(status_code=409, detail=gettext(
            "the restore point taken before the update no longer exists"
        ))

    queue = _queue()
    _username = _user_id(user)
    _restore = body.restore_data
    _was = installed_addon_dir(entry, settings.papaia_workspace_dir, inst.catalog, name)
    _params = {
        "from": inst.manifest_version,
        "to": previous.version,
        "restore_data": _restore,
        "restore_point": previous.restore_point if _restore else None,
    }

    async def _callback(ctx: JobContext) -> None:
        flags = ["-y"] + (["--restore-data"] if _restore else [])
        ctx.log(f"[ctl] papaia-ctl addon rollback {name}")
        try:
            gen = await run_addon_verb(
                verb="rollback",
                name=name,
                workspace_dir=settings.papaia_workspace_dir,
                config_dir=settings.papaia_config_dir,
                extra_flags=flags,
            )
            async for line in gen:
                ctx.log(line)
        finally:
            if sync_installed_after_rollback(
                settings.papaia_config_dir,
                settings.papaia_workspace_dir,
                name=name,
                inst=inst,
                was=_was,
            ):
                ctx.log("[info] installed.yaml follows the earlier version")
            prune_after(
                settings.papaia_config_dir,
                settings.papaia_workspace_dir,
                name=name,
                catalog=inst.catalog,
            )
            invalidate_outdated_count()
        write_audit_entry(
            settings.papaia_config_dir,
            user=_username,
            action="rollback",
            target=name,
            params=_params,
            job_id=ctx.job.id,
        )
        ctx.log("[info] done")

    job = await queue.enqueue(
        action="rollback", target=name, user=_username, params=_params, callback=_callback,
    )
    return {"job_id": job.id, "status": "queued"}


@router.post("/{name}/save-config", status_code=status.HTTP_202_ACCEPTED)
async def save_config(
    name: str,
    body: SaveConfigBody,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    verify_csrf(request)
    queue = _queue()
    _username = _user_id(user)
    _restart = body.restart

    if body.env:
        _env = _validate_env(
            _addon_fields(name, _resolve_addon_path(name, settings), settings),
            dict(body.env),
        )
    else:
        _env = {}

    async def _callback(ctx: JobContext) -> None:
        if _env:
            bundle_env_dir = Path(settings.papaia_config_dir) / "addons" / name
            bundle_env_dir.mkdir(parents=True, exist_ok=True)
            env_file = bundle_env_dir / ".env"
            existing = (
                _parse_env_file(env_file.read_text(encoding="utf-8"))
                if env_file.exists()
                else {}
            )
            existing.update(_env)
            env_file.write_text(
                "\n".join(f"{k}={v}" for k, v in existing.items()) + "\n",
                encoding="utf-8",
            )
            ctx.log(f"[info] wrote {len(_env)} env value(s) to config bundle .env")

        if _restart:
            await _restart_addon(ctx, name, settings)

        write_audit_entry(
            settings.papaia_config_dir,
            user=_username,
            action="save-config",
            target=name,
            params={"restart": _restart},
            job_id=ctx.job.id,
        )
        ctx.log("[info] done")

    job = await queue.enqueue(
        action="save-config",
        target=name,
        user=_username,
        params={"restart": _restart},
        callback=_callback,
    )
    return {"job_id": job.id, "status": "queued"}


@router.post("/{name}/check")
async def check_compat(
    name: str,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    try:
        gen = await run_addon_verb(
            verb="check",
            name=name,
            workspace_dir=settings.papaia_workspace_dir,
            config_dir=settings.papaia_config_dir,
        )
        lines: list[str] = []
        async for line in gen:
            lines.append(line)
        return {"name": name, "status": "ok", "output": "\n".join(lines)}
    except CtlError as exc:
        return {"name": name, "status": "failed", "reason": str(exc), "exit_code": exc.exit_code}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _resolve_addon_path(name: str, settings: Settings) -> Path | None:
    """Return the best available filesystem path for the named addon."""
    installed_map = load_installed(settings.papaia_config_dir)
    inst = installed_map.get(name)
    if inst:
        p = installed_addon_dir(
            deployment_entry(settings.papaia_config_dir, name),
            settings.papaia_workspace_dir,
            inst.catalog,
            name,
        )
        if p.exists():
            return p
    registry = load_registry(settings.papaia_config_dir)
    for catalog in registry.catalogs:
        if not catalog.enabled:
            continue
        candidate = catalog_scan_path(catalog, settings.papaia_workspace_dir) / name
        if (candidate / "papaia-app.yaml").exists():
            return candidate
    return None


def _addon_fields(name: str, addon_path: Path | None, settings: Settings) -> list[EnvField]:
    """Build EnvField list for validation — returns [] if the path is missing."""
    if addon_path is None or not addon_path.exists():
        return []
    bundle_env_file = Path(settings.papaia_config_dir) / "addons" / name / ".env"
    bundle_env = (
        _parse_env_file(bundle_env_file.read_text(encoding="utf-8"))
        if bundle_env_file.exists()
        else None
    )
    return build_form(
        addon_path, bundle_env=bundle_env, core_env=_load_core_env(settings.papaia_config_dir)
    )


def _validate_env(fields: list[EnvField], env: dict[str, str]) -> dict[str, str]:
    """Validate and coerce operator-supplied env values; raises HTTP 422 on error."""
    try:
        coerced, _ = coerce_env_values(fields, env)
        return coerced
    except EnvValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail={"field": exc.field, "message": exc.message},
        ) from exc


def _user_id(user: OIDCClaims) -> str:
    return user.preferred_username or user.sub


def _current_running(settings: Settings) -> set[str]:
    """Compose projects with at least one running container.

    Reads the shared services snapshot rather than issuing its own `docker ps`,
    so this and the services page can never disagree about whether an add-on is
    up. Blocking, and deliberately called from sync code paths only; the async
    routes go through `_running_projects`.
    """
    try:
        return load_snapshot(
            settings.papaia_config_dir, settings.papaia_workspace_dir
        ).running_projects
    except Exception:  # noqa: BLE001
        return set()


async def _running_projects(settings: Settings) -> set[str]:
    """`_current_running` off the event loop."""
    return await asyncio.get_running_loop().run_in_executor(None, _current_running, settings)
