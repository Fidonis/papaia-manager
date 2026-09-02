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

from app.core.upgrade import Gate, GateResult, UpgradeCheck
from app.routers.api_upgrade import UpgradeBody, _check_force, _check_gate


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
