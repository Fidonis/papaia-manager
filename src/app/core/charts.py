"""Geometry and number formats of the charts on the Models page.

Pure functions, no I/O and no markup: the template draws what is computed here as HTML and CSS
(bars are boxes with a height in percent, so the labels stay the size of the page's text on every
width) and the tests check the numbers. A chart is a list of columns, one per day, and every
column a stack of segments from the baseline up.

A segment carries a colour *slot*, not a colour: the theme defines `--chart-0` .. `--chart-6`
and `--chart-other` in CSS, once for the light and once for the dark theme. `None` is "Other".
"""
from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

from babel.dates import format_date
from babel.numbers import format_decimal

from app.i18n import SOURCE_LOCALE, current_locale

# The tops a chart can have, as multiples of a power of ten. Half of a top is a tick, and these
# halves (.5 .6 .8 1 1.5 2 2.5 3 4 5) read as round numbers.
_TOPS = (1.0, 1.2, 1.6, 2.0, 3.0, 4.0, 5.0, 6.0, 8.0, 10.0)
# A segment shorter than this (percent of the plot) is drawn this tall, so that one request among
# a million still shows.
_MIN_SEGMENT_PCT = 0.6
# About this many day labels along the axis.
_LABELS = 6


def nice_top(value: float) -> float:
    """The smallest round number that is not below `value` (1 for nothing)."""
    if not value > 0:
        return 1.0
    exponent = 10.0 ** math.floor(math.log10(value))
    fraction = value / exponent
    for top in _TOPS:
        if fraction <= top * (1 + 1e-9):
            return top * exponent
    return 10.0 * exponent


@dataclass(frozen=True)
class Segment:
    slot: int | None  # a colour slot of the theme, None for "Other"
    pct: float  # height, in percent of the plot
    muted: bool = False  # drawn lighter: the output half of a model's tokens


@dataclass(frozen=True)
class Column:
    segments: tuple[Segment, ...]  # from the baseline up
    label: str  # the axis label under the column, or ""
    short: str = ""  # the same for a screen too narrow for `label`


@dataclass(frozen=True)
class Tick:
    pct: float  # distance from the baseline, in percent of the plot
    label: str


@dataclass(frozen=True)
class BarChart:
    columns: tuple[Column, ...]
    ticks: tuple[Tick, ...]
    top: float
    empty: bool  # nothing to draw: every value is zero


Part = tuple[int | None, float, bool]  # (slot, value, muted)


def bar_chart(
    columns: Sequence[Sequence[Part]],
    labels: Sequence[str],
    axis: Callable[[float], str],
    short_labels: Sequence[str] | None = None,
) -> BarChart:
    """Stack `columns` (one list of parts per day) against a shared, round top."""
    totals = [sum(max(value, 0.0) for _, value, _ in column) for column in columns]
    biggest = max(totals, default=0.0)
    top = nice_top(biggest)
    step = 1 if len(columns) <= _LABELS + 2 else math.ceil(len(columns) / _LABELS)
    shorts = labels if short_labels is None else short_labels
    built: list[Column] = []
    for index, column in enumerate(columns):
        segments = tuple(
            Segment(slot, max(value / top * 100.0, _MIN_SEGMENT_PCT), muted)
            for slot, value, muted in column
            if value > 0
        )
        shown = index % step == 0
        label = labels[index] if shown and index < len(labels) else ""
        short = shorts[index] if shown and index < len(shorts) else ""
        built.append(Column(segments, label, short))
    ticks = (Tick(50.0, axis(top / 2)), Tick(100.0, axis(top)))
    return BarChart(tuple(built), ticks, top, empty=biggest <= 0)


def sparkline_points(values: Sequence[float], width: float, height: float) -> str:
    """The `points` of a polyline that draws `values` into a `width` x `height` box.

    Empty when there is nothing above zero to draw.
    """
    if not values or max(values) <= 0:
        return ""
    pad = 1.5
    low = 0.0
    span = max(values) - low
    last = max(len(values) - 1, 1)
    return " ".join(
        f"{pad + index / last * (width - 2 * pad):.1f},"
        f"{height - pad - (value - low) / span * (height - 2 * pad):.1f}"
        for index, value in enumerate(values)
    )


# ── numbers ─────────────────────────────────────────────────────────────────────────────────


def _decimal(value: float, places: int, *, grouping: bool = False) -> str:
    """`value` with up to `places` decimals, none when it rounds to a whole number.

    English keeps its exact layout (`1,234.50`); every other language gets its own separators.
    """
    if round(value, places) == round(value):
        places = 0
    locale = current_locale()
    if locale == SOURCE_LOCALE:
        return f"{value:,.{places}f}" if grouping else f"{value:.{places}f}"
    pattern = ("#,##0" if grouping else "0") + (("." + "0" * places) if places else "")
    return format_decimal(value, format=pattern, locale=locale)


def format_count(value: float) -> str:
    """`950`, `1.2k`, `340k`, `1.23M`, `12.4M`, `3.1B`.

    A figure that would round up to the next unit's 1000 is written in that unit (`999 500` is
    `1M`, not `1000k`).
    """
    value = float(value)
    magnitude = abs(value)
    if magnitude < 1_000:
        return _decimal(value, 0)
    if magnitude < 10_000:
        return _decimal(value / 1_000, 1) + "k"
    if magnitude < 999_500:
        return _decimal(value / 1_000, 0) + "k"
    if magnitude < 10_000_000:
        return _decimal(value / 1_000_000, 2) + "M"
    if magnitude < 999_950_000:
        return _decimal(value / 1_000_000, 1) + "M"
    return _decimal(value / 1_000_000_000, 1) + "B"


def format_usd(value: float) -> str:
    """`$0.00`, `$0.0013`, `$12.40`, `$1,234.50`. A sum too small for four places is `<$0.0001`."""
    if value == 0:
        return "$0.00"
    if abs(value) < 0.0001:
        return "<$0.0001"
    if abs(value) < 0.01:
        return "$" + _decimal(value, 4, grouping=True)
    return "$" + _fixed(value)


def _fixed(value: float) -> str:
    """Two decimals always: a column of sums reads better as `$12.00` than as `$12`."""
    locale = current_locale()
    if locale == SOURCE_LOCALE:
        return f"{value:,.2f}"
    return format_decimal(value, format="#,##0.00", locale=locale)


def format_usd_axis(value: float) -> str:
    """`$0.50`, `$2.50`, `$1,200`: an axis tick drops cents that are zero."""
    if value == 0:
        return "$0"
    return "$" + _decimal(value, 2 if abs(value) >= 0.01 else 4, grouping=True)


def format_price(value: float | None) -> str:
    """A price per million tokens: `$0.60`, `$15`, `$0.1`."""
    if value is None:
        return "–"
    return "$" + _decimal(value, 2 if value >= 0.01 else 4, grouping=True)


# ── days ────────────────────────────────────────────────────────────────────────────────────


def day_label(day: date) -> str:
    """`Oct 18`, or `18. Okt` in German."""
    locale = current_locale()
    if locale == SOURCE_LOCALE:
        return f"{day:%b} {day.day}"
    return format_date(day, "d. MMM" if locale.startswith("de") else "d MMM", locale=locale)


def day_number(day: date) -> str:
    """`18`: the day label on a screen too narrow for `Oct 18`."""
    return str(day.day)


def day_title(day: date) -> str:
    """`Sat, Oct 24`, or `Sa., 24. Okt.` in German: the heading of a tooltip."""
    locale = current_locale()
    if locale == SOURCE_LOCALE:
        return f"{day:%a}, {day:%b} {day.day}"
    return format_date(day, "EEE, d. MMM", locale=locale)
