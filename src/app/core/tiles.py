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

The file is written from the manager UI as well, which adds two obligations. A
document that cannot be parsed has to fail as `TilesFileError` rather than as
an unhandled exception, since a broken hand edit is exactly what the editor
exists to recover from. And `check_value` is the single place a link is judged,
so what the editor previews and what the dashboard renders cannot drift apart.
"""
from __future__ import annotations

import hashlib
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

# Placeholder keys the tile editor offers as one-click inserts. The core .env
# holds every stack secret, so the editor is shown the small slice of key names
# that can plausibly appear in a link -- never the values, and never the rest
# of the file.
_LINK_KEY_RE = re.compile(r"(HOST|DOMAIN|URL|PORT)$")

# Written ahead of the dumped document. A save from the UI rewrites the file
# from the parsed model, so anything the dump cannot express is lost; saying so
# in the file itself is cheaper than an operator finding out by diff.
_FILE_HEADER = (
    "# Dashboard tiles for the papAIa manager.\n"
    "#\n"
    "# Managed from the manager UI: Dashboard -> Edit dashboard. Hand edits keep\n"
    "# working, but the next save from the UI rewrites the whole document --\n"
    "# comments and key order are not preserved.\n"
)


class TilesFileError(Exception):
    """tiles.yaml exists but cannot be read as a tiles document."""


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


@dataclass
class Problem:
    """One reason a draft cannot be saved, addressed to the field at fault.

    Positional rather than named: tiles carry no identity of their own, so the
    indices are what lets the editor put the message back on the right card.
    """

    group: int
    tile: int | None
    field: str
    message: str


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

    return parse_tiles(tiles_path.read_text(encoding="utf-8"))


def parse_tiles(text: str) -> TilesConfig:
    """Parse a tiles document, failing as `TilesFileError` on anything unusable.

    Broken YAML used to surface as a 500 on the dashboard, which says nothing
    about which file is at fault. A typed failure lets the caller name it and
    point at the editor that can fix it.
    """
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise TilesFileError(f"tiles.yaml is not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise TilesFileError("tiles.yaml must hold a mapping at the top level")
    return _config_from_dict(raw)


def save_tiles(config_dir: str, config: TilesConfig) -> None:
    """Atomically write tiles.yaml."""
    tiles_path = _tiles_path(config_dir)
    tiles_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = tiles_path.with_suffix(".yaml.tmp")
    data = _config_to_dict(config)
    tmp.write_text(
        _FILE_HEADER
        + yaml.dump(data, default_flow_style=False, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    tmp.replace(tiles_path)


def load_tiles_raw(config_dir: str) -> str:
    """Return tiles.yaml as text, seeding it first if it does not exist yet."""
    tiles_path = _tiles_path(config_dir)
    if not tiles_path.exists():
        load_tiles(config_dir)
    return tiles_path.read_text(encoding="utf-8")


def save_tiles_raw(config_dir: str, text: str) -> TilesConfig:
    """Parse operator-supplied YAML and write it through the normal save path.

    Deliberately not a byte-for-byte write: routing it through the model is
    what subjects a document typed into the raw editor to the same coercion the
    loader applies, instead of letting it fail later on read.
    """
    config = parse_tiles(text)
    save_tiles(config_dir, config)
    return config


def tiles_revision(config_dir: str) -> str:
    """Content hash of tiles.yaml, or "" when the file does not exist.

    Handed out with every read and required back on every write, so a save
    built on a stale copy -- a second browser tab, or an edit made over SSH
    while the editor was open -- is refused instead of overwriting it.
    """
    tiles_path = _tiles_path(config_dir)
    if not tiles_path.exists():
        return ""
    return hashlib.sha256(tiles_path.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def resolve_placeholders(value: str, env: Mapping[str, str]) -> str:
    """Substitute `{{KEY}}` tokens from `env`.

    An unknown key is left verbatim rather than blanked. The token then
    fails the link check in `check_value`, so the tile is dropped with a
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


def check_value(value: str, env: Mapping[str, str]) -> tuple[str, str | None]:
    """Resolve a link and say why it would be dropped, if it would be.

    Both the editor's preview and `_resolve_tile` go through here, which is the
    point: a link the editor calls fine and the dashboard then discards would
    be worse than either behaviour on its own.
    """
    resolved = resolve_placeholders(value, env)

    unresolved = _PLACEHOLDER_RE.search(resolved)
    if unresolved is not None:
        return resolved, f"{unresolved.group(0)} has no value in the stack environment"

    if not _SAFE_HREF_RE.match(resolved):
        return resolved, "must start with https://, http:// or /"

    return resolved, None


def link_placeholder_keys(env: Mapping[str, str]) -> list[str]:
    """Env keys the editor may offer as placeholder inserts.

    Names only, and only the ones shaped like an address. The values never
    leave the server -- the editor asks the resolve endpoint for a preview.
    """
    return sorted(key for key in env if _LINK_KEY_RE.search(key))


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


def validate_config(config: TilesConfig, env: Mapping[str, str]) -> list[Problem]:
    """Every reason the given draft would not survive a render, at once.

    Run server-side on save. The editor applies the same checks while typing,
    but that copy is a convenience: this one decides whether the file is
    written.
    """
    problems: list[Problem] = []
    seen: set[str] = set()

    for gi, group in enumerate(config.groups):
        name = group.name.strip()
        if not name:
            problems.append(Problem(gi, None, "name", "group name must not be empty"))
        elif name.casefold() in seen:
            problems.append(Problem(gi, None, "name", f"duplicate group name {name!r}"))
        else:
            seen.add(name.casefold())

        for ti, tile in enumerate(group.tiles):
            if not tile.name.strip():
                problems.append(Problem(gi, ti, "name", "tile name must not be empty"))

            if not tile.href.strip():
                problems.append(Problem(gi, ti, "href", "link must not be empty"))
            else:
                _, problem = check_value(tile.href, env)
                if problem is not None:
                    problems.append(Problem(gi, ti, "href", problem))

            if tile.icon:
                _, problem = check_value(tile.icon, env)
                if problem is not None:
                    problems.append(Problem(gi, ti, "icon", problem))

    return problems


def _is_visible_to(tile: Tile, *, is_admin: bool) -> bool:
    return is_admin or tile.visibility == "all"


def _resolve_tile(tile: Tile, env: Mapping[str, str]) -> Tile | None:
    """Return the tile with links resolved, or None if the link is unsafe."""
    href, problem = check_value(tile.href, env)
    if problem is not None:
        logger.warning("dropping dashboard tile %r: %s (%r)", tile.name, problem, href)
        return None

    icon: str | None = None
    if tile.icon:
        icon, icon_problem = check_value(tile.icon, env)
        if icon_problem is not None:
            logger.warning(
                "ignoring icon for dashboard tile %r: %s (%r)", tile.name, icon_problem, icon
            )
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


def config_to_json(config: TilesConfig) -> dict[str, Any]:
    """The wire form shared by the tiles API and the editor template.

    Differs from the YAML form in one way on purpose: `icon` is always present,
    as null when unset, so the editor binds to a field that exists.
    """
    return {
        "version": config.version,
        "groups": [
            {
                "name": group.name,
                "tiles": [
                    {
                        "name": tile.name,
                        "href": tile.href,
                        "description": tile.description,
                        "icon": tile.icon,
                        "visibility": tile.visibility,
                    }
                    for tile in group.tiles
                ],
            }
            for group in config.groups
        ],
    }


def config_from_json(raw: dict[str, Any]) -> TilesConfig:
    """Rebuild a config from the wire form, with the loader's own coercion."""
    return _config_from_dict(raw)


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
