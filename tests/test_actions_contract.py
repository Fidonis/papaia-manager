"""The manager's reading of an add-on's `actions` block against the core's
`compat.addon_actions`: a button is offered exactly for what papaia-ctl would run.

papaia-ctl refuses to install or start an add-on whose block is malformed, so the manager
offers no action for it. These tests need the core's checkout and are skipped without it:
point `PAPAIA_SRC` at a checkout of `Fidonis/papaia` that has `actions` (Fidonis/papaia#319).
Repeat them when either side's rules change.
"""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

from app.core.actions import addon_actions

_SRC = os.environ.get("PAPAIA_SRC", "")


def _core_has_actions() -> bool:
    source = Path(_SRC) / "tools" / "lib" / "compat.py"
    if not (_SRC and source.is_file()):
        return False
    return "def addon_actions" in source.read_text(encoding="utf-8")


pytestmark = pytest.mark.skipif(
    not _core_has_actions(),
    reason="PAPAIA_SRC does not point at a checkout of the core that has add-on actions",
)


@pytest.fixture(scope="module")
def compat() -> Iterator[ModuleType]:
    tools = str(Path(_SRC) / "tools")
    sys.path.insert(0, tools)
    try:
        yield importlib.import_module("lib.compat")
    finally:
        sys.path.remove(tools)


_JOB = {"service": "job"}

_MANIFESTS: list[dict[str, Any]] = [
    {},
    {"actions": None},
    {"actions": {}},
    {"actions": {"check": _JOB}},
    {"actions": {"reset": {**_JOB, "args": ["--reset"], "description": "Reset",
                           "destructive": True}}},
    {"actions": {"check": {**_JOB, "args": None, "description": None}}},
    {"actions": {"a" + "-x" * 40: _JOB}},
    {"actions": {"check": _JOB, "bad": {**_JOB, "args": "--reset"}}},
    {"actions": {"check": _JOB, "bad": {**_JOB, "args": [1]}}},
    {"actions": {"check": _JOB, "bad": {**_JOB, "description": 3}}},
    {"actions": {"check": _JOB, "bad": {**_JOB, "destructive": "yes"}}},
    {"actions": {"check": _JOB, "bad": {**_JOB, "destructive": 1}}},
    {"actions": {"check": _JOB, "bad": {"description": "no service"}}},
    {"actions": {"check": _JOB, "Bad": _JOB}},
    {"actions": {"check": _JOB, "1st": _JOB}},
    {"actions": {"check": _JOB, 3: _JOB}},
    {"actions": ["check"]},
]  # fmt: skip


def _core_view(compat: ModuleType, manifest: dict[str, Any]) -> list[dict[str, Any]] | None:
    try:
        parsed = compat.addon_actions(manifest)
    except ValueError:
        return None
    return sorted(
        (
            {"name": n, "description": s["description"], "destructive": s["destructive"]}
            for n, s in parsed.items()
        ),
        key=lambda a: a["name"],
    )


@pytest.mark.parametrize("manifest", _MANIFESTS)
def test_the_offered_actions_match_the_core(
    compat: ModuleType, manifest: dict[str, Any]
) -> None:
    expected = _core_view(compat, manifest)
    # A block the core refuses offers nothing here.
    assert addon_actions(manifest) == (expected if expected is not None else [])
