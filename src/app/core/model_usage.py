"""The models LiteLLM serves and what each of them consumed in a window of days.

LiteLLM reports usage per UTC day and per model group, the name a user puts in a request and the
name `/model/info` lists, so a model of the catalog and its usage meet on one string. This module
turns the three answers of `app.core.litellm` into a `UsageReport`: the catalog (with prices per
million tokens), the days of the window, and for every model its numbers per day.

Reading is cached per window. The cache holds numbers and msgids only, never text in a language:
one reading is served to every account, each in its own language. A reading never raises: LiteLLM
being down, the key missing or refused, or an answer that cannot be read is a `Reading` with a
`problem`, and the page says so.
"""
from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.core.litellm import (
    NO_KEY,
    UNREADABLE,
    LitellmClient,
    LitellmError,
    Problem,
    litellm_secrets,
)
from app.core.qdrant import tls_verify
from app.i18n import N_, gettext

# ── the window ──────────────────────────────────────────────────────────────────────────────

DEFAULT_RANGE = "7d"
RANGES = ("7d", "30d", "90d", "month", "custom")
MAX_DAYS = 365
_PRESET_DAYS = {"7d": 7, "30d": 30, "90d": 90}
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")

UNKNOWN_RANGE = N_("Unknown time window.")
BOTH_DATES = N_("Enter both dates.")
BAD_DATE = N_("Dates must look like 2026-10-24.")
START_AFTER_END = N_("The start date is after the end date.")
FUTURE_END = N_("The end date can't be in the future.")
TOO_LONG = N_("A window can't be longer than %(days)s days.")


class InvalidWindow(ValueError):  # noqa: N818 - a refused window, not a failure
    def __init__(self, msgid: str, params: Mapping[str, object] | None = None) -> None:
        super().__init__(msgid)
        self.problem = Problem(msgid, dict(params or {}))

    def __str__(self) -> str:
        return self.problem.text()


@dataclass(frozen=True)
class Window:
    """A run of whole UTC days, `start` and `end` both included."""

    key: str
    start: date
    end: date

    @property
    def length(self) -> int:
        return (self.end - self.start).days + 1

    def dates(self) -> tuple[date, ...]:
        return tuple(self.start + timedelta(days=offset) for offset in range(self.length))


def utc_today() -> date:
    return datetime.now(UTC).date()


def _parse_day(text: str) -> date:
    text = text.strip()
    if not _ISO_DAY.match(text):
        raise InvalidWindow(BAD_DATE)
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise InvalidWindow(BAD_DATE) from exc


def resolve_window(
    key: str, start: str = "", end: str = "", *, today: date | None = None
) -> Window:
    """The window a request asks for. `7d` is today and the six days before it.

    Raises `InvalidWindow`, whose message is meant for the person who typed the dates.
    """
    today = today or utc_today()
    if key in _PRESET_DAYS:
        return Window(key, today - timedelta(days=_PRESET_DAYS[key] - 1), today)
    if key == "month":
        return Window(key, today.replace(day=1), today)
    if key != "custom":
        raise InvalidWindow(UNKNOWN_RANGE)
    if not start.strip() or not end.strip():
        raise InvalidWindow(BOTH_DATES)
    first, last = _parse_day(start), _parse_day(end)
    if first > last:
        raise InvalidWindow(START_AFTER_END)
    if last > today:
        raise InvalidWindow(FUTURE_END)
    if (last - first).days + 1 > MAX_DAYS:
        raise InvalidWindow(TOO_LONG, {"days": MAX_DAYS})
    return Window(key, first, last)


# ── the numbers ─────────────────────────────────────────────────────────────────────────────


def _int(value: object) -> int:
    try:
        return int(value)  # type: ignore[call-overload,no-any-return]
    except (TypeError, ValueError):
        return 0


def _float(value: object) -> float:
    try:
        return float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0.0


@dataclass(frozen=True)
class Metrics:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    spend: float = 0.0
    requests: int = 0
    failed: int = 0

    @property
    def tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: Metrics) -> Metrics:
        return Metrics(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
            self.spend + other.spend,
            self.requests + other.requests,
            self.failed + other.failed,
        )


ZERO = Metrics()


def metrics_from(raw: object) -> Metrics:
    """LiteLLM's `metrics` object. Fields it leaves out count as nothing."""
    if not isinstance(raw, dict):
        return ZERO
    return Metrics(
        prompt_tokens=_int(raw.get("prompt_tokens")),
        completion_tokens=_int(raw.get("completion_tokens")),
        spend=_float(raw.get("spend")),
        requests=_int(raw.get("api_requests")),
        failed=_int(raw.get("failed_requests")),
    )


def parse_usage(
    results: Iterable[Mapping[str, Any]], window: Window
) -> dict[str, dict[date, Metrics]]:
    """Add up the day entries per model group and day.

    The same day can come from several pages, and the same group from several keys, so values
    are added rather than taken. Days outside the window and entries without a name are dropped.
    """
    usage: dict[str, dict[date, Metrics]] = {}
    for entry in results:
        try:
            day = date.fromisoformat(str(entry.get("date")))
        except ValueError:
            continue
        if not window.start <= day <= window.end:
            continue
        breakdown = entry.get("breakdown")
        groups = breakdown.get("model_groups") if isinstance(breakdown, dict) else None
        if not isinstance(groups, dict):
            continue
        for name, item in groups.items():
            if not isinstance(name, str) or not name or not isinstance(item, dict):
                continue
            days = usage.setdefault(name, {})
            days[day] = days.get(day, ZERO) + metrics_from(item.get("metrics"))
    return usage


# ── the catalog ─────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelEntry:
    """A model LiteLLM serves, as users name it."""

    name: str
    provider: str = ""  # `openai`, `ollama`, ... ; what the deployment's model id starts with
    host: str = ""  # the host of its `api_base`, when it has one
    mode: str = ""  # `chat`, `embedding`, ... ; LiteLLM leaves it out for models it does not know
    input_per_million: float | None = None
    output_per_million: float | None = None
    deployments: int = 1

    @property
    def priced(self) -> bool:
        return bool(self.input_per_million) or bool(self.output_per_million)


def _per_million(value: object) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value) * 1_000_000, 6)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _entry_of(item: Mapping[str, Any]) -> ModelEntry | None:
    """One deployment of `/model/info`. Only these fields are read: `litellm_params` holds the
    deployment's settings, and what it carries besides is none of this page's business."""
    name = item.get("model_name")
    if not isinstance(name, str) or not name:
        return None
    params = item.get("litellm_params")
    info = item.get("model_info")
    params = params if isinstance(params, dict) else {}
    info = info if isinstance(info, dict) else {}
    model_id = params.get("model")
    provider = params.get("custom_llm_provider")
    if not isinstance(provider, str) or not provider:
        has_prefix = isinstance(model_id, str) and "/" in model_id
        provider = model_id.split("/", 1)[0] if isinstance(model_id, str) and has_prefix else ""
    api_base = params.get("api_base")
    host = (urlparse(api_base).hostname or "") if isinstance(api_base, str) else ""
    mode = info.get("mode")
    return ModelEntry(
        name=name,
        provider=provider,
        host=host,
        mode=mode if isinstance(mode, str) else "",
        input_per_million=_per_million(info.get("input_cost_per_token")),
        output_per_million=_per_million(info.get("output_cost_per_token")),
    )


def parse_catalog(data: Iterable[Mapping[str, Any]]) -> tuple[ModelEntry, ...]:
    """One entry per model name. Several deployments behind one name are one model (LiteLLM
    spreads the requests over them), described by the first, with a gap filled by a later one."""
    entries: dict[str, ModelEntry] = {}
    for item in data:
        entry = _entry_of(item)
        if entry is None:
            continue
        seen = entries.get(entry.name)
        if seen is None:
            entries[entry.name] = entry
            continue
        entries[entry.name] = ModelEntry(
            name=seen.name,
            provider=seen.provider or entry.provider,
            host=seen.host or entry.host,
            mode=seen.mode or entry.mode,
            input_per_million=(
                seen.input_per_million
                if seen.input_per_million is not None
                else entry.input_per_million
            ),
            output_per_million=(
                seen.output_per_million
                if seen.output_per_million is not None
                else entry.output_per_million
            ),
            deployments=seen.deployments + 1,
        )
    return tuple(sorted(entries.values(), key=lambda entry: entry.name.casefold()))


# ── the report ──────────────────────────────────────────────────────────────────────────────

# How many models get a colour of their own; the rest are drawn together as "Other".
SLOTS = 7


@dataclass(frozen=True)
class ModelUsage:
    name: str
    entry: ModelEntry | None  # None: usage under a name LiteLLM no longer serves
    daily: tuple[Metrics, ...]  # one per day of the window
    totals: Metrics
    slot: int | None  # the model's colour, None for "Other"

    @property
    def configured(self) -> bool:
        return self.entry is not None

    @property
    def used(self) -> bool:
        return self.totals.requests > 0


@dataclass(frozen=True)
class UsageReport:
    window: Window
    days: tuple[date, ...]
    models: tuple[ModelUsage, ...]  # by name
    totals: Metrics
    daily_totals: tuple[Metrics, ...]
    complete: bool  # False when LiteLLM had more pages than the manager reads

    def model(self, name: str) -> ModelUsage | None:
        return next((model for model in self.models if model.name == name), None)


def assign_slots(names: Iterable[str], tokens: Mapping[str, tuple[int, int]]) -> dict[str, int]:
    """A colour slot per model, stable while the set of models is.

    With up to `SLOTS` models the slot is the position among all of them by name, so a model
    keeps its colour from one window to the next. With more, the `SLOTS` busiest of the window
    keep a slot (in name order) and the rest fall into "Other".
    """
    ordered = sorted(set(names), key=str.casefold)
    if len(ordered) <= SLOTS:
        return {name: index for index, name in enumerate(ordered)}
    busiest = sorted(ordered, key=lambda name: tokens.get(name, (0, 0)), reverse=True)[:SLOTS]
    return {name: index for index, name in enumerate(sorted(busiest, key=str.casefold))}


def build_report(
    window: Window,
    catalog: Iterable[ModelEntry],
    usage: Mapping[str, Mapping[date, Metrics]],
    *,
    complete: bool = True,
) -> UsageReport:
    entries = {entry.name: entry for entry in catalog}
    days = window.dates()
    names = sorted(set(entries) | set(usage), key=str.casefold)
    summed = {
        name: sum(usage.get(name, {}).values(), ZERO) for name in names
    }
    slots = assign_slots(names, {name: (m.tokens, m.requests) for name, m in summed.items()})
    models = tuple(
        ModelUsage(
            name=name,
            entry=entries.get(name),
            daily=tuple(usage.get(name, {}).get(day, ZERO) for day in days),
            totals=summed[name],
            slot=slots.get(name),
        )
        for name in names
    )
    return UsageReport(
        window=window,
        days=days,
        models=models,
        totals=sum((model.totals for model in models), ZERO),
        daily_totals=tuple(
            sum((model.daily[index] for model in models), ZERO) for index in range(len(days))
        ),
        complete=complete,
    )


# ── reading ─────────────────────────────────────────────────────────────────────────────────

_TTL_OK = 60.0
_TTL_FAILED = 15.0
# A re-check inside this many seconds of the last reading is served that reading: two people
# pressing Reload together make one round trip to LiteLLM, not two.
_MIN_RECHECK = 5.0
_MAX_CACHED = 16

_clock: Callable[[], float] = time.monotonic


@dataclass(frozen=True)
class Reading:
    window: Window
    fetched_at: datetime
    problem: Problem | None = None
    database: str = ""  # LiteLLM's word for its database ("connected"), when it said one
    report: UsageReport | None = None

    @property
    def available(self) -> bool:
        return self.report is not None


_Key = tuple[str, date, date]
_cache: dict[_Key, tuple[float, Reading]] = {}
_inflight: dict[_Key, asyncio.Task[Reading]] = {}


def reset_cache() -> None:
    _cache.clear()
    _inflight.clear()


def _store(key: _Key, reading: Reading) -> Reading:
    _cache[key] = (_clock(), reading)
    while len(_cache) > _MAX_CACHED:
        _cache.pop(min(_cache, key=lambda k: _cache[k][0]))
    return reading


async def _fetch(
    settings: Settings, window: Window, transport: httpx.AsyncBaseTransport | None
) -> Reading:
    key: _Key = (settings.litellm_url, window.start, window.end)
    secrets = litellm_secrets(settings.papaia_config_dir)
    if not secrets.master_key:
        # Nothing can be read without the key, so nobody is asked.
        return _store(key, Reading(window, datetime.now(UTC), problem=Problem(NO_KEY)))
    client = LitellmClient(
        settings.litellm_url,
        secrets.master_key,
        verify=tls_verify(settings.ssl_cert_file),
        transport=transport,
    )
    try:
        health, info, daily = await asyncio.gather(
            client.health(),
            client.model_info(),
            client.daily_activity(window.start, window.end),
            return_exceptions=True,
        )
    finally:
        await client.aclose()
    now = datetime.now(UTC)
    database = str(health.get("db") or "") if isinstance(health, dict) else ""
    for outcome in (info, daily):
        if isinstance(outcome, LitellmError):
            return _store(key, Reading(window, now, problem=outcome.problem, database=database))
        if isinstance(outcome, BaseException):
            raise outcome
    assert isinstance(info, list)
    assert isinstance(daily, tuple)
    results, complete = daily
    try:
        report = build_report(
            window, parse_catalog(info), parse_usage(results, window), complete=complete
        )
    except (KeyError, TypeError, ValueError, AttributeError):
        return _store(key, Reading(window, now, problem=Problem(UNREADABLE), database=database))
    return _store(key, Reading(window, now, database=database, report=report))


async def load_reading(
    settings: Settings,
    window: Window,
    *,
    fresh: bool = False,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Reading:
    """The reading for `window`, from the cache while it is young.

    `fresh` asks for a new one but is folded into a reading younger than a few seconds. A
    reading in flight is shared by everyone who asks for it meanwhile.
    """
    key: _Key = (settings.litellm_url, window.start, window.end)
    hit = _cache.get(key)
    if hit is not None:
        age = _clock() - hit[0]
        ttl = _TTL_OK if hit[1].available else _TTL_FAILED
        if age < ttl and (not fresh or age < _MIN_RECHECK):
            return hit[1]
    task = _inflight.get(key)
    if task is None:
        task = asyncio.ensure_future(_fetch(settings, window, transport))
        _inflight[key] = task

        def _forget(done: asyncio.Task[Reading], key: _Key = key) -> None:
            if _inflight.get(key) is done:
                del _inflight[key]

        task.add_done_callback(_forget)
    return await asyncio.shield(task)


# ── words ───────────────────────────────────────────────────────────────────────────────────

_MODE_LABELS = {
    "chat": N_("Chat"),
    "embedding": N_("Embedding"),
    "completion": N_("Completion"),
    "image_generation": N_("Image generation"),
    "audio_transcription": N_("Transcription"),
    "audio_speech": N_("Speech"),
    "rerank": N_("Rerank"),
    "moderation": N_("Moderation"),
}


def mode_label(mode: str) -> str:
    """A model's type in the reader's language; a type this list does not know is shown as is."""
    label = _MODE_LABELS.get(mode)
    return gettext(label) if label else mode.replace("_", " ").capitalize()
