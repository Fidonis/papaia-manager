"""Whitelisted subprocess wrapper for papaia-ctl.

Two entry points, two separate allowlists: `run_addon_verb` for
`papaia-ctl addon <verb> <name>`, `run_core_verb` for the stack-level verbs that
take no target. Keeping the sets apart is what makes it impossible for an addon
name to be dispatched as a stack operation, or the other way round.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import AsyncGenerator, Collection, Iterable
from pathlib import Path

from app.core.inventory import SELF_PROFILE
from app.core.services import invalidate_snapshot

logger = logging.getLogger(__name__)

# Only these verbs may be dispatched via papaia-ctl addon <verb>.
ALLOWED_VERBS: frozenset[str] = frozenset(
    {"install", "start", "stop", "remove", "uninstall", "check"}
)

# Stack-level verbs that may run as a child of this process.
#
# `start` and `stop` are here only because the services page always scopes them
# with `--profiles=`, and never to the manager's own profile. An unscoped `stop`
# would take down the container running the request -- which is why the
# stack-wide actions go through a detached container instead; see
# app.core.runner. Nothing in this module enforces the scoping: `profiles_flag`
# is what the callers build their flag with, and it refuses `manager`.
#
# `restore` remains deliberately absent. It tears the core stack down via
# `docker compose down` unconditionally, so there is no scoping that would make
# it safe to run here.
ALLOWED_CORE_VERBS: frozenset[str] = frozenset({"backup", "start", "stop"})

_ADDON_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

# Compose profile names, same shape as an add-on name. The pattern is the cheap
# half of the check -- `profiles_flag` also requires membership in the set read
# out of the shipped Compose fragments.
_PROFILE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")


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

    cmd: list[str] = [
        "bash",
        str(papaia_ctl_path(workspace_dir)),
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


def papaia_ctl_path(workspace_dir: str) -> Path:
    """Absolute path of the papaia-ctl entrypoint in the mounted workspace."""
    return Path(workspace_dir) / "papaia" / "tools" / "papaia-ctl"


def profiles_flag(names: Iterable[str], *, allowed: Collection[str]) -> str:
    """Build `--profiles=a,b` after checking every name against `allowed`.

    `allowed` is the mapping `inventory.core_groups` reads out of the shipped
    Compose fragments, so a caller cannot name a profile this deployment does
    not have -- and, since that function filters by the active profile set, not
    one whose env file setup never rendered either. `docker compose config`
    fails outright on the latter.

    `manager` is refused regardless of what `allowed` contains. It is the
    profile serving the request, and an operation that removes its own container
    can never report whether it worked.
    """
    ordered = sorted(set(names))
    if not ordered:
        raise ValueError("no service group given")
    for name in ordered:
        if not _PROFILE_RE.match(name):
            raise ValueError(
                f"profile {name!r} does not match ^[a-z0-9][a-z0-9-]{{0,31}}$"
            )
        if name == SELF_PROFILE:
            raise ValueError(
                f"profile {name!r} runs this panel and cannot be controlled from it"
            )
        if name not in allowed:
            raise ValueError(f"profile {name!r} is not a service group of this deployment")
    return f"--profiles={','.join(ordered)}"


async def run_core_verb(
    *,
    verb: str,
    workspace_dir: str,
    config_dir: str,
    extra_flags: list[str] | None = None,
) -> AsyncGenerator[str, None]:
    """Yield output lines from ``papaia-ctl <verb>``.

    Same contract as run_addon_verb: stdin is /dev/null, validation happens
    before the first line is yielded, CtlError is raised after a non-zero exit.
    """
    if verb not in ALLOWED_CORE_VERBS:
        raise ValueError(
            f"verb {verb!r} is not in the allowed set {sorted(ALLOWED_CORE_VERBS)}"
        )

    cmd: list[str] = [
        "bash",
        str(papaia_ctl_path(workspace_dir)),
        verb,
        f"--config-dir={config_dir}",
        *(extra_flags or []),
    ]

    env = dict(os.environ)
    env["PAPAIA_CONFIG_DIR"] = config_dir

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
    # Every verb reachable from here -- install, start, stop, remove, uninstall,
    # backup -- moves containers, so the cached `docker ps` reading behind the
    # services page and `compute_status` is stale the moment this returns.
    # Dropping it here rather than at each call site is what keeps a freshly
    # started addon from reading as stopped for the next five seconds.
    invalidate_snapshot()
    if proc.returncode != 0:
        code = proc.returncode if proc.returncode is not None else 1
        raise CtlError(
            f"papaia-ctl exited with code {code}",
            exit_code=code,
        )
