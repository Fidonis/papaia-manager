"""A reading taken for everybody is put into the language of whoever looks at it.

The host check, Docker's disk use and the mail relay's state are each measured once and shared.
The account that happened to start a run may read another language than the one that looks at
the result a moment later.
"""
from __future__ import annotations

import json
import time
from collections.abc import Iterator

import pytest

from app.core import docker_usage, host_health
from app.core.mail import status as mail_status
from app.i18n import use_locale

_NO_OUTPUT = "doctor exited with status 3 and no output"
_NO_OUTPUT_DE = "doctor wurde mit Status 3 und ohne Ausgabe beendet"


@pytest.fixture(autouse=True)
def _clean() -> Iterator[None]:
    host_health.reset_cache()
    mail_status.reset_cache()
    yield
    host_health.reset_cache()
    mail_status.reset_cache()


def test_a_host_reading_without_output_is_worded_again_for_another_language() -> None:
    with use_locale("en"):
        health = host_health.unavailable(host_health._(host_health._TOO_OLD))
    host_health._cache = host_health._Entry(
        at=time.monotonic(),
        stale_after=60.0,
        health=health,
        locale="en",
        reason=(host_health._TOO_OLD, {}),
    )

    with use_locale("en"):
        in_english = host_health.cached_host_health()
    with use_locale("de"):
        in_german = host_health.cached_host_health()

    assert in_english is not None and in_german is not None
    assert in_english.reason.startswith("This papAIa core does not provide doctor yet")
    assert in_german.reason.startswith("Dieser papAIa-Core bietet doctor noch nicht")
    assert not in_german.available


def test_a_host_reading_is_parsed_again_from_what_doctor_printed() -> None:
    document = json.dumps(
        {
            "schema_version": 1,
            "checks": [
                {
                    "name": "memory",
                    "status": "pass",
                    "summary": "",
                    "details": {"total_bytes": 8 * 1024**3, "available_bytes": 4 * 1024**3},
                }
            ],
        }
    )
    with use_locale("en"):
        health = host_health.parse_doctor(document)
    host_health._cache = host_health._Entry(
        at=time.monotonic(), stale_after=60.0, health=health, out=document, locale="en"
    )

    with use_locale("de"):
        in_german = host_health.cached_host_health()

    assert in_german is not None
    memory = next(check for check in in_german.checks if "memory" in check.key)
    assert "verfügbar" in memory.measure
    assert "available" not in memory.measure


def test_a_docker_usage_reason_is_worded_again_for_another_language() -> None:
    with use_locale("en"):
        words = docker_usage.gettext(docker_usage._NO_OUTPUT) % {"code": 3}
        usage = docker_usage.unavailable(words)
    entry = docker_usage._Entry(
        at=time.monotonic(),
        usage=usage,
        locale="en",
        reason=(docker_usage._NO_OUTPUT, {"code": 3}),
    )

    with use_locale("en"):
        assert docker_usage._in_current_language(entry).reason == _NO_OUTPUT
    with use_locale("de"):
        assert docker_usage._in_current_language(entry).reason != _NO_OUTPUT
        assert "Status 3" in docker_usage._in_current_language(entry).reason


def test_a_mail_status_reason_is_worded_again_for_another_language() -> None:
    with use_locale("en"):
        status = mail_status.unavailable(mail_status._(mail_status._NO_OUTPUT) % {"code": 3})
    entry = mail_status._Entry(
        at=time.monotonic(),
        status=status,
        locale="en",
        reason=(mail_status._NO_OUTPUT, {"code": 3}),
    )

    with use_locale("en"):
        assert mail_status._in_current_language(entry).reason == _NO_OUTPUT
    with use_locale("de"):
        assert "Status 3" in mail_status._in_current_language(entry).reason
        assert mail_status._in_current_language(entry).reason != _NO_OUTPUT


def test_a_reading_in_the_language_of_the_request_is_served_as_it_is() -> None:
    with use_locale("de"):
        health = host_health.unavailable("ein Grund")
    entry = host_health._Entry(
        at=time.monotonic(), stale_after=60.0, health=health, locale="de", reason=None
    )

    with use_locale("de"):
        assert host_health._in_current_language(entry) is health
