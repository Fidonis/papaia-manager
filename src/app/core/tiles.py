"""Dashboard tiles: tiles.yaml CRUD, placeholder resolution and visibility.

The dashboard is operator-configured through a YAML file under
`$PAPAIA_CONFIG_DIR/manager/`, the same place and the same load/seed/atomic-save
shape the catalog registry uses. Tiles are plain links -- there is no lifecycle
attached to them -- so this module owns no state beyond the file itself.

Two rules here are load-bearing for access control:

* Visibility filtering happens server-side, in `visible_groups`. A tile a
  caller may not see is absent from the response, not hidden by CSS.
* Both filtering and link validation fail closed. An unparseable visibility
  value restricts the tile, and a link that does not resolve to an http(s) or
  site-relative target is dropped rather than rendered.
"""
from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, get_args

import yaml

logger = logging.getLogger(__name__)

Visibility = Literal["all", "admin"]

_VISIBILITIES: frozenset[str] = frozenset(get_args(Visibility))

# `{{KEY}}` with optional inner whitespace. Keys are env-var shaped, which
# keeps the pattern from matching Jinja or JavaScript braces by accident.
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

# Schemes a rendered tile link may use. Anything else -- most importantly
# `javascript:` and `data:` -- is dropped, so a malformed or hostile config
# file cannot turn a dashboard link into script execution in a user's browser.
_SAFE_HREF_RE = re.compile(r"^(https?://|/)")


@dataclass
class Tile:
    """A single dashboard link."""

    name: str
    href: str
    description: str = ""
    # Optional operator-hosted image URL. Absent means the UI renders a
    # lettered badge, matching how add-on cards already look.
    icon: str | None = None
    visibility: Visibility = "all"


@dataclass
class TileGroup:
    """A named section of the dashboard."""

    name: str
    tiles: list[Tile] = field(default_factory=list)


@dataclass
class TilesConfig:
    version: int = 1
    groups: list[TileGroup] = field(default_factory=list)


# Seeded on first start so a fresh deployment has a populated dashboard.
# Mirrors the applications the stack ships today; infrastructure endpoints are
# restricted to administrators.
_DEFAULT_TILES: dict[str, Any] = {
    "version": 1,
    "groups": [
        {
            "name": "AI & Automation",
            "tiles": [
                {
                    "name": "LibreChat",
                    "href": "{{PAPAIA_HOST}}:8000",
                    "description": "ChatGPT compatible, self-hosted AI chatbot",
                    "visibility": "all",
                },
                {
                    "name": "LiteLLM",
                    "href": "{{PAPAIA_HOST}}:8200/ui",
                    "description": "LLM proxy and load balancer",
                    "visibility": "all",
                },
            ],
        },
        {
            "name": "Infrastructure",
            "tiles": [
                {
                    "name": "Keycloak",
                    "href": "{{PAPAIA_HOST}}:8110/",
                    "description": "Identity and access management",
                    "visibility": "admin",
                },
                {
                    "name": "NGINX Proxy Manager",
                    "href": "{{PAPAIA_HOST}}:8100/",
                    "description": "Reverse proxy with admin UI",
                    "visibility": "admin",
                },
            ],
        },
    ],
}


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def load_tiles(config_dir: str) -> TilesConfig:
    """Load tiles.yaml, seeding the shipped default set on first run."""
    tiles_path = _tiles_path(config_dir)
    if not tiles_path.exists():
        logger.info("tiles.yaml not found; seeding default dashboard tiles")
        config = _config_from_dict(_DEFAULT_TILES)
        save_tiles(config_dir, config)
        return config

    raw = yaml.safe_load(tiles_path.read_text(encoding="utf-8")) or {}
    return _config_from_dict(raw)


def save_tiles(config_dir: str, config: TilesConfig) -> None:
    """Atomically write tiles.yaml."""
    tiles_path = _tiles_path(config_dir)
    tiles_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tiles_path.with_suffix(".yaml.tmp")
    data = _config_to_dict(config)
    tmp.write_text(
        yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    tmp.replace(tiles_path)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def resolve_placeholders(value: str, env: Mapping[str, str]) -> str:
    """Substitute `{{KEY}}` tokens from `env`.

    An unknown key is left verbatim rather than blanked. The token then
    fails the link check in `_resolve_tile`, so the tile is dropped with a
    warning naming the key -- which beats rendering a link that silently
    points at the wrong host because the placeholder collapsed to nothing.
    """

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key in env:
            return env[key]
        logger.warning("tiles.yaml references unknown env key %r", key)
        return match.group(0)

    return _PLACEHOLDER_RE.sub(_replace, value)


def visible_groups(
    config: TilesConfig,
    *,
    is_admin: bool,
    env: Mapping[str, str] | None = None,
) -> list[TileGroup]:
    """Return the groups a caller may see, with placeholders resolved.

    Tiles the caller may not see, and tiles whose resolved link is not a
    safe target, are removed. Groups left empty are dropped so the dashboard
    never renders a heading with nothing under it.
    """
    resolved_env: Mapping[str, str] = env if env is not None else {}
    result: list[TileGroup] = []

    for group in config.groups:
        tiles: list[Tile] = []
        for tile in group.tiles:
            if not _is_visible_to(tile, is_admin=is_admin):
                continue
            resolved = _resolve_tile(tile, resolved_env)
            if resolved is not None:
                tiles.append(resolved)
        if tiles:
            result.append(TileGroup(name=group.name, tiles=tiles))

    return result


def _is_visible_to(tile: Tile, *, is_admin: bool) -> bool:
    return is_admin or tile.visibility == "all"


def _resolve_tile(tile: Tile, env: Mapping[str, str]) -> Tile | None:
    """Return the tile with links resolved, or None if the link is unsafe."""
    href = resolve_placeholders(tile.href, env)
    if not _SAFE_HREF_RE.match(href):
        logger.warning(
            "dropping dashboard tile %r: %r is not an http(s) or site-relative link",
            tile.name,
            href,
        )
        return None

    icon = resolve_placeholders(tile.icon, env) if tile.icon else None
    if icon is not None and not _SAFE_HREF_RE.match(icon):
        logger.warning("ignoring icon for dashboard tile %r: unsafe URL %r", tile.name, icon)
        icon = None

    return Tile(
        name=tile.name,
        href=href,
        description=tile.description,
        icon=icon,
        visibility=tile.visibility,
    )


# ---------------------------------------------------------------------------
# (De)serialization
# ---------------------------------------------------------------------------


def _tiles_path(config_dir: str) -> Path:
    return Path(config_dir) / "manager" / "tiles.yaml"


def _config_from_dict(raw: dict[str, Any]) -> TilesConfig:
    groups = [_group_from_dict(g) for g in raw.get("groups") or [] if isinstance(g, dict)]
    return TilesConfig(version=int(raw.get("version", 1)), groups=groups)


def _group_from_dict(raw: dict[str, Any]) -> TileGroup:
    tiles = [_tile_from_dict(t) for t in raw.get("tiles") or [] if isinstance(t, dict)]
    return TileGroup(name=str(raw.get("name", "")), tiles=tiles)


def _tile_from_dict(raw: dict[str, Any]) -> Tile:
    icon = raw.get("icon")
    return Tile(
        name=str(raw.get("name", "")),
        href=str(raw.get("href", "")),
        description=str(raw.get("description", "")),
        icon=str(icon) if icon else None,
        visibility=_visibility_from_raw(raw.get("visibility"), tile_name=raw.get("name")),
    )


def _visibility_from_raw(value: Any, *, tile_name: Any = None) -> Visibility:
    """Coerce a raw visibility value, failing closed on anything unexpected.

    Omitting the field is the documented way to say "everyone", so absence
    yields `all`. A value that is present but unrecognised is an operator
    mistake -- restricting it is the safe reading, since the alternative is
    exposing a tile that was meant to be limited.
    """
    if value is None:
        return "all"
    if isinstance(value, str) and value in _VISIBILITIES:
        return "all" if value == "all" else "admin"
    logger.warning(
        "tile %r has unrecognised visibility %r; restricting it to administrators",
        tile_name,
        value,
    )
    return "admin"


def _config_to_dict(config: TilesConfig) -> dict[str, Any]:
    return {
        "version": config.version,
        "groups": [
            {
                "name": group.name,
                "tiles": [_tile_to_dict(tile) for tile in group.tiles],
            }
            for group in config.groups
        ],
    }


def _tile_to_dict(tile: Tile) -> dict[str, Any]:
    data: dict[str, Any] = {
        "name": tile.name,
        "href": tile.href,
        "description": tile.description,
    }
    if tile.icon:
        data["icon"] = tile.icon
    data["visibility"] = tile.visibility
    return data
