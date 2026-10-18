"""The effective result of a draft overlay, as the core would render it.

The core owns the merge (`render_core`), so the manager does not repeat it: it hands the
draft to the core's `render-preview` command, which merges it in memory onto the same layers
`render` uses and prints the result as JSON, writing nothing. The manager shows the difference
between what the target renders to now and what it would render to with the draft.

A core that has no such command (it comes with the release that carries Fidonis/papaia#238)
is detected from its source, not from a version number, the way `mail.settings.supported`
detects the mail relay. The editor still works then, with the syntax check alone.

The draft travels in a temporary file of mode 0600 under `manager/tmp/` that is removed again
whatever happens: it can hold an API key, and `ps` would show an argument.
"""

from __future__ import annotations

import contextlib
import difflib
import json
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from app.core import ctl

COMMAND = "render-preview"
TIMEOUT_SECONDS = 30.0

_AVAILABLE: dict[str, tuple[int, bool]] = {}


def available(workspace_dir: str) -> bool:
    """Whether the core in the workspace has the `render-preview` command."""
    source = Path(workspace_dir) / "papaia" / "tools" / "lib" / "cli.py"
    try:
        stamp = source.stat().st_mtime_ns
    except OSError:
        return False
    cached = _AVAILABLE.get(str(source))
    if cached is not None and cached[0] == stamp:
        return cached[1]
    try:
        found = COMMAND in source.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        found = False
    _AVAILABLE[str(source)] = (stamp, found)
    return found


class PreviewError(Exception):
    """The core could not be asked, or answered with something unusable."""


@dataclass(frozen=True)
class Preview:
    ok: bool
    error: str = ""
    line: int | None = None
    column: int | None = None
    current: str = ""
    preview: str = ""
    changed: bool = False
    diff: list[dict[str, str]] = field(default_factory=list)
    added: int = 0
    removed: int = 0


def diff_lines(current: str, preview: str, *, context: int = 3) -> list[dict[str, str]]:
    """The change from `current` to `preview` as the lines a diff view shows."""
    out: list[dict[str, str]] = []
    generator = difflib.unified_diff(
        current.splitlines(), preview.splitlines(), lineterm="", n=context
    )
    for line in generator:
        if line.startswith(("---", "+++")):
            continue
        if line.startswith("@@"):
            out.append({"kind": "hunk", "text": line})
        elif line.startswith("+"):
            out.append({"kind": "add", "text": line[1:]})
        elif line.startswith("-"):
            out.append({"kind": "del", "text": line[1:]})
        else:
            out.append({"kind": "ctx", "text": line[1:]})
    return out


def parse_answer(stdout: str) -> Preview:
    """The core's JSON document as a `Preview`. Raises PreviewError for anything else."""
    try:
        data = json.loads(stdout)
    except ValueError as exc:
        raise PreviewError("The core's answer was not understood.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("ok"), bool):
        raise PreviewError("The core's answer was not understood.")
    if not data["ok"]:
        line, column = data.get("line"), data.get("column")
        return Preview(
            ok=False,
            error=str(data.get("error") or "The draft can't be merged."),
            line=line if isinstance(line, int) else None,
            column=column if isinstance(column, int) else None,
        )
    current, preview = data.get("current"), data.get("preview")
    if not isinstance(current, str) or not isinstance(preview, str):
        raise PreviewError("The core's answer was not understood.")
    diff = diff_lines(current, preview)
    return Preview(
        ok=True,
        current=current,
        preview=preview,
        changed=bool(data.get("changed", current != preview)),
        diff=diff,
        added=sum(1 for d in diff if d["kind"] == "add"),
        removed=sum(1 for d in diff if d["kind"] == "del"),
    )


async def run(*, workspace_dir: str, config_dir: str, target_path: str, draft: str) -> Preview:
    """Ask the core for the effective result of `draft` on `target_path`."""
    scratch = Path(config_dir) / "manager" / "tmp"
    draft_file = scratch / f"overlay-draft-{uuid.uuid4().hex}.tmp"
    try:
        scratch.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(
            draft_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
        )
        with os.fdopen(fd, "wb") as handle:
            handle.write(draft.encode("utf-8"))
    except OSError as exc:
        raise PreviewError(
            f"The draft can't be handed to the core ({exc.strerror or exc})."
        ) from exc
    try:
        try:
            code, out, err = await ctl.run_py_cli(
                command=COMMAND,
                workspace_dir=workspace_dir,
                config_dir=config_dir,
                extra_flags=[f"--target={target_path}", f"--overlay-file={draft_file}"],
                limit=TIMEOUT_SECONDS,
            )
        except ctl.CtlError as exc:
            raise PreviewError(f"The core didn't answer in time ({exc}).") from exc
        if code != 0:
            first = next((ln.strip() for ln in (err or out).splitlines() if ln.strip()), "")
            raise PreviewError(first[:300] or f"The preview exited with status {code}.")
        return parse_answer(out)
    finally:
        with contextlib.suppress(OSError):
            draft_file.unlink(missing_ok=True)
