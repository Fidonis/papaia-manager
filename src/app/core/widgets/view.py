"""What the templates need to lay out and poll a widget tile.

The dashboard renders a placeholder per widget tile and lets the browser fetch the card
itself (`/partials/widgets/{id}`), so each widget refreshes on its own and a slow one never
holds the page up. The placeholder needs only the tile's id, how long to wait between
polls, and a title for the moment before the card arrives.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.tiles import WidgetTile
from app.core.widgets import registry


@dataclass(frozen=True)
class Slot:
    id: str
    title: str
    # Seconds between polls.
    interval: int


def title_of(tile: WidgetTile) -> str:
    """The title a card shows: the tile's own, else the type's name, else the raw id."""
    if tile.title:
        return tile.title
    widget_type = registry.get(tile.widget)
    return widget_type.name if widget_type is not None else tile.widget


def slot(tile: WidgetTile) -> Slot:
    widget_type = registry.get(tile.widget)
    # A type nobody registered renders once as "not available" and has nothing to refresh.
    interval = widget_type.interval if widget_type is not None else registry.MAX_REFRESH_SECONDS
    return Slot(id=tile.id, title=title_of(tile), interval=interval)
