"""Clearing one account's LibreChat chat history, through the core's `librechat clear-history`.

The core command (papAIa core 1.9.0) removes a LibreChat user's conversations with their
sub-threads, messages, files, shared links and tool call records, and keeps the account, its
agents and its prompts. It finds the user by e-mail address and runs inside the running
LibreChat container, so the manager only hands it the address of a Keycloak account.

The Users page asks twice: a dry run for the counts the confirmation shows, and after the
confirmation the deletion itself as a queued job. Both print the core's summary, a few lines
of `label  count`, which `parse_counts` reads; a failure is an `ERROR:` (Python) or `[error]`
(shell) line, which `core_error` picks out. Nothing is decided here that the core decides:
an unknown user or a stopped LibreChat is the core's message, shown as it is.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.ctl import CtlError, count_chat_history
from app.i18n import gettext as _

MIN_CORE_VERSION = "1.9.0"

# Where the core keeps the command, and what shows that it is there: the shell verb papaia-ctl
# dispatches the deletion to, and the Python sub-command the dry run calls. A version number
# would be wrong for a checkout that has the change before the release.
_SHELL_SCRIPT = Path("papaia") / "tools" / "lib" / "sh" / "librechat.sh"
_SHELL_MARKER = "cmd_librechat_clear_history()"
_PY_SCRIPT = Path("papaia") / "tools" / "lib" / "cli.py"
_PY_MARKER = '"librechat-clear-history"'

# The job's action and the audit entry's, for success, partial success and failure alike.
AUDIT_ACTION = "user.chat-history.clear"

# The core's labels (`librechat_history.KINDS`) and the keys they are reported under here.
_LABELS: dict[str, str] = {
    "conversations": "conversations",
    "messages": "messages",
    "files": "files",
    "shared links": "shared_links",
    "tool call records": "tool_calls",
}
_COUNT_LINE = re.compile(
    r"^\s+(conversations|messages|files|shared links|tool call records)\s+(\d+)"
    r"(?:\s+\(including (\d+) sub-threads?\))?\s*$"
)
_FAILED_LINE = re.compile(r"^\s+(\d+) file\(s\) could not be removed from storage")
_ERROR_LINE = re.compile(r"^(?:ERROR:|\[error\])\s*(.+?)\s*$")
_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class ChatHistoryError(Exception):
    """The history could not be read or cleared; the message is the reason, for the page."""


@dataclass(frozen=True)
class HistoryCounts:
    """What a dry run would remove, or what a deletion removed."""

    conversations: int = 0
    sub_threads: int = 0
    messages: int = 0
    files: int = 0
    shared_links: int = 0
    tool_calls: int = 0
    # Files the deletion could not remove from storage; their records are kept.
    files_failed: int = 0

    @property
    def empty(self) -> bool:
        return not (
            self.conversations
            or self.messages
            or self.files
            or self.shared_links
            or self.tool_calls
        )

    def as_dict(self) -> dict[str, Any]:
        return {**asdict(self), "empty": self.empty}


@lru_cache(maxsize=4)
def _supports(shell: str, shell_mtime: int, py: str, py_mtime: int) -> bool:
    del shell_mtime, py_mtime  # part of the cache key: a core upgraded in place is read again
    try:
        return (
            _SHELL_MARKER in Path(shell).read_text(encoding="utf-8", errors="replace")
            and _PY_MARKER in Path(py).read_text(encoding="utf-8", errors="replace")
        )
    except OSError:
        return False


def core_supports_clear_history(workspace_dir: str) -> bool:
    """Whether the papaia core in the workspace has `librechat clear-history`."""
    shell = Path(workspace_dir) / _SHELL_SCRIPT
    py = Path(workspace_dir) / _PY_SCRIPT
    try:
        return _supports(str(shell), shell.stat().st_mtime_ns, str(py), py.stat().st_mtime_ns)
    except OSError:
        return False


def core_requirement_message() -> str:
    return _("Clearing chats needs papAIa core %(version)s or newer.") % {
        "version": MIN_CORE_VERSION
    }


def _clean(text: str) -> list[str]:
    return [_ANSI.sub("", line).rstrip() for line in text.splitlines()]


def parse_counts(text: str) -> HistoryCounts | None:
    """The counts in the core's summary, or None when the output has none.

    The summary is the last thing the command prints, so a later block wins over an earlier
    one (the shell wrapper prints a dry run's before its prompt, which `-y` skips)."""
    found: dict[str, int] = {}
    sub_threads = 0
    failed = 0
    for line in _clean(text):
        match = _COUNT_LINE.match(line)
        if match:
            key = _LABELS[match.group(1)]
            found[key] = int(match.group(2))
            if key == "conversations":
                sub_threads = int(match.group(3) or 0)
            continue
        failed_match = _FAILED_LINE.match(line)
        if failed_match:
            failed = int(failed_match.group(1))
    if not found:
        return None
    return HistoryCounts(**found, sub_threads=sub_threads, files_failed=failed)


def core_error(text: str) -> str:
    """The core's own reason (its last `ERROR:` or `[error]` line), or an empty string."""
    reasons = [m.group(1) for m in map(_ERROR_LINE.match, _clean(text)) if m]
    return reasons[-1] if reasons else ""


async def preview(email: str, *, workspace_dir: str, config_dir: str) -> HistoryCounts:
    """What clearing the history of the LibreChat user with this address would remove.

    Raises ChatHistoryError with the reason the page shows: the core's own message for an
    unknown user or a LibreChat that is not running."""
    try:
        code, out, err = await count_chat_history(
            email=email, workspace_dir=workspace_dir, config_dir=config_dir
        )
    except ValueError as exc:
        raise ChatHistoryError(str(exc)) from exc
    except CtlError as exc:
        raise ChatHistoryError(
            _("The chat history could not be read: %(reason)s") % {"reason": str(exc)}
        ) from exc
    counts = parse_counts(out)
    if code == 0 and counts is not None:
        return counts
    reason = core_error(err) or core_error(out)
    if not reason:
        lines = [line for line in _clean(err) if line.strip()]
        reason = lines[-1].strip() if lines else _("exit status %(code)s") % {"code": code}
    raise ChatHistoryError(reason)
