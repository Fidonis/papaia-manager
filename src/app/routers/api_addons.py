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
from app.core.audit import write_audit_entry
from app.core.catalogs import (
    catalog_scan_path,
    load_registry,
    materialize_local_addon,
)
from app.core.ctl import CtlError, run_addon_verb
from app.core.envforms import EnvField, build_form, field_to_dict
from app.core.envvalidate import EnvValidationError, coerce_env_values
from app.core.jobs import JobContext, JobQueue
from app.core.resolve import resolve_catalog_addons
from app.core.services import load_snapshot
from app.core.snapshots import (
    catalog_clone_path,
    load_installed,
    managed_snapshot_path,
    materialize_snapshot,
    record_installed,
    remove_installed,
)
from app.core.state import (
    compute_status,
    deployment_addons_by_name,
    load_deployment_yaml,
)

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


class SaveConfigBody(BaseModel):
    env: dict[str, str] = {}
    restart: bool = False


def _queue() -> JobQueue:
    from app.main import _job_queue  # noqa: PLC0415

    if _job_queue is None:
        raise HTTPException(status_code=503, detail="job queue not initialized")
    return _job_queue


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
        "update_available": (
            inst is not None
            and inst.managed
            and manifest.get("version") is not None
            and inst.manifest_version != manifest.get("version")
        ),
        "managed": inst.managed if inst else True,
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
            key=resolved.key, shadowed_by=resolved.shadowed_by, is_variant=resolved.is_variant,
        )

    for addon_name, deploy_entry in deployment_addons.items():
        if addon_name in seen_names:
            continue
        inst = installed_map.get(addon_name)
        addons[addon_name] = _addon_summary(
            addon_name, {}, inst.catalog if inst else None, inst, deploy_entry,
            running, settings.papaia_workspace_dir,
        )

    return list(addons.values())


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
        addon_path: Path | None = managed_snapshot_path(
            settings.papaia_workspace_dir, inst.catalog, name
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
            detail=f"catalog {body.catalog!r} not found or disabled",
        )
    clone = catalog_scan_path(catalog, settings.papaia_workspace_dir)
    if not (clone / name).exists():
        raise HTTPException(
            status_code=404,
            detail=f"addon {name!r} not found in catalog {catalog.name!r}",
        )

    queue = _queue()
    _cat = catalog.name
    _is_local = catalog.type == "local"
    _src = clone / name
    _start = body.start
    _username = _user_id(user)

    _core_env = _load_core_env(settings.papaia_config_dir)
    _env = (
        _validate_env(build_form(clone / name, core_env=_core_env), dict(body.env))
        if body.env
        else {}
    )

    async def _callback(ctx: JobContext) -> None:
        dest = managed_snapshot_path(settings.papaia_workspace_dir, _cat, name)
        ctx.log(f"[info] materializing snapshot for {name!r} from {_cat!r}")
        if _is_local:
            sha = await materialize_local_addon(_src, dest)
        else:
            sha = await materialize_snapshot(
                catalog_clone=catalog_clone_path(settings.papaia_workspace_dir, _cat),
                addon_subdir=name,
                dest=dest,
            )
        ctx.log(f"[info] snapshot at {dest} (revision {sha[:12]})")

        ver = _manifest_version(dest)

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

        record_installed(
            settings.papaia_config_dir,
            name=name,
            catalog=_cat,
            commit=sha,
            manifest_version=ver,
        )
        ctx.log(f"[info] recorded in installed.yaml (version {ver})")

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
    defaults to true because the callers that restart do it to pick up changed
    configuration, and `docker compose stop` alone would leave the old container
    -- with the old environment baked in -- in place.
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
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    verify_csrf(request)
    queue = _queue()
    _username = _user_id(user)

    async def _callback(ctx: JobContext) -> None:
        await _restart_addon(ctx, name, settings)
        write_audit_entry(
            settings.papaia_config_dir,
            user=_username,
            action="restart",
            target=name,
            job_id=ctx.job.id,
        )
        ctx.log("[info] done")

    job = await queue.enqueue(
        action="restart", target=name, user=_username, callback=_callback
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
    queue = _queue()
    _username = _user_id(user)

    async def _callback(ctx: JobContext) -> None:
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


@router.post("/{name}/update", status_code=status.HTTP_202_ACCEPTED)
async def update(
    name: str,
    body: UpdateBody,
    request: Request,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, str]:
    verify_csrf(request)
    installed_map = load_installed(settings.papaia_config_dir)
    inst = installed_map.get(name)
    if inst is None:
        raise HTTPException(status_code=404, detail=f"addon {name!r} is not installed")

    queue = _queue()
    _username = _user_id(user)
    _cat = inst.catalog
    _was_running = name in _current_running(settings)

    # The catalog may since have been removed from the registry. Git catalogs
    # keep working off their workspace clone in that case, so only switch to
    # the local path when the registry still says the catalog is local.
    _catalog = next(
        (c for c in load_registry(settings.papaia_config_dir).catalogs if c.name == _cat),
        None,
    )
    _is_local = _catalog is not None and _catalog.type == "local"
    _src = (
        catalog_scan_path(_catalog, settings.papaia_workspace_dir) / name
        if _catalog is not None
        else None
    )

    if body.env:
        _cur_path = managed_snapshot_path(settings.papaia_workspace_dir, _cat, name)
        _env = _validate_env(_addon_fields(name, _cur_path, settings), dict(body.env))
    else:
        _env = {}

    async def _callback(ctx: JobContext) -> None:
        dest = managed_snapshot_path(settings.papaia_workspace_dir, _cat, name)
        ctx.log(f"[info] updating snapshot for {name!r} from {_cat!r}")
        if _is_local and _src is not None:
            sha = await materialize_local_addon(_src, dest)
        else:
            sha = await materialize_snapshot(
                catalog_clone=catalog_clone_path(settings.papaia_workspace_dir, _cat),
                addon_subdir=name,
                dest=dest,
            )
        ctx.log(f"[info] snapshot at {dest} (revision {sha[:12]})")

        ver = _manifest_version(dest)

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

        record_installed(
            settings.papaia_config_dir,
            name=name,
            catalog=_cat,
            commit=sha,
            manifest_version=ver,
        )
        ctx.log(f"[info] updated installed.yaml to version {ver}")

        if _was_running:
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
            action="update",
            target=name,
            params={"catalog": _cat},
            job_id=ctx.job.id,
        )
        ctx.log("[info] done")

    job = await queue.enqueue(
        action="update", target=name, user=_username,
        params={"catalog": _cat}, callback=_callback,
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
        p = managed_snapshot_path(settings.papaia_workspace_dir, inst.catalog, name)
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
