"""Whitelisted subprocess wrapper for papaia-ctl addon verbs."""
from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import AsyncGenerator
from pathlib import Path

logger = logging.getLogger(__name__)

# Only these verbs may be dispatched via papaia-ctl addon <verb>.
ALLOWED_VERBS: frozenset[str] = frozenset(
    {"install", "start", "stop", "remove", "uninstall", "check"}
)

_ADDON_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


class CtlError(Exception):
    """Raised when papaia-ctl exits with a non-zero status."""

    def __init__(self, message: str, exit_code: int) -> None:
        super().__init__(message)
        self.exit_code = exit_code


async def run_addon_verb(
    *,
    verb: str,
    name: str,
    workspace_dir: str,
    config_dir: str,
    extra_flags: list[str] | None = None,
    env_override: dict[str, str] | None = None,
) -> AsyncGenerator[str, None]:
    """Yield output lines from ``papaia-ctl addon <verb> <name>``.

    stdin is /dev/null so interactive CHANGE_ME prompts are skipped by design.
    Validation runs before the first line is yielded; CtlError is raised after
    the subprocess exits non-zero.
    """
    if verb not in ALLOWED_VERBS:
        raise ValueError(
            f"verb {verb!r} is not in the allowed set {sorted(ALLOWED_VERBS)}"
        )
    if not _ADDON_NAME_RE.match(name):
        raise ValueError(
            f"addon name {name!r} does not match ^[a-z0-9][a-z0-9-]{{0,31}}$"
        )

    papaia_ctl = Path(workspace_dir) / "papaia" / "tools" / "papaia-ctl"
    cmd: list[str] = [
        "bash",
        str(papaia_ctl),
        "addon",
        verb,
        name,
        f"--config-dir={config_dir}",
        *(extra_flags or []),
    ]

    env = dict(os.environ)
    env["PAPAIA_CONFIG_DIR"] = config_dir
    if env_override:
        env.update(env_override)

    return _stream_subprocess(cmd, env)


async def _stream_subprocess(
    cmd: list[str],
    env: dict[str, str],
) -> AsyncGenerator[str, None]:
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        stdin=asyncio.subprocess.DEVNULL,
        env=env,
    )

    assert proc.stdout is not None
    async for raw in proc.stdout:
        yield raw.decode(errors="replace").rstrip()

    await proc.wait()
    if proc.returncode != 0:
        code = proc.returncode if proc.returncode is not None else 1
        raise CtlError(
            f"papaia-ctl addon exited with code {code}",
            exit_code=code,
        )
