"""Request guards on the stack routes.

The handlers themselves fork papaia-ctl or start a container, so what is tested
here is the layer in front of that: the two checks every request passes before
anything is dispatched, and the status codes they map onto. The allowlist they
delegate to is covered in `test_ctl.py`.
"""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from app.routers.api_stack import GROUP_ACTIONS, _check_clean_up, _flag

ALLOWED = {
    "keycloak": frozenset({"keycloak"}),
    "librechat-websearch": frozenset({"firecrawl", "searxng"}),
    "manager": frozenset({"manager"}),
}


def test_the_group_actions_are_the_three_the_ui_offers() -> None:
    assert set(GROUP_ACTIONS) == {"start", "stop", "restart"}


def test_a_valid_selection_becomes_the_profiles_flag() -> None:
    assert _flag(["keycloak"], ALLOWED) == "--profiles=keycloak"


@pytest.mark.parametrize("groups", [["manager"], ["nope"], [], ["keycloak", "manager"]])
def test_a_refused_selection_is_a_400_not_a_500(groups: list[str]) -> None:
    # The caller asked for something this deployment cannot do; that is a bad
    # request, not a broken server, and the message says which name was at fault.
    with pytest.raises(HTTPException) as exc:
        _flag(groups, ALLOWED)
    assert exc.value.status_code == 400


@pytest.mark.parametrize("action", ["stop", "restart"])
def test_clean_up_is_accepted_wherever_something_is_stopped(action: str) -> None:
    # On a restart it attaches to the stop half and makes the operation a full
    # recreate, which is a choice the operator gets to make either way.
    _check_clean_up(action, clean_up=True)


def test_clean_up_is_refused_on_a_start() -> None:
    # Ignoring it would confirm an operation that did not happen: the caller
    # asked for the containers to be removed and would be told it worked.
    with pytest.raises(HTTPException) as exc:
        _check_clean_up("start", clean_up=True)
    assert exc.value.status_code == 400
    assert "needs something to stop" in exc.value.detail


@pytest.mark.parametrize("action", ["start", "stop", "restart"])
def test_omitting_clean_up_is_always_fine(action: str) -> None:
    _check_clean_up(action, clean_up=False)
