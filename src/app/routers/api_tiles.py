"""REST API — dashboard tile configuration.

The dashboard editor writes through here. Two shapes of the same document are
exposed: the structured one the editor binds to, and the raw YAML the operator
falls back to for bulk edits. Both take the `revision` handed out by the
matching read and refuse a write built on a stale copy, which is what keeps a
save from a forgotten browser tab from silently undoing an edit made over SSH.

Every route is administrator-only. The dashboard itself is not -- so this is
the boundary where the two tiers part company, and the reason the editor is a
separate partial rather than a flag on the gallery.
"""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field

from app.auth.csrf import verify_csrf
from app.auth.deps import AdminUser
from app.config import Settings, get_settings
from app.core.audit import write_audit_entry
from app.core.envfile import load_env_file
from app.core.tiles import (
    TilesConfig,
    TilesFileError,
    check_value,
    config_from_json,
    config_to_json,
    link_placeholder_keys,
    load_tiles,
    load_tiles_raw,
    parse_tiles,
    save_tiles,
    save_tiles_raw,
    tiles_revision,
    validate_config,
)

router = APIRouter(prefix="/api/v1/tiles")

# One dialog resolves one link and one icon; the cap is there so a crafted body
# cannot turn a preview into a workload.
_MAX_RESOLVE_VALUES = 64


class TileBody(BaseModel):
    name: str
    href: str
    description: str = ""
    icon: str | None = None
    visibility: Literal["all", "admin"] = "all"


class GroupBody(BaseModel):
    name: str
    tiles: list[TileBody] = Field(default_factory=list)


class TilesBody(BaseModel):
    revision: str
    version: int = 1
    groups: list[GroupBody] = Field(default_factory=list)


class RawBody(BaseModel):
    revision: str
    yaml: str


class ResolveBody(BaseModel):
    values: list[str] = Field(default_factory=list)


@router.get("")
async def get_tiles(
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """The whole configuration, unfiltered.

    Deliberately not `visible_groups`: the editor has to see restricted tiles
    and groups that are currently empty, both of which the dashboard view drops.
    """
    config = _load(settings)
    return {
        "revision": tiles_revision(settings.papaia_config_dir),
        **config_to_json(config),
        "link_keys": link_placeholder_keys(_env(settings)),
    }


@router.put("")
async def replace_tiles(
    request: Request,
    body: TilesBody,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Replace the whole document.

    Whole-document rather than per-tile, because a tile has no identity beyond
    its position in the list: any addressable route would have to invent one.
    It also makes a save exactly one atomic write, whatever the editor changed.
    """
    verify_csrf(request)
    _require_current(settings, body.revision)

    config = config_from_json(body.model_dump())
    _reject_problems(settings, config)
    save_tiles(settings.papaia_config_dir, config)
    _audit(settings, user.preferred_username, config, source="editor")

    return {
        "revision": tiles_revision(settings.papaia_config_dir),
        **config_to_json(config),
    }


@router.get("/raw")
async def get_tiles_raw(
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """The file as text, for the raw editor."""
    # Read before hashing: the first call seeds the file, and a revision taken
    # ahead of that would describe a file that does not exist yet -- which the
    # editor would then hand back as stale on its first save.
    text = load_tiles_raw(settings.papaia_config_dir)
    return {
        "revision": tiles_revision(settings.papaia_config_dir),
        "yaml": text,
    }


@router.put("/raw")
async def replace_tiles_raw(
    request: Request,
    body: RawBody,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> dict[str, Any]:
    """Replace the file from operator-supplied YAML.

    Parsed and validated before it lands, so the raw editor cannot write a
    document that the dashboard would then refuse to render.
    """
    verify_csrf(request)
    _require_current(settings, body.revision)

    try:
        config = parse_tiles(body.yaml)
    except TilesFileError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc

    _reject_problems(settings, config)
    save_tiles_raw(settings.papaia_config_dir, body.yaml)
    _audit(settings, user.preferred_username, config, source="raw")

    return {
        "revision": tiles_revision(settings.papaia_config_dir),
        **config_to_json(config),
    }


@router.post("/resolve")
async def resolve_links(
    request: Request,
    body: ResolveBody,
    user: AdminUser,
    settings: Annotated[Settings, Depends(get_settings)],
) -> list[dict[str, Any]]:
    """Resolve `{{KEY}}` links for the editor's preview.

    Server-side on purpose. The core .env is the only place these values live
    and it holds every stack secret, so the substitution happens here and only
    the finished string travels back.
    """
    verify_csrf(request)
    env = _env(settings)

    results: list[dict[str, Any]] = []
    for value in body.values[:_MAX_RESOLVE_VALUES]:
        resolved, problem = check_value(value, env)
        results.append(
            {"input": value, "resolved": resolved, "ok": problem is None, "reason": problem}
        )
    return results


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _env(settings: Settings) -> dict[str, str]:
    return load_env_file(Path(settings.papaia_config_dir) / ".env")


def _load(settings: Settings) -> TilesConfig:
    try:
        return load_tiles(settings.papaia_config_dir)
    except TilesFileError as exc:
        # 409 rather than 500: the file is readable, it is the content that is
        # wrong, and the raw editor is where that gets fixed.
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc


def _require_current(settings: Settings, revision: str) -> None:
    current = tiles_revision(settings.papaia_config_dir)
    if revision != current:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="tiles.yaml changed since it was loaded; reload before saving",
        )


def _reject_problems(settings: Settings, config: TilesConfig) -> None:
    problems = validate_config(config, _env(settings))
    if problems:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=[asdict(problem) for problem in problems],
        )


def _audit(settings: Settings, username: str, config: TilesConfig, *, source: str) -> None:
    write_audit_entry(
        settings.papaia_config_dir,
        user=username,
        action="tiles-save",
        target="tiles.yaml",
        params={
            "source": source,
            "groups": len(config.groups),
            "tiles": sum(len(group.tiles) for group in config.groups),
        },
    )
