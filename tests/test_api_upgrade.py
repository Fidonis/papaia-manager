"""Request guards on the upgrade routes.

The handler itself starts a container that takes the whole stack down, so what
is tested here is everything in front of that: the checks a request has to pass
before `docker run` is reached, and the status codes they map onto. The argv
that container receives is covered in `test_runner.py`, and the allowlist behind
the check half in `test_ctl.py`.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.core.upgrade import (
    CheckoutState,
    Gate,
    GateResult,
    Migration,
    UpgradeCheck,
    VersionState,
)
from app.routers.api_upgrade import UpgradeBody, _check_force, _check_gate, _check_to_dict
from app.templating import templates


def _check(*, passed: bool = True, error: bool = False) -> UpgradeCheck:
    status = "ERROR" if error else "INCOMPATIBLE"
    results = () if passed else (GateResult(name="n8n", status=status, reason="needs api 3"),)
    return UpgradeCheck(
        current="1.0.0",
        target="1.2.0",
        tag="v1.2.0",
        status="ok",
        gate=Gate(passed=passed, results=results),
    )


def _body(**kwargs: object) -> UpgradeBody:
    return UpgradeBody(version="1.2.0", **kwargs)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# --force
# ---------------------------------------------------------------------------


def test_force_is_refused_when_the_gate_passed() -> None:
    # Same reasoning as `clean_up` on a start: confirming an override that
    # overrode nothing is worse than refusing it.
    with pytest.raises(HTTPException) as exc:
        _check_force(_body(force=True), _check(passed=True))
    assert exc.value.status_code == 400
    assert "nothing for force to override" in exc.value.detail


def test_force_is_refused_against_a_malformed_manifest() -> None:
    # `compat.gate` returns 2 for an ERROR whatever the flag says, so accepting
    # it here would promise something papaia-ctl cannot deliver -- and the
    # promise would be discovered after the stack is down.
    with pytest.raises(HTTPException) as exc:
        _check_force(_body(force=True), _check(passed=False, error=True))
    assert exc.value.status_code == 400
    assert "ERROR" in exc.value.detail


def test_force_is_accepted_against_an_incompatibility() -> None:
    _check_force(_body(force=True), _check(passed=False))


@pytest.mark.parametrize("check", [_check(passed=True), _check(passed=False)])
def test_omitting_force_is_always_fine(check: UpgradeCheck) -> None:
    _check_force(_body(force=False), check)


# ---------------------------------------------------------------------------
# The gate itself
# ---------------------------------------------------------------------------


def test_a_failed_gate_blocks_the_upgrade() -> None:
    with pytest.raises(HTTPException) as exc:
        _check_gate(_body(force=False), _check(passed=False))
    assert exc.value.status_code == 409
    # The names matter: "an add-on is incompatible" sends the operator looking.
    assert "n8n" in exc.value.detail


def test_a_failed_gate_is_passable_with_force() -> None:
    _check_gate(_body(force=True), _check(passed=False))


def test_a_passing_gate_needs_nothing() -> None:
    _check_gate(_body(force=False), _check(passed=True))


# ---------------------------------------------------------------------------
# The check as a precondition
# ---------------------------------------------------------------------------


def test_a_check_result_carries_the_version_it_was_run_for() -> None:
    # The route refuses a version the cached check does not name, and this is
    # the field it compares against. Without it an operator could confirm one
    # release's migration list and install another's.
    assert _check().target == "1.2.0"


def test_an_up_to_date_check_has_nothing_to_install() -> None:
    assert UpgradeCheck(current="1.2.0", target="1.2.0", status="up-to-date").up_to_date


# ---------------------------------------------------------------------------
# A check that could not reach the remote
# ---------------------------------------------------------------------------
#
# The page used to say "papAIa X is the newest release" in green and label the
# button "Up to date" when the fetch had failed. Nothing had been compared with
# the remote, so neither was true.

_FETCH_ERROR = "git fetch failed: fatal: could not read Username for 'https://github.com'"
_FETCH_HINT = "git -C /w/papaia fetch --tags origin   # on the host"


def _unreachable(*, up_to_date: bool = True) -> UpgradeCheck:
    return UpgradeCheck(
        current="1.1.0",
        target="1.1.0" if up_to_date else "1.2.0",
        tag="v1.1.0" if up_to_date else "v1.2.0",
        status="up-to-date" if up_to_date else "ok",
        available=[] if up_to_date else ["1.2.0"],
        migrations=[] if up_to_date else [Migration(id="1.2.0__x", version="1.2.0", kind="sh")],
        fetch_error=_FETCH_ERROR,
        fetch_hint=_FETCH_HINT,
        checked_at="2026-09-20T11:07:13+00:00",
    )


def _render_check(check: UpgradeCheck) -> str:
    return templates.env.get_template("partials/upgrade_check.html").render(check=check)


def _render_status(check: UpgradeCheck) -> str:
    return templates.env.get_template("partials/upgrade_status.html").render(
        version=VersionState(recorded="1.1.0", checkout="1.1.0"),
        checkout=CheckoutState(is_git=True, clean=True, tag="v1.1.0"),
        backup_dir=None,
        backup_dir_reachable=False,
        check=check,
    )


def test_the_api_carries_the_fetch_hint_beside_the_error() -> None:
    body = _check_to_dict(_unreachable())
    assert body["fetch_error"] == _FETCH_ERROR
    assert body["fetch_hint"] == _FETCH_HINT


def test_a_failed_fetch_is_a_warning_not_the_newest_release() -> None:
    html = _render_check(_unreachable())
    assert "Could not check for new releases" in html
    assert "is the newest release" not in html
    assert "could not read Username" in html
    assert "fetch --tags origin" in html


def test_a_reachable_remote_still_says_the_newest_release() -> None:
    check = _unreachable()
    check.fetch_error = ""
    check.fetch_hint = ""
    html = _render_check(check)
    assert "papAIa 1.1.0 is the newest release" in html
    assert "Could not check" not in html


def test_a_release_found_locally_still_warns_that_the_fetch_failed() -> None:
    # The tag was already in the checkout. It is on offer, but it may not be the
    # newest one, and the page has to say so.
    html = _render_check(_unreachable(up_to_date=False))
    assert "Could not check for new releases" in html
    assert "Migrations in this update" in html


def test_the_header_button_learns_the_check_failed() -> None:
    assert "checkFailed: true" in _render_status(_unreachable())


def test_a_reachable_remote_leaves_the_button_alone() -> None:
    check = _unreachable()
    check.fetch_error = ""
    assert "checkFailed: false" in _render_status(check)


def test_a_release_on_offer_is_not_a_failed_check() -> None:
    # The button says "Install 1.2.0" there; "Check failed" is only for the case
    # where it would otherwise claim there is nothing to install.
    assert "checkFailed: false" in _render_status(_unreachable(up_to_date=False))
