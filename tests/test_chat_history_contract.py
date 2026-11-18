"""The manager's reading of `librechat clear-history` against the core's own code.

The counts the Users page shows are parsed from the summary the core prints
(`lib.librechat_history.format_result`), and the page offers the command only when the core's
checkout has it. These tests need that checkout and are skipped without it: point `PAPAIA_SRC`
at a checkout of `Fidonis/papaia` that has the command (Fidonis/papaia#329, papAIa core 1.9.0).
Repeat them when the core's summary, its labels or its exit codes change.
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

from app.core.chat_history import HistoryCounts, core_supports_clear_history, parse_counts

_SRC = os.environ.get("PAPAIA_SRC", "")


def _core_has_clear_history() -> bool:
    return bool(_SRC) and (Path(_SRC) / "tools" / "lib" / "librechat_history.py").is_file()


pytestmark = pytest.mark.skipif(
    not _core_has_clear_history(),
    reason="PAPAIA_SRC does not point at a checkout of the core that has librechat clear-history",
)


@pytest.fixture(scope="module")
def history() -> Iterator[ModuleType]:
    tools = str(Path(_SRC) / "tools")
    sys.path.insert(0, tools)
    try:
        yield importlib.import_module("lib.librechat_history")
    finally:
        sys.path.remove(tools)


_RESULTS: list[dict[str, Any]] = [
    {"dry_run": True, "email": "jane@example.com"},
    {
        "dry_run": True,
        "email": "jane@example.com",
        "conversations": 4,
        "sub_threads": 2,
        "messages": 120,
        "files": 3,
        "shared_links": 1,
        "tool_calls": 9,
    },
    {
        "dry_run": False,
        "email": "jane@example.com",
        "conversations": 1,
        "sub_threads": 1,
        "messages": 2,
        "files": 5,
        "shared_links": 0,
        "tool_calls": 0,
        "files_failed": ["a.png", "b.pdf"],
    },
]


@pytest.mark.parametrize("result", _RESULTS)
@pytest.mark.parametrize("before", [None, "2026-01-31"])
def test_the_cores_summary_reads_back_as_its_counts(
    history: ModuleType, result: dict[str, Any], before: str | None
) -> None:
    text = history.format_result(result, before=before)
    failed = result.get("files_failed") or []
    assert parse_counts(text) == HistoryCounts(
        conversations=result.get("conversations", 0),
        sub_threads=result.get("sub_threads", 0),
        messages=result.get("messages", 0),
        files=result.get("files", 0),
        shared_links=result.get("shared_links", 0),
        tool_calls=result.get("tool_calls", 0),
        files_failed=len(failed),
    )


def test_every_kind_the_core_reports_is_read(history: ModuleType) -> None:
    keys = {key for key, _label in history.KINDS}
    assert keys == {"conversations", "messages", "files", "shared_links", "tool_calls"}


def test_the_checkout_is_recognised_as_having_the_command(tmp_path: Path) -> None:
    # The manager looks for the core under `<workspace>/papaia`.
    (tmp_path / "papaia").symlink_to(Path(_SRC).resolve(), target_is_directory=True)
    assert core_supports_clear_history(str(tmp_path)) is True
