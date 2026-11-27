"""Clearing an account's chat history: what the core prints, read back, and when it is there.

The core's `librechat clear-history` prints a summary of `label  count` lines and an `ERROR:`
line on failure (`lib/librechat_history.format_result`, `lib/cli.cmd_librechat_clear_history`
in papAIa core 1.9.0). The samples below are that output; nothing here forks a process.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.core import chat_history
from app.core.chat_history import (
    ChatHistoryError,
    HistoryCounts,
    core_error,
    core_supports_clear_history,
    parse_counts,
    preview,
)

DRY_RUN = """\
Would remove from jane@example.com's history:
  conversations      4 (including 2 sub-threads)
  messages           10
  files              3
  shared links       1
  tool call records  7
Nothing was changed (dry run).
"""

REMOVED_WITH_FAILED_FILES = """\
[ctl] papaia-ctl librechat clear-history jane@example.com -y
Removed from jane@example.com's history:
  conversations      1
  messages           2
  files              1
  shared links       0
  tool call records  0
  2 file(s) could not be removed from storage and were kept: a.png, b.pdf
--- ERROR: papaia-ctl exited with code 1 ---
"""

NOTHING = """\
Would remove from jane@example.com's history:
  conversations      0
  messages           0
  files              0
  shared links       0
  tool call records  0
Nothing was changed (dry run).
"""


def test_the_summary_is_read_into_counts() -> None:
    assert parse_counts(DRY_RUN) == HistoryCounts(
        conversations=4, sub_threads=2, messages=10, files=3, shared_links=1, tool_calls=7
    )


def test_one_sub_thread_is_read_as_well() -> None:
    text = DRY_RUN.replace("(including 2 sub-threads)", "(including 1 sub-thread)")
    counts = parse_counts(text)
    assert counts is not None and counts.sub_threads == 1


def test_files_that_stayed_in_storage_are_counted() -> None:
    counts = parse_counts(REMOVED_WITH_FAILED_FILES)
    assert counts == HistoryCounts(conversations=1, messages=2, files=1, files_failed=2)
    # The queue's own failure line is not the core's reason.
    assert core_error(REMOVED_WITH_FAILED_FILES) == ""


def test_an_empty_history_is_empty() -> None:
    counts = parse_counts(NOTHING)
    assert counts is not None and counts.empty
    assert counts.as_dict()["empty"] is True
    assert parse_counts(DRY_RUN).as_dict()["empty"] is False  # type: ignore[union-attr]


def test_colour_codes_do_not_hide_a_line() -> None:
    assert core_error("\x1b[0;31m[error]\x1b[0m LibreChat is not running.") == (
        "LibreChat is not running."
    )


def test_output_without_a_summary_has_no_counts() -> None:
    assert parse_counts("") is None
    assert parse_counts("ERROR: LibreChat has no user with the e-mail address 'x@y'.") is None


def test_the_last_error_line_is_the_reason() -> None:
    text = "[papaia-ctl] something\nERROR: first\nnoise\n[error] second\n"
    assert core_error(text) == "second"


# -- whether the core has the command ------------------------------------------------


def _core(workspace: Path, *, shell: bool = True, py: bool = True) -> Path:
    lib = workspace / "papaia" / "tools" / "lib"
    (lib / "sh").mkdir(parents=True)
    (lib / "sh" / "librechat.sh").write_text(
        "cmd_librechat_clear_history() {\n:\n}\n" if shell else "cmd_librechat() { :; }\n",
        encoding="utf-8",
    )
    (lib / "cli.py").write_text(
        'p = sub.add_parser("librechat-clear-history")\n' if py else "p = None\n",
        encoding="utf-8",
    )
    return workspace


def test_a_core_with_the_command_is_recognised(tmp_path: Path) -> None:
    assert core_supports_clear_history(str(_core(tmp_path))) is True


@pytest.mark.parametrize(("shell", "py"), [(False, True), (True, False), (False, False)])
def test_a_core_without_both_halves_is_not(tmp_path: Path, shell: bool, py: bool) -> None:
    assert core_supports_clear_history(str(_core(tmp_path, shell=shell, py=py))) is False


def test_a_core_older_than_the_command_is_not(tmp_path: Path) -> None:
    # papAIa core up to 1.8.x has neither file.
    assert core_supports_clear_history(str(tmp_path)) is False


def test_a_core_upgraded_in_place_is_read_again(tmp_path: Path) -> None:
    workspace = _core(tmp_path, shell=False)
    assert core_supports_clear_history(str(workspace)) is False
    script = workspace / "papaia" / "tools" / "lib" / "sh" / "librechat.sh"
    script.write_text("cmd_librechat_clear_history() {\n:\n}\n# newer\n", encoding="utf-8")
    assert core_supports_clear_history(str(workspace)) is True


def test_the_requirement_names_the_release() -> None:
    assert "1.9.0" in chat_history.core_requirement_message()


# -- the dry run ---------------------------------------------------------------------


def _fake_count(
    monkeypatch: pytest.MonkeyPatch, result: tuple[int, str, str] | Exception
) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    async def count(**kwargs: Any) -> tuple[int, str, str]:
        calls.append(kwargs)
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(chat_history, "count_chat_history", count)
    return calls


async def test_the_preview_is_the_dry_runs_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _fake_count(monkeypatch, (0, DRY_RUN, ""))
    counts = await preview("jane@example.com", workspace_dir="/w", config_dir="/c")
    assert counts.conversations == 4
    assert calls == [{"email": "jane@example.com", "workspace_dir": "/w", "config_dir": "/c"}]


async def test_an_unknown_user_is_the_cores_reason(monkeypatch: pytest.MonkeyPatch) -> None:
    reason = "LibreChat has no user with the e-mail address 'jane@example.com'."
    _fake_count(monkeypatch, (1, "", f"ERROR: {reason}\n"))
    with pytest.raises(ChatHistoryError) as raised:
        await preview("jane@example.com", workspace_dir="/w", config_dir="/c")
    assert str(raised.value) == reason


async def test_a_failure_without_an_error_line_still_says_something(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_count(monkeypatch, (1, "", "Traceback ...\nKeyError: 'x'\n"))
    with pytest.raises(ChatHistoryError, match="KeyError"):
        await preview("jane@example.com", workspace_dir="/w", config_dir="/c")
    _fake_count(monkeypatch, (2, "", ""))
    with pytest.raises(ChatHistoryError, match="exit status 2"):
        await preview("jane@example.com", workspace_dir="/w", config_dir="/c")


async def test_a_dry_run_that_hangs_is_a_reason_too(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.core.ctl import CtlError

    _fake_count(monkeypatch, CtlError("python3 -m lib.cli x timed out after 60s", 124))
    with pytest.raises(ChatHistoryError, match="timed out"):
        await preview("jane@example.com", workspace_dir="/w", config_dir="/c")


async def test_an_address_that_could_be_an_option_never_reaches_the_core() -> None:
    with pytest.raises(ChatHistoryError, match="not an e-mail address"):
        await preview("--dry-run@x", workspace_dir="/w", config_dir="/c")
