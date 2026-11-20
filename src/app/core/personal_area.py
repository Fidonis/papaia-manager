"""The way from the manager to the personal settings area.

The area is a service of its own (the core's module `me`): every person sees and changes their own
settings there. The manager stores nothing of it and only links to it, from the account menu and
from a dashboard tile, when `MANAGER_PERSONAL_SETTINGS_URL` names it.
"""
from __future__ import annotations

from app.core.tiles import Tile, TileGroup, WidgetTile


def _same_target(a: str, b: str) -> bool:
    return a.rstrip("/") == b.rstrip("/")


def with_personal_tile(
    groups: list[TileGroup], url: str, *, name: str, description: str, group_name: str
) -> list[TileGroup]:
    """`groups` with a tile for the area in a group of its own at the start.

    Computed, never written to `tiles.yaml`, and shown to every role. A link tile the operator
    made to the same address wins, so the area is not shown twice. The input is not modified;
    the texts come in already translated, since the tiles are gathered off the request's
    context.
    """
    if any(
        isinstance(tile, Tile) and _same_target(tile.href, url)
        for group in groups
        for tile in group.tiles
    ):
        return groups
    tiles: list[Tile | WidgetTile] = [
        Tile(name=name, href=url, description=description, visibility="all")
    ]
    return [TileGroup(name=group_name, tiles=tiles), *groups]
