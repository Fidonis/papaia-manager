"""The widget payload: what is accepted, and the many things that are refused.

The payload is the one thing a widget gives the dashboard, and the dashboard draws it with
templates of its own. So the contract is the boundary that keeps a widget from putting
anything on the page that it was not meant to, and most of what is tested here is refusals.
"""

from __future__ import annotations

import copy
import math
from typing import Any

import pytest

from app.core.widgets.model import (
    MAX_LIST_ITEMS,
    MAX_PAYLOAD_BYTES,
    PayloadError,
    ProgressPayload,
    State,
    StatPayload,
    StatusPayload,
    parse_payload,
)
from app.core.widgets.model import ListPayload as ListPayloadModel


def _stat(**data: Any) -> dict[str, Any]:
    return {
        "widget_api": 1,
        "view": "stat",
        "state": "ok",
        "data": {"value": "3", "unit": "days", "label": "Last backup", **data},
    }


# ---------------------------------------------------------------------------
# What is accepted
# ---------------------------------------------------------------------------


def test_a_stat_is_accepted() -> None:
    payload = parse_payload(_stat())

    assert isinstance(payload, StatPayload)
    assert (payload.state, payload.data.value, payload.data.unit) == (State.OK, "3", "days")


def test_a_status_is_accepted() -> None:
    payload = parse_payload(
        {
            "widget_api": 1,
            "view": "status",
            "state": "warn",
            "data": {"message": "1.5.0", "label": "Update available"},
        }
    )

    assert isinstance(payload, StatusPayload) and payload.state is State.WARN


def test_a_progress_is_accepted_and_knows_its_fill() -> None:
    payload = parse_payload(
        {
            "widget_api": 1,
            "view": "progress",
            "state": "ok",
            "data": {"value": 62.5, "max": 100, "unit": "%", "label": "Config disk"},
        }
    )

    assert isinstance(payload, ProgressPayload) and payload.data.percent == 62.5


def test_a_list_is_accepted() -> None:
    payload = parse_payload(
        {
            "widget_api": 1,
            "view": "list",
            "state": "ok",
            "data": {
                "items": [
                    {"label": "keycloak", "value": "up", "state": "ok", "href": "/services"},
                    {"label": "litellm", "state": "critical"},
                ]
            },
        }
    )

    assert isinstance(payload, ListPayloadModel)
    assert [i.label for i in payload.data.items] == ["keycloak", "litellm"]


def test_a_link_to_a_page_or_to_an_address_is_accepted() -> None:
    for href in ("/backup", "https://grafana.test/d/1"):
        document = _stat()
        document["link"] = {"label": "Open", "href": href}
        assert parse_payload(document).link is not None


def test_text_that_only_contains_a_less_than_sign_is_text() -> None:
    assert parse_payload(_stat(value="<1", label="< 5 minutes ago")).data.value == "<1"


def test_fields_the_manager_does_not_know_are_dropped() -> None:
    document = _stat()
    document["colour"] = "red"
    document["data"]["extra"] = "ignored"

    payload = parse_payload(document)

    assert not hasattr(payload, "colour")


def test_text_is_trimmed() -> None:
    assert parse_payload(_stat(label="  padded  ")).data.label == "padded"


# ---------------------------------------------------------------------------
# What is refused
# ---------------------------------------------------------------------------


def test_a_document_that_is_not_an_object_is_refused() -> None:
    with pytest.raises(PayloadError):
        parse_payload(["not", "an", "object"])


def test_another_generation_is_refused_by_name() -> None:
    document = _stat()
    document["widget_api"] = 2

    with pytest.raises(PayloadError, match="widget_api 2 is not supported"):
        parse_payload(document)


def test_a_missing_generation_is_refused() -> None:
    document = _stat()
    del document["widget_api"]

    with pytest.raises(PayloadError, match="widget_api"):
        parse_payload(document)


def test_a_view_the_manager_does_not_have_is_refused() -> None:
    document = _stat()
    document["view"] = "html"

    with pytest.raises(PayloadError):
        parse_payload(document)


def test_a_state_the_manager_does_not_have_is_refused() -> None:
    document = _stat()
    document["state"] = "purple"

    with pytest.raises(PayloadError, match="state"):
        parse_payload(document)


@pytest.mark.parametrize(
    "text",
    [
        "<script>alert(1)</script>",
        "<b>bold</b>",
        "<img src=x onerror=y>",
        "a </div> b",
        "<!-- note -->",
        "<?php",
    ],
)
def test_text_that_looks_like_markup_is_refused(text: str) -> None:
    with pytest.raises(PayloadError, match="plain text"):
        parse_payload(_stat(label=text))


def test_markup_is_refused_in_every_text_field() -> None:
    for field in ("value", "unit", "label"):
        with pytest.raises(PayloadError):
            parse_payload(_stat(**{field: "<b>x</b>"}))


def test_a_control_character_is_refused() -> None:
    with pytest.raises(PayloadError, match="control characters"):
        parse_payload(_stat(label="line one\nline two"))


def test_text_longer_than_its_limit_is_refused() -> None:
    with pytest.raises(PayloadError, match="label"):
        parse_payload(_stat(label="x" * 121))
    with pytest.raises(PayloadError, match="value"):
        parse_payload(_stat(value="x" * 41))


@pytest.mark.parametrize(
    "href",
    [
        "javascript:alert(1)",
        "data:text/html,<script>",
        "ftp://files.test",
        "//evil.test/x",
        "/\\evil.test/x",
        "{{PAPAIA_HOST}}/x",
        "",
        "x" * 501,
    ],
)
def test_a_link_that_is_not_safe_is_refused(href: str) -> None:
    document = _stat()
    document["link"] = {"label": "Open", "href": href}

    with pytest.raises(PayloadError):
        parse_payload(document)


def test_an_unsafe_link_in_a_list_item_is_refused() -> None:
    with pytest.raises(PayloadError):
        parse_payload(
            {
                "widget_api": 1,
                "view": "list",
                "state": "ok",
                "data": {"items": [{"label": "x", "href": "javascript:alert(1)"}]},
            }
        )


def test_a_list_with_too_many_items_is_refused() -> None:
    items = [{"label": str(i)} for i in range(MAX_LIST_ITEMS + 1)]

    with pytest.raises(PayloadError):
        parse_payload({"widget_api": 1, "view": "list", "state": "ok", "data": {"items": items}})


@pytest.mark.parametrize(
    "data",
    [
        {"value": -1, "max": 100},
        {"value": 101, "max": 100},
        {"value": 5, "max": 0},
        {"value": math.inf, "max": 100},
        {"value": math.nan, "max": 100},
        {"value": "ten", "max": 100},
    ],
)
def test_a_progress_that_makes_no_sense_is_refused(data: dict[str, Any]) -> None:
    with pytest.raises(PayloadError):
        parse_payload({"widget_api": 1, "view": "progress", "state": "ok", "data": data})


def test_a_document_larger_than_the_limit_is_refused_before_it_is_parsed() -> None:
    document = _stat()
    document["padding"] = "x" * (MAX_PAYLOAD_BYTES + 1)

    with pytest.raises(PayloadError, match="larger than"):
        parse_payload(document)


def test_a_document_that_is_not_json_is_refused() -> None:
    document = _stat()
    document["data"]["cycle"] = document  # a cycle cannot be serialised

    with pytest.raises(PayloadError, match="JSON"):
        parse_payload(document)


def test_an_error_names_the_field_and_never_repeats_the_value() -> None:
    secret = "<b>hunter2-should-not-be-echoed</b>"

    with pytest.raises(PayloadError) as caught:
        parse_payload(_stat(label=secret))

    assert "data.label" in str(caught.value)
    assert "hunter2" not in str(caught.value)


def test_the_document_the_caller_passed_is_not_changed() -> None:
    document = _stat()
    before = copy.deepcopy(document)

    parse_payload(document)

    assert document == before
