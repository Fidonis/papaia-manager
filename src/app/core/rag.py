"""The optional RAG system of the core, as far as the manager shows it.

The core's `rag` Compose profile starts Qdrant, an MCP server in front of it and
the ingest service. The manager learns about it from the core `.env` only: whether
`rag` is in `COMPOSE_PROFILES`, and the two browser URLs the core stores next to it.

It deliberately does not test whether the URL keys exist. The core derives and keeps
them while the profile is off, so their presence says nothing about whether the
system is installed. A core without the profile (1.4.0 and older) yields nothing,
which keeps the manager independent of the core release.

Two surfaces use this module:

* the dashboard, through `with_rag_tiles`, which adds a computed tile group at
  render time. Nothing is written to `tiles.yaml`: seeding would never reach an
  existing deployment, and a placeholder that cannot be resolved makes every save
  in the tile editor fail validation. The trade-off is that these tiles cannot be
  reordered or removed in the editor.
* the sidebar, through `rag_links`, behind the `rag_nav` template global.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from app.core.envfile import load_env_file
from app.core.inventory import profiles_in
from app.core.tiles import Tile, TileGroup, check_value

RAG_PROFILE = "rag"

# Heading of the dashboard group, and of the sidebar category, which the template
# spells out itself.
GROUP_NAME = "RAG"


@dataclass(frozen=True)
class RagLink:
    """One web interface of the RAG system, shown as a tile and as a menu entry."""

    label: str  # sidebar entry
    tile_name: str  # dashboard tile
    href: str
    description: str
    icon: str  # "upload" or "database"; the template draws it


@dataclass(frozen=True)
class _Spec:
    label: str
    tile_name: str
    env_key: str
    path: str
    description: str
    icon: str


# In tile order. The Qdrant dashboard bypasses the MCP server's role checks, which is
# why the tiles are administrator-only.
_SPECS: tuple[_Spec, ...] = (
    _Spec(
        "Qdrant",
        "Qdrant",
        "QDRANT_PUBLIC_URL",
        "/dashboard",
        "Vector database: REST API and dashboard (needs the api-key)",
        "database",
    ),
    _Spec(
        "Ingest",
        "Qdrant Ingest",
        "QDRANT_INGEST_PUBLIC_URL",
        "/ui",
        "Ingestion jobs and database connections: web interface",
        "upload",
    ),
)

# The sidebar leads with ingestion, which is the day-to-day work; the vector
# database dashboard is the rarer visit.
_MENU_ORDER = ("Ingest", "Qdrant")


def _links(env: Mapping[str, str]) -> list[RagLink]:
    if RAG_PROFILE not in profiles_in(env):
        return []
    links: list[RagLink] = []
    for spec in _SPECS:
        base = env.get(spec.env_key, "").strip().rstrip("/")
        if not base:
            continue
        href, problem = check_value(base + spec.path, {})
        if problem is not None:
            # Same rule as every other tile link: http(s) or site-relative. A value
            # the core could not have derived is dropped rather than rendered.
            continue
        links.append(RagLink(spec.label, spec.tile_name, href, spec.description, spec.icon))
    return links


def rag_links(config_dir: str) -> list[RagLink]:
    """The sidebar entries, in menu order; empty unless the `rag` profile is active."""
    links = _links(load_env_file(Path(config_dir) / ".env"))
    return sorted(links, key=lambda link: _MENU_ORDER.index(link.label))


def _same_target(a: str, b: str) -> bool:
    return a.rstrip("/") == b.rstrip("/")


def with_rag_tiles(groups: list[TileGroup], env: Mapping[str, str]) -> list[TileGroup]:
    """`groups` plus the computed RAG tiles, when the `rag` profile is active.

    A tile the operator already created for the same service wins, matched by name
    or by link, so nothing is shown twice. Tiles go into a group the operator named
    "RAG" if there is one, otherwise into a group of their own at the end. The input
    is not modified.
    """
    links = _links(env)
    if not links:
        return groups

    shown = [tile for group in groups for tile in group.tiles]
    names = {tile.name.casefold() for tile in shown}
    tiles = [
        Tile(name=link.tile_name, href=link.href, description=link.description, visibility="admin")
        for link in links
        if link.tile_name.casefold() not in names
        and not any(_same_target(link.href, tile.href) for tile in shown)
    ]
    if not tiles:
        return groups

    for index, group in enumerate(groups):
        if group.name.casefold() == GROUP_NAME.casefold():
            merged = TileGroup(name=group.name, tiles=[*group.tiles, *tiles])
            return [*groups[:index], merged, *groups[index + 1 :]]
    return [*groups, TileGroup(name=GROUP_NAME, tiles=tiles)]
