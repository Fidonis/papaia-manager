"""The Models page, as the data its templates draw.

`build` reads the usage of the asked window (`app.core.model_usage`) and returns one context for
`partials/models_body.html`: the controls with their links, the figures, the charts and the
table, all of it already worded in the reader's language and formatted. The template decides
where things go and nothing else, so what the page says is tested here, without a browser.

The page's whole state is its query string (`Query`): the window, the metric a chart shows, the
tab and the model in detail. Every control is a link to the same page with one thing changed, so
a view can be bookmarked or sent to someone, and the page works without the script that swaps
it in place.
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import date
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlencode

import httpx

from app.config import Settings
from app.core import charts
from app.core.model_usage import (
    DEFAULT_RANGE,
    MAX_DAYS,
    InvalidWindow,
    Metrics,
    ModelUsage,
    Reading,
    UsageReport,
    Window,
    load_reading,
    mode_label,
    resolve_window,
    utc_today,
)
from app.i18n import N_, gettext, ngettext

METRICS = ("tokens", "usd", "requests")
VIEWS = ("overview", "models")
DEFAULT_METRIC = "tokens"
DEFAULT_VIEW = "overview"

PAGE = "/models"
PARTIAL = "/partials/models"
BODY = "#models-body"  # the element of the page the body is swapped into

_RANGE_LABELS = {
    "7d": N_("Last 7 days"),
    "30d": N_("Last 30 days"),
    "90d": N_("Last 90 days"),
    "month": N_("This month"),
    "custom": N_("Custom"),
}
_METRIC_LABELS = {
    "tokens": N_("Tokens"),
    "usd": N_("USD"),
    "requests": N_("Requests"),
}


@dataclass(frozen=True)
class Link:
    page: str  # the address of the page showing this state
    partial: str  # the address of the body alone, for the in-place swap

    @property
    def attrs(self) -> dict[str, str]:
        """The attributes of an anchor that opens this state: a plain link that htmx swaps into
        the body in place and records as the address of the page."""
        return {
            "href": self.page,
            "hx-get": self.partial,
            "hx-target": BODY,
            "hx-swap": "innerHTML",
            "hx-push-url": self.page,
            "hx-indicator": BODY,
        }


@dataclass(frozen=True)
class Query:
    range: str = DEFAULT_RANGE
    start: str = ""
    end: str = ""
    metric: str = DEFAULT_METRIC
    view: str = DEFAULT_VIEW
    model: str = ""

    @classmethod
    def parse(
        cls,
        *,
        range_: str = "",
        start: str = "",
        end: str = "",
        metric: str = "",
        view: str = "",
        model: str = "",
    ) -> Query:
        """A query string, as the page was asked. A value it does not know falls back to the
        default, except the window, whose mistakes are told to the person who made them."""
        return cls(
            range=range_.strip() or DEFAULT_RANGE,
            start=start.strip(),
            end=end.strip(),
            metric=metric if metric in METRICS else DEFAULT_METRIC,
            view=view if view in VIEWS else DEFAULT_VIEW,
            model=model.strip(),
        )

    def _pairs(self) -> list[tuple[str, str]]:
        pairs: list[tuple[str, str]] = []
        if self.range != DEFAULT_RANGE:
            pairs.append(("range", self.range))
        if self.range == "custom":
            pairs += [("from", self.start), ("to", self.end)]
        if self.metric != DEFAULT_METRIC:
            pairs.append(("metric", self.metric))
        if self.model:
            pairs.append(("model", self.model))
        elif self.view != DEFAULT_VIEW:
            pairs.append(("view", self.view))
        return pairs

    def link(self, **changes: str) -> Link:
        """The same page with `changes` applied: `link(range="30d")`, `link(model="")`."""
        query = replace(self, **changes).query_string()
        return Link(PAGE + query, PARTIAL + query)

    def query_string(self) -> str:
        pairs = self._pairs()
        return "?" + urlencode(pairs) if pairs else ""


# ── the figures ─────────────────────────────────────────────────────────────────────────────


def _value(metrics: Metrics, metric: str) -> float:
    if metric == "usd":
        return metrics.spend
    if metric == "requests":
        return float(metrics.requests)
    return float(metrics.tokens)


def _formatter(metric: str) -> Callable[[float], str]:
    return charts.format_usd if metric == "usd" else charts.format_count


def _axis(metric: str) -> Callable[[float], str]:
    return charts.format_usd_axis if metric == "usd" else charts.format_count


def _day_chart(
    columns: Sequence[Sequence[charts.Part]], days: Sequence[date], metric: str
) -> charts.BarChart:
    return charts.bar_chart(
        columns,
        [charts.day_label(day) for day in days],
        _axis(metric),
        [charts.day_number(day) for day in days],
    )


def _unpriced_note(models: Sequence[ModelUsage]) -> int:
    """How many served models with traffic have no price, so their spend is not the whole story.

    A model LiteLLM no longer serves is not counted: what it spent was priced when it ran.
    """
    return sum(
        1
        for model in models
        if model.used and model.entry is not None and not model.entry.priced
    )


def _kpis(totals: Metrics, models: Sequence[ModelUsage], *, overview: bool) -> list[dict[str, str]]:
    unpriced = _unpriced_note(models)
    items = [
        {
            "label": gettext("Tokens"),
            "value": charts.format_count(totals.tokens),
            "sub": gettext("%(input)s in · %(output)s out")
            % {
                "input": charts.format_count(totals.prompt_tokens),
                "output": charts.format_count(totals.completion_tokens),
            },
        },
        {
            "label": gettext("Spend"),
            "value": charts.format_usd(totals.spend),
            "sub": (
                ngettext(
                    "%(count)s model without a price",
                    "%(count)s models without a price",
                    unpriced,
                )
                % {"count": unpriced}
                if unpriced
                else gettext("USD, as LiteLLM prices it")
            ),
        },
        {
            "label": gettext("Requests"),
            "value": charts.format_count(totals.requests),
            "sub": gettext("%(count)s failed") % {"count": charts.format_count(totals.failed)},
        },
    ]
    if overview:
        served = [model for model in models if model.configured]
        used = sum(1 for model in served if model.used)
        items.append(
            {
                "label": gettext("Models used"),
                "value": gettext("%(used)s of %(total)s") % {"used": used, "total": len(served)},
                "sub": gettext("%(count)s not used") % {"count": len(served) - used},
            }
        )
    return items


def _empty_text(totals: Metrics, metric: str) -> str:
    """What an empty chart says: nothing was spent is not the same as nothing was used, which
    is what a model without a price looks like in dollars."""
    if metric == "usd" and totals.requests > 0:
        return gettext("Nothing was spent in this period.")
    return gettext("Nothing was used in this period.")


def _tip_rows(
    items: Sequence[tuple[str, int | None, float, bool]], fmt: Callable[[float], str]
) -> list[dict[str, Any]]:
    return [
        {"name": name, "slot": -1 if slot is None else slot, "muted": muted, "value": fmt(value)}
        for name, slot, value, muted in sorted(items, key=lambda item: -item[2])
    ]


def _overview_chart(report: UsageReport, metric: str) -> dict[str, Any]:
    fmt = _formatter(metric)
    ordered = sorted(
        report.models,
        key=lambda model: (model.slot is None, model.slot or 0, model.name.casefold()),
    )
    columns: list[list[charts.Part]] = []
    tips: list[dict[str, Any]] = []
    other_name = gettext("Other")
    for index, day in enumerate(report.days):
        parts: list[charts.Part] = []
        items: list[tuple[str, int | None, float, bool]] = []
        other = 0.0
        for model in ordered:
            value = _value(model.daily[index], metric)
            if value <= 0:
                continue
            if model.slot is None:
                other += value
                continue
            parts.append((model.slot, value, False))
            items.append((model.name, model.slot, value, False))
        if other > 0:
            parts.append((None, other, False))
            items.append((other_name, None, other, False))
        columns.append(parts)
        tips.append(
            {
                "title": charts.day_title(day),
                "rows": _tip_rows(items, fmt),
                "total": fmt(sum(value for _, _, value, _ in items)),
            }
        )
    chart = _day_chart(columns, report.days, metric)
    folded = [m for m in report.models if m.slot is None and _value(m.totals, metric) > 0]
    legend: list[dict[str, Any]] = [
        {"name": model.name, "slot": model.slot, "muted": False}
        for model in ordered
        if model.slot is not None and _value(model.totals, metric) > 0
    ]
    if folded:
        legend.append(
            {
                "name": ngettext(
                    "Other (%(count)s model)", "Other (%(count)s models)", len(folded)
                )
                % {"count": len(folded)},
                "slot": None,
                "muted": False,
            }
        )
    return {
        "chart": chart,
        "tips": tips,
        "legend": legend,
        "empty_text": _empty_text(report.totals, metric),
    }


def _detail_chart(model: ModelUsage, days: Sequence[Any], metric: str) -> dict[str, Any]:
    fmt = _formatter(metric)
    columns: list[list[charts.Part]] = []
    tips: list[dict[str, Any]] = []
    for index, day in enumerate(days):
        daily = model.daily[index]
        if metric == "tokens":
            parts: list[charts.Part] = [
                (model.slot, float(daily.prompt_tokens), False),
                (model.slot, float(daily.completion_tokens), True),
            ]
            items = [
                (gettext("Input tokens"), model.slot, float(daily.prompt_tokens), False),
                (gettext("Output tokens"), model.slot, float(daily.completion_tokens), True),
            ]
            total = float(daily.tokens)
        else:
            value = _value(daily, metric)
            parts = [(model.slot, value, False)]
            items = [(gettext(_METRIC_LABELS[metric]), model.slot, value, False)]
            total = value
        columns.append(parts)
        tips.append(
            {
                "title": charts.day_title(day),
                "rows": _tip_rows([item for item in items if item[2] > 0], fmt),
                "total": fmt(total),
            }
        )
    chart = _day_chart(columns, days, metric)
    legend: list[dict[str, Any]] = (
        [
            {"name": gettext("Input tokens"), "slot": model.slot, "muted": False},
            {"name": gettext("Output tokens"), "slot": model.slot, "muted": True},
        ]
        if metric == "tokens"
        else []
    )
    return {
        "chart": chart,
        "tips": tips,
        "legend": legend,
        "empty_text": _empty_text(model.totals, metric),
    }


@dataclass(frozen=True)
class Row:
    usage: ModelUsage
    type: str  # "Chat", "Embedding", ... or ""
    provider: str  # "openai · localai" or ""
    price: str  # "$0.60 / $2.40" per million tokens, "" when there is none
    spark: str  # the points of the tokens-per-day line
    link: Link


def _row(model: ModelUsage, query: Query) -> Row:
    entry = model.entry
    provider = ""
    price = ""
    kind = ""
    if entry is not None:
        provider = " · ".join(part for part in (entry.provider, entry.host) if part)
        kind = mode_label(entry.mode) if entry.mode else ""
        if entry.priced:
            price = (
                f"{charts.format_price(entry.input_per_million)} / "
                f"{charts.format_price(entry.output_per_million)}"
            )
    return Row(
        usage=model,
        type=kind,
        provider=provider,
        price=price,
        spark=charts.sparkline_points([float(day.tokens) for day in model.daily], 72, 20),
        link=query.link(model=model.name),
    )


def _shares(report: UsageReport, query: Query) -> list[dict[str, Any]]:
    metric = query.metric
    fmt = _formatter(metric)
    total = _value(report.totals, metric)
    rows = [
        (_value(model.totals, metric), model)
        for model in report.models
        if _value(model.totals, metric) > 0
    ]
    return [
        {
            "name": model.name,
            "slot": model.slot,
            "value": fmt(value),
            "pct": value / total * 100.0 if total else 0.0,
            "link": query.link(model=model.name),
        }
        for value, model in sorted(rows, key=lambda item: -item[0])
    ]


def _day_table(report: UsageReport, model: ModelUsage | None) -> list[dict[str, str]]:
    daily = model.daily if model is not None else report.daily_totals
    return [
        {
            "day": charts.day_title(day),
            "tokens": charts.format_count(metrics.tokens),
            "spend": charts.format_usd(metrics.spend),
            "requests": charts.format_count(metrics.requests),
        }
        for day, metrics in zip(report.days, daily, strict=True)
    ]


def _body(report: UsageReport, query: Query) -> dict[str, Any]:
    """What the tab asked for: the overview, the table of models or one model."""
    metric = query.metric
    detail = report.model(query.model) if query.model else None
    body: dict[str, Any] = {
        "kind": "overview",
        "metric_label": gettext(_METRIC_LABELS[metric]),
        "model_missing": query.model if query.model and detail is None else "",
    }
    if detail is not None:
        body.update(
            kind="detail",
            model=detail,
            row=_row(detail, query),
            kpis=_kpis(detail.totals, [detail], overview=False),
            unpriced=_unpriced_note([detail]),
            day_table=_day_table(report, detail),
            **_detail_chart(detail, report.days, metric),
        )
        return body
    body["kpis"] = _kpis(report.totals, report.models, overview=True)
    body["unpriced"] = _unpriced_note(report.models)
    body["day_table"] = _day_table(report, None)
    if query.view == "models" or body["model_missing"]:
        order = sorted(
            report.models,
            key=lambda model: (-model.totals.tokens, -model.totals.requests, model.name.casefold()),
        )
        body.update(kind="models", rows=[_row(model, query) for model in order])
        return body
    body.update(shares=_shares(report, query), **_overview_chart(report, metric))
    return body


# ── the page ────────────────────────────────────────────────────────────────────────────────


def _window_text(window: Window) -> str:
    return ngettext(
        "%(start)s to %(end)s · %(count)s day",
        "%(start)s to %(end)s · %(count)s days",
        window.length,
    ) % {"start": window.start.isoformat(), "end": window.end.isoformat(), "count": window.length}


def _resolve(query: Query) -> tuple[Window, str]:
    """The asked window, or the default one and why the asked one was refused.

    A custom window without any date is not a mistake but the button just pressed: the form
    asks for the dates, and the page shows the default window meanwhile.
    """
    if query.range == "custom" and not query.start and not query.end:
        return resolve_window(DEFAULT_RANGE), ""
    try:
        return resolve_window(query.range, query.start, query.end), ""
    except InvalidWindow as exc:
        return resolve_window(DEFAULT_RANGE), str(exc)


def fmt_namespace() -> SimpleNamespace:
    return SimpleNamespace(
        count=charts.format_count, usd=charts.format_usd, price=charts.format_price
    )


async def build(
    settings: Settings,
    query: Query,
    *,
    fresh: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    window, notice = _resolve(query)
    reading: Reading = await load_reading(settings, window, fresh=fresh, transport=transport)
    here = query.link()
    joiner = "&" if "?" in here.partial else "?"
    context: dict[str, Any] = {
        "q": query,
        "link": query.link,
        "reload": Link(here.page, here.partial + joiner + "fresh=true"),
        "ranges": [
            {
                "key": key,
                "label": gettext(label),
                "active": query.range == key,
                # A custom window needs its dates, which the form asks for; the link only opens it.
                "link": query.link(range=key, start=query.start, end=query.end),
            }
            for key, label in _RANGE_LABELS.items()
        ],
        "metrics": [
            {
                "key": key,
                "label": gettext(label),
                "active": query.metric == key,
                "link": query.link(metric=key),
            }
            for key, label in _METRIC_LABELS.items()
        ],
        "tabs": [
            {
                "key": "overview",
                "label": gettext("Overview"),
                "active": query.view == "overview",
                "link": query.link(view="overview", model=""),
            },
            {
                "key": "models",
                "label": gettext("Per model"),
                "active": query.view == "models",
                "link": query.link(view="models", model=""),
            },
        ],
        "max_days": MAX_DAYS,
        "today": utc_today().isoformat(),
        "window": window,
        "window_text": _window_text(window),
        "window_notice": notice,
        "reading": reading,
        "served": sum(1 for model in reading.report.models if model.configured)
        if reading.report is not None
        else 0,
        "problem": reading.problem.text() if reading.problem else "",
        "updated": reading.fetched_at.strftime("%H:%M"),
        "fmt": fmt_namespace(),
        "report": reading.report,
        "kind": "",
    }
    if reading.report is not None:
        context.update(_body(reading.report, query))
    return context
