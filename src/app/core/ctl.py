"""Whitelisted subprocess wrapper for papaia-ctl.

Three entry points, three separate allowlists: `run_addon_verb` for
`papaia-ctl addon <verb> <name>`, `run_core_verb` for the stack-level verbs that
take no target, and `run_py_cli` for the core's own read-only Python
sub-commands. Keeping the sets apart is what makes it impossible for an addon
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
#
# `restore-scoped` is a different verb, not the same one with flags. It requires
# `--only` and refuses `--restart-clean` in papaia-ctl itself, before it
# delegates, so it cannot replace $PAPAIA_CONFIG_DIR however it is called. That
# makes the property structural: allowing `restore` and passing the right flags
# would be safe only for as long as every call site keeps passing them.
#
# `backup-delete` is safe for the same reason `backup` is: it runs no
# `docker compose` command at all -- it only removes a snapshot directory and
# rewrites `backup.yaml` under $PAPAIA_BACKUP_DIR -- and papaia-ctl requires an
# explicit `--restore-point=ID` for every point it deletes (each a guarded
# timestamp id), so no call from here can widen it into a mass delete.
ALLOWED_CORE_VERBS: frozenset[str] = frozenset(
    {"backup", "backup-delete", "start", "stop", "restore-scoped"}
)

# `upgrade` is deliberately absent from the set above, for a stronger version of
# the reason `restore` is: it runs `stop --clean-up --addons`, which removes the
# container this process is running in, and it does so unconditionally. It goes
# through a detached runner instead; see app.core.runner.

# The core's Python entry point, `python3 -m lib.cli <command>`. Every command
# here is read-only -- they resolve a version, list pending migrations, and
# evaluate the addon compatibility gate. None of them writes anything.
#
# `upgrade-record` is the one that would belong here by shape and does not: it
# appends to the migration ledger, and only the upgrade's own second phase may
# do that. A ledger entry written by the manager would make a migration that
# never ran look applied.
ALLOWED_PY_COMMANDS: frozenset[str] = frozenset(
    {"upgrade-resolve", "upgrade-plan", "addon-check"}
)

_ADDON_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")

# Restore selectors, mirroring the grammar in the core's tools/lib/backup.py.
# The prefix is mandatory there because `librechat` is at once a module, a
# service, a profile and the prefix of six volume names.
_MODULE_SELECTOR_RE = re.compile(r"^(module|addon):[a-z0-9][a-z0-9-]{0,31}$")
_VOLUME_SELECTOR_RE = re.compile(r"^volume:[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")

MAX_SELECTORS = 32

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


def selection_flag(selectors: Iterable[str], *, allowed: Collection[str]) -> str:
    """Build `--only=a,b` after checking every selector against `allowed`.

    Same two-stage shape as `profiles_flag`: the pattern is the cheap half, and
    `allowed` -- derived from the snapshot's own manifest, not from a pattern --
    is what stops a well-formed selector naming something this restore point
    does not contain.

    `module:manager` is refused regardless of what `allowed` holds, for the same
    reason `profiles_flag` refuses the profile: it runs this panel.
    """
    ordered = sorted(set(selectors))
    if not ordered:
        raise ValueError("no selection given")
    if len(ordered) > MAX_SELECTORS:
        raise ValueError(f"at most {MAX_SELECTORS} selectors, got {len(ordered)}")
    for selector in ordered:
        if not (
            _MODULE_SELECTOR_RE.match(selector) or _VOLUME_SELECTOR_RE.match(selector)
        ):
            raise ValueError(
                f"selector {selector!r} is not module:NAME, addon:NAME or volume:NAME"
            )
        if selector == f"module:{SELF_PROFILE}":
            raise ValueError(
                f"selector {selector!r} runs this panel and cannot be restored from it"
            )
        if selector not in allowed:
            raise ValueError(f"selector {selector!r} is not part of this restore point")
    return f"--only={','.join(ordered)}"


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


async def run_py_cli(
    *,
    command: str,
    workspace_dir: str,
    config_dir: str,
    repo_root: str | None = None,
    extra_flags: list[str] | None = None,
) -> tuple[int, str, str]:
    """Run one of the core's read-only Python sub-commands and collect its output.

    The same invocation shape papaia-ctl uses for itself (`py_cli` in the
    entrypoint, `_py_cli_at` in upgrade.sh): `lib.cli` is imported off the
    mounted workspace via PYTHONPATH rather than executed as a file, so its
    relative imports resolve.

    `repo_root` defaults to the checkout and is a *worktree* of the target tag
    when the caller is evaluating an upgrade candidate. That is the entire
    reason this parameter exists: the target's ADDON_API window and its
    migration directory live only in the target's tree.

    Unlike the two streaming entry points this returns the exit code instead of
    raising on it, and keeps stderr separate. Both differences are `addon-check`:
    it exits 2 to *mean* "incompatible" and still prints its JSON, so a non-zero
    status here is a result to read, not a failure to report.
    """
    if command not in ALLOWED_PY_COMMANDS:
        raise ValueError(
            f"command {command!r} is not in the allowed set {sorted(ALLOWED_PY_COMMANDS)}"
        )

    tools = Path(workspace_dir) / "papaia" / "tools"
    cmd: list[str] = [
        "python3",
        "-m",
        "lib.cli",
        "--repo-root",
        repo_root or str(Path(workspace_dir) / "papaia"),
        "--config-dir",
        config_dir,
        command,
        *(extra_flags or []),
    ]

    env = dict(os.environ)
    env["PAPAIA_CONFIG_DIR"] = config_dir
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{tools}{os.pathsep}{existing}" if existing else str(tools)

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
            env=env,
        )
    except (FileNotFoundError, OSError) as exc:
        raise CtlError(f"cannot invoke the core's python entry point: {exc}", exit_code=1) from exc
    raw_out, raw_err = await proc.communicate()
    return (
        proc.returncode if proc.returncode is not None else 1,
        raw_out.decode(errors="replace"),
        raw_err.decode(errors="replace"),
    )


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
