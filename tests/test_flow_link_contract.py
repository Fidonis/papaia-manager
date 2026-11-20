"""The manager against the core's real installer: what it reads back is what the core prints.

`app.core.flow_link.outcome` reads the outcome of an add-on's Flow link from the lines of
`papaia-ctl addon install`, and the manager passes `--approve-flow-link` and `PAPAIA_FLOW_TOKEN`.
These tests run the core's own `flow_link.link_at_install` with a stand-in of Flow for every
outcome and compare, and check the flag and the variable against the core's installer.

They need the core's checkout and are skipped without it. Point `PAPAIA_SRC` at a checkout of
`Fidonis/papaia` whose `tools/lib/flow_link.py` exists (the approval of a Flow link at install).
The core's file is read and executed in memory, so nothing is written to that checkout.
"""
from __future__ import annotations

import io
import os
import sys
import types
from pathlib import Path
from typing import Any

import pytest

_SRC = os.environ.get("PAPAIA_SRC", "")
_CORE = Path(_SRC) / "tools" / "lib" / "flow_link.py"

pytestmark = pytest.mark.skipif(
    not (_SRC and _CORE.is_file()),
    reason="PAPAIA_SRC does not point at a checkout of the core with tools/lib/flow_link.py",
)

from app.core import flow_link  # noqa: E402

_NAME = "archive"
_MANIFEST: dict[str, Any] = {
    "name": _NAME,
    "version": "1.4.0",
    "flow": {"actions": [{"name": "find", "via": "tools", "server": "s", "tool": "t"}]},
}


def _core() -> types.ModuleType:
    module = types.ModuleType("papaia_core_flow_link")
    sys.modules[module.__name__] = module
    try:
        exec(compile(_CORE.read_text(encoding="utf-8"), str(_CORE), "exec"), module.__dict__)
    finally:
        sys.modules.pop(module.__name__, None)
    return module


class _Flow:
    def __init__(self, core: types.ModuleType, case: str) -> None:
        self.core = core
        self.case = case

    def show(self, addon: str) -> dict[str, Any] | None:
        if self.case == "error":
            raise self.core.FlowError("the API container of the workflow module is not running")
        if self.case == "not_read":
            return None
        return {
            "addon": addon,
            "addon_version": "1.4.0",
            "declaration_digest": "sha256:abc",
            "pending": self.case != "unchanged",
            "state": "active",
            "declaration": _MANIFEST["flow"],
        }

    def approve(self, addon: str, digest: str, version: str, reason: str) -> dict[str, Any]:
        if self.case == "approved":
            return {"approved_at_install": True}
        return {
            "approved_at_install": False,
            "reasons": ["data_class:find", "egress:tag"],
            "widened": ["action_added"],
        }


@pytest.mark.parametrize(
    ("case", "approve", "token"),
    [
        ("approved", True, "t"),
        ("proposal", True, "t"),
        ("not_approved", False, "t"),
        ("error", True, "t"),
        ("not_read", True, "t"),
        ("unchanged", True, "t"),
        ("no_token", True, ""),
    ],
)
def test_the_manager_reads_each_outcome_the_core_prints(
    case: str, approve: bool, token: str
) -> None:
    core = _core()
    out = io.StringIO()

    returned = core.link_at_install(
        _NAME,
        _MANIFEST,
        lambda _t: _Flow(core, case),
        approve=approve,
        ask=None,
        token=token,
        out=out,
        wait_s=0,
        poll_s=0,
        sleep=lambda _s: None,
    )

    expected = "not_approved" if returned == "refused" else returned
    read, reasons = flow_link.outcome(_NAME, out.getvalue().splitlines())
    assert read == expected, out.getvalue()
    if case == "proposal":
        assert reasons == ["data_class:find", "egress:tag"]


def test_the_core_takes_the_flag_and_the_variable_the_manager_passes() -> None:
    core = _core()
    installer = Path(_SRC) / "tools" / "lib" / "sh" / "addon.sh"

    assert core.TOKEN_ENV == flow_link.TOKEN_ENV
    assert flow_link.APPROVE_FLAG in installer.read_text(encoding="utf-8")
    assert core.FLOW_ADDON == flow_link.FLOW_ADDON
    assert core.SECTION == flow_link.SECTION
