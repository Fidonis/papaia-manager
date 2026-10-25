"""The geometry and number formats of the Models page charts. Pure functions, no page."""
from __future__ import annotations

from datetime import date

import pytest

from app.core import charts
from app.i18n import use_locale


@pytest.mark.parametrize(
    ("value", "top"),
    [
        (0, 1.0),
        (-5, 1.0),
        (0.3, 0.3),
        (1, 1.0),
        (9.5, 10.0),
        (11, 12.0),
        (1000, 1000.0),
        (1234, 1600.0),
        (5001, 6000.0),
        (7_300_000, 8_000_000.0),
    ],
)
def test_the_top_of_a_chart_is_the_next_round_number(value: float, top: float) -> None:
    assert charts.nice_top(value) == pytest.approx(top)


def test_segments_are_a_share_of_the_top_and_stack_to_the_days_total() -> None:
    columns = [[(0, 30.0, False), (1, 10.0, False)], [], [(None, 80.0, False)]]

    chart = charts.bar_chart(columns, ["a", "b", "c"], str)

    assert chart.top == 80.0  # the biggest day, which is already a round number
    assert [sum(s.pct for s in column.segments) for column in chart.columns] == [50.0, 0, 100.0]
    assert [s.slot for s in chart.columns[0].segments] == [0, 1]
    assert chart.columns[1].segments == ()
    assert chart.columns[2].segments[0].slot is None
    assert not chart.empty


def test_the_axis_has_two_ticks_at_half_and_full_height_worded_by_the_caller() -> None:
    chart = charts.bar_chart([[(0, 1200.0, False)]], ["a"], charts.format_count)

    assert [(tick.pct, tick.label) for tick in chart.ticks] == [(50.0, "600"), (100.0, "1.2k")]


def test_a_chart_of_nothing_is_empty_and_still_has_a_scale() -> None:
    chart = charts.bar_chart([[], [(0, 0.0, False)]], ["a", "b"], str)

    assert chart.empty and chart.top == 1.0
    assert all(column.segments == () for column in chart.columns)


def test_a_tiny_value_next_to_a_huge_one_is_still_drawn() -> None:
    chart = charts.bar_chart([[(0, 1_000_000.0, False)], [(1, 1.0, False)]], ["a", "b"], str)

    assert chart.columns[1].segments[0].pct >= 0.5


def test_the_muted_half_of_a_stack_is_kept() -> None:
    chart = charts.bar_chart([[(2, 5.0, False), (2, 3.0, True)]], ["a"], str)

    assert [s.muted for s in chart.columns[0].segments] == [False, True]


@pytest.mark.parametrize(("days", "labelled"), [(1, 1), (7, 7), (8, 8), (30, 6), (90, 6), (365, 6)])
def test_the_axis_names_about_six_days_however_many_there_are(days: int, labelled: int) -> None:
    chart = charts.bar_chart([[(0, 1.0, False)]] * days, [f"d{i}" for i in range(days)], str)

    assert sum(1 for column in chart.columns if column.label) == labelled
    assert chart.columns[0].label == "d0"


def test_a_narrow_screen_gets_a_short_label_for_the_same_days() -> None:
    days = 30
    long = [f"Oct {i}" for i in range(days)]
    short = [str(i) for i in range(days)]

    chart = charts.bar_chart([[(0, 1.0, False)]] * days, long, str, short)

    shown = [column for column in chart.columns if column.label]
    assert len(shown) == 6
    assert [column.short for column in shown] == [
        column.label.removeprefix("Oct ") for column in shown
    ]
    assert all(not column.short for column in chart.columns if not column.label)


def test_without_short_labels_the_long_ones_are_used() -> None:
    chart = charts.bar_chart([[(0, 1.0, False)]] * 2, ["a", "b"], str)

    assert [column.short for column in chart.columns] == ["a", "b"]


def test_a_day_number_is_the_day_of_the_month_without_a_zero() -> None:
    assert charts.day_number(date(2026, 10, 8)) == "8"
    assert charts.day_number(date(2026, 10, 24)) == "24"


def test_a_sparkline_runs_from_the_left_edge_to_the_right_with_the_peak_at_the_top() -> None:
    raw = charts.sparkline_points([0, 5, 10], 72, 20).split()
    points = [tuple(map(float, p.split(","))) for p in raw]

    assert points[0] == (1.5, 18.5)
    assert points[1] == (36.0, 10.0)
    assert points[2] == (70.5, 1.5)


@pytest.mark.parametrize("values", [[], [0, 0, 0], [0.0]])
def test_there_is_no_sparkline_for_nothing(values: list[float]) -> None:
    assert charts.sparkline_points(values, 72, 20) == ""


def test_one_value_is_a_single_point() -> None:
    assert len(charts.sparkline_points([4], 72, 20).split()) == 1


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0, "0"),
        (7, "7"),
        (950, "950"),
        (1000, "1k"),
        (1234, "1.2k"),
        (9990, "10k"),
        (10_400, "10k"),
        (340_000, "340k"),
        (999_499, "999k"),
        (999_500, "1M"),
        (999_999, "1M"),
        (1_234_567, "1.23M"),
        (12_400_000, "12.4M"),
        (999_949_999, "999.9M"),
        (999_950_000, "1B"),
        (3_100_000_000, "3.1B"),
    ],
)
def test_counts_are_short_and_never_say_1000_of_a_unit(value: float, text: str) -> None:
    assert charts.format_count(value) == text


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (0, "$0.00"),
        (0.00001, "<$0.0001"),
        (0.0013, "$0.0013"),
        (0.0021, "$0.0021"),
        (0.03, "$0.03"),
        (12, "$12.00"),
        (12.4, "$12.40"),
        (1234.5, "$1,234.50"),
    ],
)
def test_dollars_keep_cents_and_show_the_sub_cent_sums_that_litellm_records(
    value: float, text: str
) -> None:
    assert charts.format_usd(value) == text


@pytest.mark.parametrize(
    ("value", "text"),
    [(0, "$0"), (0.5, "$0.50"), (2.5, "$2.50"), (1200, "$1,200"), (0.002, "$0.0020")],
)
def test_an_axis_tick_in_dollars_drops_cents_that_are_zero(value: float, text: str) -> None:
    assert charts.format_usd_axis(value) == text


@pytest.mark.parametrize(
    ("value", "text"), [(None, "–"), (0.15, "$0.15"), (3, "$3"), (15.0, "$15")]
)
def test_a_price_per_million_tokens(value: float | None, text: str) -> None:
    assert charts.format_price(value) == text


def test_a_day_is_named_in_the_language_of_the_reader() -> None:
    day = date(2026, 10, 24)

    assert (charts.day_label(day), charts.day_title(day)) == ("Oct 24", "Sat, Oct 24")
    with use_locale("de"):
        assert (charts.day_label(day), charts.day_title(day)) == ("24. Okt.", "Sa., 24. Okt.")


def test_numbers_use_the_separators_of_the_language() -> None:
    with use_locale("de"):
        assert charts.format_count(1234) == "1,2k"
        assert charts.format_usd(1234.5) == "$1.234,50"
        assert charts.format_usd(0.0021) == "$0,0021"
        assert charts.format_price(0.15) == "$0,15"
