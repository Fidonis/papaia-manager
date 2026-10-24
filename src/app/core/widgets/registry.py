"""The registry of widget types: what can be placed on the dashboard.

A widget type is a name, a view, a few defaults and a *provider*, the function that
produces the type's payload (see `model`). The registry is the only place a tile's
`widget:` is resolved: a tile that names a type nobody registered renders as
unavailable, never as something improvised from the file.

Registration is explicit and happens at import time (`builtin` does it for the types
that ship with the manager). There is no discovery by file name and nothing is loaded
from the configuration directory: the code that can run in this process is the code
that was installed with it.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any

from app.config import Settings
from app.core.tiles import Size, Visibility
from app.core.widgets.model import VIEWS
from app.i18n import gettext as _

if TYPE_CHECKING:
    from app.core.jobs import JobQueue

# The icons a card can show, by name, as the `d` of a 24x24 outline path. The templates draw
# them (the dashboard, and the editor from the same table); a widget cannot bring one, which
# is what keeps a remote image address, and the tracking that comes with it, off the cards.
ICON_PATHS: dict[str, str] = {
    "archive": "M5 8h14M5 8a2 2 0 110-4h14a2 2 0 110 4M5 8v10a2 2 0 002 2h10a2 2 0 002-2V8m-9 4h4",
    "server": (
        "M5 12h14M5 12a2 2 0 01-2-2V6a2 2 0 012-2h14a2 2 0 012 2v4a2 2 0 01-2 2M5 12a2 2 0 "
        "00-2 2v4a2 2 0 002 2h14a2 2 0 002-2v-4a2 2 0 00-2-2m-2-4h.01M17 16h.01"
    ),
    "heart": (
        "M4.318 6.318a4.5 4.5 0 000 6.364L12 20.364l7.682-7.682a4.5 4.5 0 00-6.364-6.364L12 "
        "7.636l-1.318-1.318a4.5 4.5 0 00-6.364 0z"
    ),
    "alert": (
        "M12 9v2m0 4h.01M4.93 19h14.14a2 2 0 001.74-3L13.74 4a2 2 0 00-3.48 0L3.19 16a2 2 "
        "0 001.74 3z"
    ),
    "tag": (
        "M7 7h.01M7 3h5c.512 0 1.024.195 1.414.586l7 7a2 2 0 010 2.828l-7 7a2 2 0 01-2.828 "
        "0l-7-7A1.994 1.994 0 013 12V7a4 4 0 014-4z"
    ),
    "shield": (
        "M9 12l2 2 4-4m5.618-4.016A11.955 11.955 0 0112 2.944a11.955 11.955 0 01-8.618 "
        "3.04A12.02 12.02 0 003 9c0 5.591 3.824 10.29 9 11.622 5.176-1.332 9-6.03 "
        "9-11.622 0-1.042-.133-2.052-.382-3.016z"
    ),
}
ICONS: frozenset[str] = frozenset(ICON_PATHS)

# How often a card may ask again. Below the floor a poll is a load test; above the ceiling
# the card is stale before it is read.
MIN_REFRESH_SECONDS = 15
MAX_REFRESH_SECONDS = 3600

_ID_RE = re.compile(r"^[a-z][a-z0-9-]{0,39}$")


class WidgetUnavailableError(Exception):
    """A provider's way of saying "not now", with a reason an administrator can act on.

    Anything else a provider raises is logged and shown as a sentence of the template's
    own: the text of an unexpected exception is not for a dashboard.
    """


@dataclass(frozen=True)
class WidgetContext:
    """What a provider may look at. Read-only: a widget reports, it never changes anything."""

    settings: Settings
    now: datetime
    # The job queue, for the providers that must not judge the stack while a job works on it.
    queue: JobQueue | None = None


Provider = Callable[[WidgetContext], Awaitable[dict[str, Any]]]
# Answers with the reason a type cannot be used on this installation, or None.
Requirement = Callable[[Settings], str | None]


def _no_requirement(settings: Settings) -> str | None:
    return None


@dataclass(frozen=True)
class WidgetType:
    id: str
    name: str
    description: str
    icon: str
    view: str
    provider: Provider
    size: Size = "s"
    # What a tile gets when an administrator places the widget. Third parties' data is
    # not assumed to be for everyone; neither are most readings of the installation.
    visibility: Visibility = "admin"
    refresh_seconds: int = 60
    requires: Requirement = _no_requirement

    @property
    def interval(self) -> int:
        """The refresh interval, held between the floor and the ceiling."""
        return min(max(self.refresh_seconds, MIN_REFRESH_SECONDS), MAX_REFRESH_SECONDS)


_TYPES: dict[str, WidgetType] = {}


def register(widget_type: WidgetType) -> WidgetType:
    """Add a type. A malformed or repeated one is a programming error and raises."""
    if not _ID_RE.match(widget_type.id):
        raise ValueError(_(
            "widget id %(id)r must match %(pattern)s"
        ) % {"id": widget_type.id, "pattern": _ID_RE.pattern})
    if widget_type.id in _TYPES:
        raise ValueError(_("widget %(id)r is registered twice") % {"id": widget_type.id})
    if widget_type.icon not in ICONS:
        raise ValueError(_(
            "widget %(id)r uses the unknown icon %(icon)r"
        ) % {"id": widget_type.id, "icon": widget_type.icon})
    if widget_type.view not in VIEWS:
        raise ValueError(_(
            "widget %(id)r uses the unknown view %(view)r"
        ) % {"id": widget_type.id, "view": widget_type.view})
    _TYPES[widget_type.id] = widget_type
    return widget_type


def get(widget_id: str) -> WidgetType | None:
    return _TYPES.get(widget_id)


def all_types() -> list[WidgetType]:
    """Every registered type, in the order they were registered."""
    return list(_TYPES.values())


def known_ids() -> frozenset[str]:
    return frozenset(_TYPES)


def describe(settings: Settings) -> list[dict[str, Any]]:
    """The types as the editor's gallery needs them, with whether each can be used here.

    A type whose requirement is not met is listed all the same, with the reason, so an
    administrator learns what is missing instead of wondering where a widget went.
    """
    described: list[dict[str, Any]] = []
    for widget_type in all_types():
        reason = widget_type.requires(settings)
        described.append(
            {
                "id": widget_type.id,
                "name": _(widget_type.name),
                "description": _(widget_type.description),
                "icon": widget_type.icon,
                "view": widget_type.view,
                "size": widget_type.size,
                "visibility": widget_type.visibility,
                "refresh": widget_type.interval,
                "available": reason is None,
                "reason": reason or "",
            }
        )
    return described
