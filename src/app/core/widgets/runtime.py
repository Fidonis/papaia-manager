"""Reading a widget: one read at a time, remembered for its interval, and never an error.

A dashboard polls every widget on its own, from every open tab. Three things keep that
cheap and keep it from taking the dashboard down with it:

* A reading is remembered for the type's interval, however many people have a tab open.
* At most one read per type runs at a time. Whoever wants a reading while one is under
  way waits for that one instead of starting another, the way the host check does.
* A read that fails comes back as a *result* -- `unavailable`, or `stale` when an older
  good reading is still young enough to show -- and never as an exception. The card for
  a slow or broken source is a card; the other widgets are not affected.

Every reading, built-in or not, is parsed by `parse_payload` here. A provider that
returns something the contract does not allow is as unavailable as one that raised.

The cache is keyed by the widget type. A type has no per-tile settings yet, so two tiles
of the same type share one reading. A type that gets some must key on them as well.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass

from app.core.widgets.model import PayloadError, WidgetResult, parse_payload
from app.core.widgets.registry import WidgetContext, WidgetType, WidgetUnavailableError

logger = logging.getLogger(__name__)

# Longer than the slowest thing a provider waits for (the host check allows itself 30 s), so
# a provider reports its own failure and this is only the backstop for one that hangs.
READ_TIMEOUT_SECONDS = 40.0

# A failed reading is remembered for less than the interval, so a transient error clears
# itself soon without every poll turning into a retry.
FAILURE_TTL_SECONDS = 15.0

# The last good reading is shown as "stale" for this long after the reads start failing. Past
# it the card says it has nothing: an hour-old figure with a red dot would mislead.
STALE_MAX_SECONDS = 900.0

# Indirection for the tests, which move time instead of waiting for it.
_clock = time.monotonic


@dataclass(frozen=True)
class _Entry:
    at: float
    result: WidgetResult


_cache: dict[str, _Entry] = {}
_good: dict[str, _Entry] = {}
# The read in flight per type. Held at module level because the loop keeps only a weak
# reference to a task, and because the read belongs to the process, not to the request that
# happened to start it: a visitor who closes the tab must not cancel what the next one awaits.
_inflight: dict[str, asyncio.Task[WidgetResult]] = {}


def reset_cache() -> None:
    """Forget every reading and every read under way."""
    _cache.clear()
    _good.clear()
    _inflight.clear()


async def read(widget_type: WidgetType, context: WidgetContext) -> WidgetResult:
    """The widget's reading, from the cache while it is young, else a read awaited."""
    key = widget_type.id
    entry = _cache.get(key)
    if entry is not None:
        limit = float(widget_type.interval)
        if entry.result.status != "ok":
            limit = min(FAILURE_TTL_SECONDS, limit)
        if _clock() - entry.at < limit:
            return entry.result

    task = _inflight.get(key)
    if task is None or task.done():
        task = asyncio.get_running_loop().create_task(_measure(widget_type, context))
        _inflight[key] = task
    # Shielded: cancelling this request must not cancel the read the others wait for.
    return await asyncio.shield(task)


async def _measure(widget_type: WidgetType, context: WidgetContext) -> WidgetResult:
    """One read, stored. Never raises: a task nobody awaits would lose the error."""
    key = widget_type.id
    try:
        document = await asyncio.wait_for(widget_type.provider(context), READ_TIMEOUT_SECONDS)
        payload = parse_payload(document)
    except WidgetUnavailableError as exc:
        failure = str(exc) or "The widget has nothing to show."
    except PayloadError as exc:
        failure = f"The widget produced a document the dashboard cannot show ({exc})."
    except TimeoutError:
        failure = "Reading the widget took too long."
    except Exception:
        logger.exception("widget %s: unexpected failure", key)
        failure = "The widget could not be read."
    else:
        entry = _Entry(_clock(), WidgetResult("ok", payload, fetched_at=context.now))
        _cache[key] = entry
        _good[key] = entry
        return entry.result

    logger.warning("widget %s: %s", key, failure)
    result = _failed(key, failure)
    _cache[key] = _Entry(_clock(), result)
    return result


def _failed(key: str, failure: str) -> WidgetResult:
    """`stale` while the last good reading is young enough to show, else `unavailable`."""
    last = _good.get(key)
    if last is not None and _clock() - last.at <= STALE_MAX_SECONDS:
        return WidgetResult(
            "stale", last.result.payload, reason=failure, fetched_at=last.result.fetched_at
        )
    return WidgetResult("unavailable", reason=failure)
