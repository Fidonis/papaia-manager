"""The widget payload: what every widget produces and what the dashboard draws.

A widget never hands the dashboard markup. It hands a *document* -- a state, a view
and the data for that view -- and the manager draws it with templates of its own.
Built-in widgets and, later, widgets from elsewhere go through the same parser, so
the contract is exercised by the code that ships with the manager.

The contract is deliberately small and closed:

* `widget_api` is the generation of this document format, not the manager's version.
  A document of another generation is refused, not guessed at.
* The views are a fixed set (`stat`, `status`, `progress`, `list`). There is no way to
  ask for a view the manager does not have, and no field that carries HTML, a style
  or a script.
* Every string is plain text with a length limit. Text that looks like markup or
  carries a control character is refused rather than escaped: the templates escape it
  anyway, and a payload that tries is not one to show.
* A link goes through the same check as a tile's link (`tiles.check_value`), so a
  `javascript:` address is as unwelcome here as it is there.
* The state is the widget's verdict. The manager draws it and never second-guesses
  it, the way the host page takes the core's verdicts as they are.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import (
    AfterValidator,
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    ValidationError,
    model_validator,
)

from app.core.tiles import check_value

# The generation of the document format this manager reads.
WIDGET_API = 1

# A document larger than this is refused before it is parsed. A card shows a number and a
# line of text; anything this size is not one.
MAX_PAYLOAD_BYTES = 64 * 1024

MAX_LIST_ITEMS = 8


class PayloadError(ValueError):
    """A widget document that cannot be shown. The message is for an administrator."""


class State(StrEnum):
    """The widget's own verdict. `unknown` is "cannot tell", which is not a problem."""

    OK = "ok"
    WARN = "warn"
    CRITICAL = "critical"
    UNKNOWN = "unknown"


# Something shaped like a tag: `<b`, `</`, `<!--`, `<?`. A bare "<" before a digit or a
# space ("< 5 min", "<1 minute") is ordinary text.
_TAG_LIKE = re.compile(r"<\s*[A-Za-z/!?]")


def _plain(value: str) -> str:
    if _TAG_LIKE.search(value):
        raise ValueError("must be plain text, not markup")
    if any(ord(char) < 32 for char in value):
        raise ValueError("must not contain control characters")
    return value.strip()


# Three lengths cover every field: a figure or a headline, a line under it, and a link.
Short = Annotated[str, Field(max_length=40), AfterValidator(_plain)]
Line = Annotated[str, Field(max_length=120), AfterValidator(_plain)]
Unit = Annotated[str, Field(max_length=20), AfterValidator(_plain)]


class _Model(BaseModel):
    # Unknown fields are dropped, not refused: a newer producer of the same generation may
    # add one, and the card does not need it.
    model_config = ConfigDict(extra="ignore", frozen=True)


def _safe_href(value: str) -> str:
    _, problem = check_value(value, {})
    if problem is not None:
        raise ValueError(problem)
    # `//host/path` and `/\host/path` pass the check above ("starts with /") and are addresses
    # on another host. A link that starts with "/" must mean a page of this manager: the card
    # shows such a link to administrators only, on exactly that reading.
    if value.startswith(("//", "/\\")):
        raise ValueError("must be an address or a path on this manager, not //host")
    return value


Href = Annotated[str, Field(max_length=500), AfterValidator(_safe_href)]


class Link(_Model):
    """Where the card leads: the page that explains what it shows."""

    label: Unit
    href: Href


class StatData(_Model):
    """One figure. `value` is text so the producer decides how it is written (`14/15`)."""

    value: Short
    unit: Unit = ""
    label: Line = ""


class StatusData(_Model):
    """One short statement, for things that are a word rather than a number."""

    message: Short
    label: Line = ""


class ProgressData(_Model):
    """A bar. The fill is arithmetic for the eye; the colour is the payload's `state`."""

    value: float = Field(allow_inf_nan=False, ge=0)
    max: float = Field(default=100.0, allow_inf_nan=False, gt=0)
    unit: Unit = ""
    label: Line = ""

    @model_validator(mode="after")
    def _within_max(self) -> ProgressData:
        if self.value > self.max:
            raise ValueError("value must not exceed max")
        return self

    @property
    def percent(self) -> float:
        return round(100.0 * self.value / self.max, 1)


class ListItem(_Model):
    label: Line
    value: Short = ""
    state: State | None = None
    href: Href | None = None


class ListData(_Model):
    items: list[ListItem] = Field(max_length=MAX_LIST_ITEMS)


class _Base(_Model):
    widget_api: Literal[1]
    state: State
    link: Link | None = None
    # When the figures are from, if the producer knows. Shown on a card that is stale.
    updated_at: AwareDatetime | None = None


class StatPayload(_Base):
    view: Literal["stat"]
    data: StatData


class StatusPayload(_Base):
    view: Literal["status"]
    data: StatusData


class ProgressPayload(_Base):
    view: Literal["progress"]
    data: ProgressData


class ListPayload(_Base):
    view: Literal["list"]
    data: ListData


WidgetPayload = StatPayload | StatusPayload | ProgressPayload | ListPayload

# The views, in the order the editor lists them.
VIEWS: tuple[str, ...] = ("stat", "status", "progress", "list")


class _Document(RootModel[Annotated[WidgetPayload, Field(discriminator="view")]]):
    pass


def parse_payload(document: object) -> WidgetPayload:
    """Check a widget document and return it as a typed payload.

    Raises `PayloadError` for anything that is not a document of this generation, with a
    sentence that names the field and never repeats the value.
    """
    if not isinstance(document, dict):
        raise PayloadError("the widget document must be an object")
    generation = document.get("widget_api")
    if generation != WIDGET_API:
        raise PayloadError(
            f"widget_api {generation!r} is not supported (this manager reads {WIDGET_API})"
        )
    try:
        size = len(json.dumps(document, default=str))
    except (TypeError, ValueError) as exc:
        raise PayloadError("the widget document is not valid JSON") from exc
    if size > MAX_PAYLOAD_BYTES:
        raise PayloadError(f"the widget document is larger than {MAX_PAYLOAD_BYTES // 1024} KiB")
    try:
        return _Document.model_validate(document).root
    except ValidationError as exc:
        first = exc.errors()[0]
        where = ".".join(str(part) for part in first["loc"])
        raise PayloadError(f"{where}: {first['msg']}") from exc


@dataclass(frozen=True)
class WidgetResult:
    """What reading a widget came to.

    `ok` has a fresh payload. `stale` has the last good one, because the latest read
    failed: the card shows it with the time it is from. `unavailable` has none, and
    `reason` says why in words for an administrator -- everyone else gets a sentence of
    the template's own.
    """

    status: Literal["ok", "stale", "unavailable"]
    payload: WidgetPayload | None = None
    reason: str = ""
    fetched_at: datetime | None = None
