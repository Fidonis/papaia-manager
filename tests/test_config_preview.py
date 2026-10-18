"""The effective result of a draft overlay: finding out whether the core can show it, handing it
the draft without leaving it behind, and reading the answer.

The core's own command is replaced by a recorder. `test_config_contract.py` runs the real one.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

import pytest

from app.core import ctl
from app.core.config import preview


def _core(workspace: Path, source: str | None) -> None:
    lib = workspace / "papaia" / "tools" / "lib"
    lib.mkdir(parents=True, exist_ok=True)
    if source is not None:
        (lib / "cli.py").write_text(source, encoding="utf-8")


# -- does the core have it -----------------------------------------------------------------


def test_a_core_with_the_command_can_preview(tmp_path: Path) -> None:
    _core(tmp_path, "sub.add_parser('render-preview')\n")
    assert preview.available(str(tmp_path)) is True


def test_a_core_without_the_command_cannot(tmp_path: Path) -> None:
    _core(tmp_path, "sub.add_parser('render')\n")
    assert preview.available(str(tmp_path)) is False


def test_no_core_at_all_cannot(tmp_path: Path) -> None:
    assert preview.available(str(tmp_path / "nowhere")) is False


def test_the_answer_follows_the_file_when_the_core_is_upgraded(tmp_path: Path) -> None:
    _core(tmp_path, "nothing here\n")
    assert preview.available(str(tmp_path)) is False
    source = tmp_path / "papaia" / "tools" / "lib" / "cli.py"
    source.write_text("sub.add_parser('render-preview')\n", encoding="utf-8")
    later = source.stat().st_mtime + 5
    os.utime(source, (later, later))
    assert preview.available(str(tmp_path)) is True


# -- reading the answer ----------------------------------------------------------------------


def _answer(**fields: Any) -> str:
    base: dict[str, Any] = {
        "ok": True,
        "target": "ai/librechat/librechat.yaml",
        "structured": True,
        "current": "a: 1\n",
        "preview": "a: 1\nb: 2\n",
        "changed": True,
    }
    base.update(fields)
    return json.dumps(base)


def test_a_merged_draft_comes_back_as_a_diff() -> None:
    result = preview.parse_answer(_answer())
    assert result.ok and result.changed
    assert (result.added, result.removed) == (1, 0)
    assert {"kind": "add", "text": "b: 2"} in result.diff
    assert {"kind": "ctx", "text": "a: 1"} in result.diff


def test_a_draft_that_changes_nothing_is_said_to_change_nothing() -> None:
    result = preview.parse_answer(_answer(preview="a: 1\n", changed=False))
    assert result.ok and not result.changed and result.diff == []


def test_a_draft_the_core_cannot_merge_is_an_answer_not_an_error() -> None:
    result = preview.parse_answer(
        json.dumps({"ok": False, "error": "invalid YAML", "line": 7, "column": 3})
    )
    assert (result.ok, result.error, result.line, result.column) == (False, "invalid YAML", 7, 3)


@pytest.mark.parametrize(
    "text",
    ["", "not json", "[]", "{}", '{"ok": "yes"}', '{"ok": true}', '{"ok": true, "current": 1}'],
)
def test_anything_else_is_not_understood(text: str) -> None:
    with pytest.raises(preview.PreviewError, match="not understood"):
        preview.parse_answer(text)


def test_the_diff_keeps_three_lines_of_context() -> None:
    old = "\n".join(f"line{i}" for i in range(30)) + "\n"
    new = old.replace("line15", "changed")
    diff = preview.diff_lines(old, new)
    assert diff[0]["kind"] == "hunk"
    assert [d["text"] for d in diff if d["kind"] == "del"] == ["line15"]
    assert [d["text"] for d in diff if d["kind"] == "add"] == ["changed"]
    assert sum(1 for d in diff if d["kind"] == "ctx") == 6


# -- asking the core -------------------------------------------------------------------------


class _Core:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.draft_seen = ""
        self.draft_mode = 0
        self.draft_path = Path()
        self.reply: tuple[int, str, str] = (0, _answer(), "")
        self.raises: Exception | None = None

    async def run_py_cli(self, **kwargs: Any) -> tuple[int, str, str]:
        self.calls.append(kwargs)
        flags = {f.split("=", 1)[0]: f.split("=", 1)[1] for f in kwargs["extra_flags"]}
        self.draft_path = Path(flags["--overlay-file"])
        self.draft_seen = self.draft_path.read_text(encoding="utf-8")
        self.draft_mode = self.draft_path.stat().st_mode & 0o777
        if self.raises is not None:
            raise self.raises
        return self.reply


@pytest.fixture
def core(monkeypatch: pytest.MonkeyPatch) -> _Core:
    fake = _Core()
    monkeypatch.setattr(ctl, "run_py_cli", fake.run_py_cli)
    return fake


def _run(tmp_path: Path, draft: str = "b: 2\n") -> preview.Preview:
    return asyncio.run(
        preview.run(
            workspace_dir=str(tmp_path / "w"),
            config_dir=str(tmp_path / "c"),
            target_path="ai/librechat/librechat.yaml",
            draft=draft,
        )
    )


def test_the_core_gets_the_target_and_the_draft_in_a_file(tmp_path: Path, core: _Core) -> None:
    result = _run(tmp_path, "b: 2\n")
    assert result.ok
    [call] = core.calls
    assert call["command"] == "render-preview"
    assert call["extra_flags"][0] == "--target=ai/librechat/librechat.yaml"
    assert core.draft_seen == "b: 2\n"
    assert call["limit"] == preview.TIMEOUT_SECONDS


def test_the_draft_is_not_an_argument(tmp_path: Path, core: _Core) -> None:
    _run(tmp_path, "apiKey: very-secret-value\n")
    assert "very-secret-value" not in " ".join(core.calls[0]["extra_flags"])


def test_the_draft_file_is_for_its_owner_only_and_gone_afterwards(
    tmp_path: Path, core: _Core
) -> None:
    _run(tmp_path)
    if os.name != "nt":
        assert core.draft_mode == 0o600
    assert not core.draft_path.exists()
    assert list((tmp_path / "c" / "manager" / "tmp").iterdir()) == []


def test_the_draft_file_is_gone_when_the_core_fails(tmp_path: Path, core: _Core) -> None:
    core.reply = (1, "", "Traceback ...\nValueError: boom")
    with pytest.raises(preview.PreviewError, match="Traceback"):
        _run(tmp_path)
    assert not core.draft_path.exists()


def test_the_draft_file_is_gone_when_the_core_takes_too_long(tmp_path: Path, core: _Core) -> None:
    core.raises = ctl.CtlError("timed out", exit_code=ctl.TIMEOUT_EXIT_CODE)
    with pytest.raises(preview.PreviewError, match="in time"):
        _run(tmp_path)
    assert not core.draft_path.exists()


def test_an_unreadable_answer_is_an_error(tmp_path: Path, core: _Core) -> None:
    core.reply = (0, "hello", "")
    with pytest.raises(preview.PreviewError, match="not understood"):
        _run(tmp_path)
