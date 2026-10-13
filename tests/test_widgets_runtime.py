"""Reading a widget: how long a reading is kept, who shares it, and how a failure looks.

A dashboard polls every widget from every open tab, so what matters is not the happy path
but that a slow, broken or lying widget costs its own card and nothing else.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, cast

import pytest

from app.config import Settings
from app.core.widgets import runtime
from app.core.widgets.model import WidgetResult
from app.core.widgets.registry import WidgetContext, WidgetType, WidgetUnavailableError


class Clock:
    """Time that moves when the test says so."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> Iterator[Clock]:
    runtime.reset_cache()
    fake = Clock()
    monkeypatch.setattr(runtime, "_clock", fake)
    yield fake
    runtime.reset_cache()


def _context() -> WidgetContext:
    # The providers here never look at the settings.
    return WidgetContext(settings=cast(Settings, object()), now=datetime.now(tz=UTC))


def _document(value: str = "1") -> dict[str, Any]:
    return {
        "widget_api": 1,
        "view": "stat",
        "state": "ok",
        "data": {"value": value, "unit": "x", "label": "l"},
    }


class Provider:
    """A provider that counts its calls and does what it is told."""

    def __init__(self) -> None:
        self.calls = 0
        self.document: dict[str, Any] = _document()
        self.raises: BaseException | None = None
        self.delay = 0.0

    async def __call__(self, context: WidgetContext) -> dict[str, Any]:
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raises is not None:
            raise self.raises
        return self.document


def _type(provider: Provider, *, refresh: int = 60) -> WidgetType:
    return WidgetType(
        id="probe",
        name="Probe",
        description="",
        icon="tag",
        view="stat",
        provider=provider,
        refresh_seconds=refresh,
    )


def _value(result: WidgetResult) -> str:
    assert result.payload is not None
    return str(result.payload.data.value)  # type: ignore[union-attr]


# ---------------------------------------------------------------------------
# Keeping a reading
# ---------------------------------------------------------------------------


async def test_a_reading_is_kept_for_the_interval(clock: Clock) -> None:
    provider = Provider()
    widget = _type(provider, refresh=60)

    first = await runtime.read(widget, _context())
    clock.now += 59
    second = await runtime.read(widget, _context())

    assert first.status == second.status == "ok"
    assert provider.calls == 1


async def test_a_reading_is_taken_again_after_the_interval(clock: Clock) -> None:
    provider = Provider()
    widget = _type(provider, refresh=60)

    await runtime.read(widget, _context())
    provider.document = _document("2")
    clock.now += 61
    result = await runtime.read(widget, _context())

    assert provider.calls == 2 and _value(result) == "2"


async def test_the_interval_has_a_floor() -> None:
    assert _type(Provider(), refresh=1).interval == 15


async def test_the_interval_has_a_ceiling() -> None:
    assert _type(Provider(), refresh=999_999).interval == 3600


async def test_everyone_who_asks_while_a_read_is_under_way_shares_it(clock: Clock) -> None:
    provider = Provider()
    provider.delay = 0.05
    widget = _type(provider)

    results = await asyncio.gather(*(runtime.read(widget, _context()) for _ in range(10)))

    assert provider.calls == 1
    assert {r.status for r in results} == {"ok"}


async def test_cancelling_one_request_does_not_cancel_the_read_for_the_others(
    clock: Clock,
) -> None:
    provider = Provider()
    provider.delay = 0.05
    widget = _type(provider)

    impatient = asyncio.ensure_future(runtime.read(widget, _context()))
    patient = asyncio.ensure_future(runtime.read(widget, _context()))
    await asyncio.sleep(0.01)
    impatient.cancel()

    assert (await patient).status == "ok"
    assert provider.calls == 1


async def test_two_types_do_not_share_a_reading(clock: Clock) -> None:
    one, other = Provider(), Provider()
    first = _type(one)
    second = WidgetType(**{**first.__dict__, "id": "other", "provider": other})

    await runtime.read(first, _context())
    await runtime.read(second, _context())

    assert (one.calls, other.calls) == (1, 1)


# ---------------------------------------------------------------------------
# Failing
# ---------------------------------------------------------------------------


async def test_a_provider_that_says_why_not_gives_an_unavailable_card_with_the_reason(
    clock: Clock,
) -> None:
    provider = Provider()
    provider.raises = WidgetUnavailableError("The backup directory is not mounted.")

    result = await runtime.read(_type(provider), _context())

    assert (result.status, result.payload) == ("unavailable", None)
    assert result.reason == "The backup directory is not mounted."


async def test_an_unexpected_exception_is_not_shown_to_anyone(clock: Clock) -> None:
    provider = Provider()
    provider.raises = RuntimeError("password=hunter2 in /srv/secret/path")

    result = await runtime.read(_type(provider), _context())

    assert result.status == "unavailable"
    assert "hunter2" not in result.reason and "/srv" not in result.reason


async def test_a_document_the_contract_refuses_is_an_unavailable_card(clock: Clock) -> None:
    provider = Provider()
    provider.document = {**_document(), "view": "html"}

    result = await runtime.read(_type(provider), _context())

    assert result.status == "unavailable"
    assert "cannot show" in result.reason


async def test_markup_in_a_document_never_becomes_a_reading(clock: Clock) -> None:
    provider = Provider()
    provider.document = _document("<script>alert(1)</script>")

    result = await runtime.read(_type(provider), _context())

    assert (result.status, result.payload) == ("unavailable", None)


async def test_a_provider_that_hangs_is_given_up_on(
    clock: Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(runtime, "READ_TIMEOUT_SECONDS", 0.05)
    provider = Provider()
    provider.delay = 5

    result = await runtime.read(_type(provider), _context())

    assert result.status == "unavailable" and "too long" in result.reason


async def test_a_broken_widget_does_not_hold_up_another(clock: Clock) -> None:
    slow, quick = Provider(), Provider()
    slow.delay = 0.3
    first = _type(slow)
    second = WidgetType(**{**first.__dict__, "id": "quick", "provider": quick})

    started = asyncio.get_running_loop().time()
    slow_task = asyncio.ensure_future(runtime.read(first, _context()))
    result = await runtime.read(second, _context())
    elapsed = asyncio.get_running_loop().time() - started
    await slow_task

    assert result.status == "ok" and elapsed < 0.2


async def test_a_failure_is_remembered_for_less_time_than_a_success(clock: Clock) -> None:
    provider = Provider()
    provider.raises = WidgetUnavailableError("down")
    widget = _type(provider, refresh=600)

    await runtime.read(widget, _context())
    await runtime.read(widget, _context())
    assert provider.calls == 1

    clock.now += runtime.FAILURE_TTL_SECONDS + 1
    await runtime.read(widget, _context())
    assert provider.calls == 2


async def test_a_failure_that_clears_is_gone_at_the_next_read(clock: Clock) -> None:
    provider = Provider()
    provider.raises = WidgetUnavailableError("down")
    widget = _type(provider)
    assert (await runtime.read(widget, _context())).status == "unavailable"

    provider.raises = None
    clock.now += runtime.FAILURE_TTL_SECONDS + 1

    assert (await runtime.read(widget, _context())).status == "ok"


# ---------------------------------------------------------------------------
# The last good reading
# ---------------------------------------------------------------------------


async def test_the_last_good_reading_is_shown_as_stale_when_the_next_one_fails(
    clock: Clock,
) -> None:
    provider = Provider()
    widget = _type(provider, refresh=60)
    await runtime.read(widget, _context())

    provider.raises = WidgetUnavailableError("down")
    clock.now += 61
    result = await runtime.read(widget, _context())

    assert result.status == "stale"
    assert _value(result) == "1" and result.reason == "down" and result.fetched_at is not None


async def test_a_reading_that_is_too_old_is_not_shown_as_stale(clock: Clock) -> None:
    provider = Provider()
    widget = _type(provider, refresh=60)
    await runtime.read(widget, _context())

    provider.raises = WidgetUnavailableError("down")
    clock.now += runtime.STALE_MAX_SECONDS + 1
    result = await runtime.read(widget, _context())

    assert (result.status, result.payload) == ("unavailable", None)


async def test_a_stale_card_recovers_with_the_next_good_reading(clock: Clock) -> None:
    provider = Provider()
    widget = _type(provider, refresh=60)
    await runtime.read(widget, _context())
    provider.raises = WidgetUnavailableError("down")
    clock.now += 61
    assert (await runtime.read(widget, _context())).status == "stale"

    provider.raises = None
    provider.document = _document("9")
    clock.now += runtime.FAILURE_TTL_SECONDS + 1
    result = await runtime.read(widget, _context())

    assert result.status == "ok" and _value(result) == "9"
