"""Whitelisted subprocess wrapper for papaia-ctl.

Four kinds of entry point, separate allowlists: `run_addon_verb` for
`papaia-ctl addon <verb> <name>`, `run_core_verb` for the stack-level verbs that
take no target, `run_py_cli` for the core's own read-only Python
sub-commands, and `run_smtp_setup` / `run_module_setup` for the one that writes (the
core's `setup`, restricted to the mail relay's or one optional module's options).
Keeping the sets apart is what makes it
impossible for an addon name to be dispatched as a stack operation, or the
other way round.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
from collections.abc import AsyncGenerator, Collection, Iterable
from dataclasses import dataclass, field
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
#
# `keycloak-role-sync` is the standalone form of the last step `start` runs. It
# applies what the realm template and the active profiles say (roles, composites,
# and the relay's mail server) to the running Keycloak, runs no `docker compose`
# command and is idempotent. The mail page needs it after switching the relay
# off, where there is no `start` to ride on.
ALLOWED_CORE_VERBS: frozenset[str] = frozenset(
    {"backup", "backup-delete", "start", "stop", "restore-scoped", "keycloak-role-sync"}
)

# `upgrade` is deliberately absent from the set above, for a stronger version of
# the reason `restore` is: it runs `stop --clean-up --addons`, which removes the
# container this process is running in, and it does so unconditionally. It goes
# through a detached runner instead; see app.core.runner.

# The core's Python entry point, `python3 -m lib.cli <command>`. Every command
# here is read-only -- they resolve a version, list pending migrations, evaluate
# the addon compatibility gate, and run the host diagnostics. None of them
# writes anything.
#
# `doctor` is allowed for its `disk_space` and `certs` checks only; the caller
# (`app.core.host_health`) skips the rest, because the others fork `docker`,
# resolve names and probe ports, which is not something to repeat on a poll.
#
# `upgrade-record` is the one that would belong here by shape and does not: it
# appends to the migration ledger, and only the upgrade's own second phase may
# do that. A ledger entry written by the manager would make a migration that
# never ran look applied.
# `render-preview` merges a draft overlay onto the rendered layers in memory and prints
# the result as JSON. It reads the draft from a file the caller names and writes
# nothing, so the overlay editor can show the effective configuration before anything
# is saved.
ALLOWED_PY_COMMANDS: frozenset[str] = frozenset(
    {"upgrade-resolve", "upgrade-plan", "addon-check", "doctor", "render-preview"}
)

# The one command that writes, kept apart from the read-only set so that adding to
# either is a decision of its own. `setup` is the core's non-interactive
# configuration engine; it is reachable only through `run_smtp_setup` and
# `run_module_setup`, which build their arguments from a typed spec (`SmtpSetup`,
# `ModuleSetup`) and pass nothing but the options of the mail relay or of one optional
# module -- never `--force`, `--env`, a host name or any other part of the stack's
# configuration. The core is what validates and stores the values, so the rules
# (what is sticky, when the password revision rises, how `COMPOSE_PROFILES` changes)
# stay in one place.
ALLOWED_PY_WRITE_COMMANDS: frozenset[str] = frozenset({"setup"})

# The optional modules the Modules tab may switch, and the `setup` option for each. A
# module is named by the profile the core adds to `COMPOSE_PROFILES`.
MODULE_SETUP_FLAGS: dict[str, str] = {
    "localai": "--enable-local-ai",
    "web-search": "--enable-web-search",
}

# LocalAI image variants the manager offers. `vulkan` is left out on purpose: the core
# decides its device list by looking at /dev while it renders, and from inside this
# container that says nothing about the host.
LOCALAI_VARIANTS: tuple[str, ...] = (
    "cpu",
    "nvidia-cuda-12",
    "nvidia-cuda-13",
    "intel",
    "hipblas",
)

SMTP_SECURITY_MODES: tuple[str, ...] = ("starttls", "tls", "none")

# The environment variable the core reads the upstream password from. A password
# in an argument would show in `ps` and in the process table of the container.
SMTP_PASSWORD_ENV = "PAPAIA_SMTP_PASSWORD"

# Longer than the read-only commands: setup renders the configuration and writes the
# env tree, but it starts nothing.
SETUP_TIMEOUT_SECONDS = 120.0

# Exit status `run_py_cli` reports for a child it had to kill, the one the
# shell's `timeout` uses.
TIMEOUT_EXIT_CODE = 124

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
    limit: float | None = None,
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

    `limit` bounds the whole run, in seconds. The default is no bound, which is what the
    upgrade check wants: it fetches from a remote and has its own budget. A
    caller that polls passes one, and a child that outlives it is killed and
    reaped before `CtlError` (exit code `TIMEOUT_EXIT_CODE`) is raised, so a
    hung probe cannot pile up one process per request.
    """
    if command not in ALLOWED_PY_COMMANDS:
        raise ValueError(
            f"command {command!r} is not in the allowed set {sorted(ALLOWED_PY_COMMANDS)}"
        )
    return await _run_py(
        command=command,
        workspace_dir=workspace_dir,
        config_dir=config_dir,
        repo_root=repo_root,
        extra_flags=extra_flags,
        limit=limit,
    )


@dataclass(frozen=True)
class SmtpSetup:
    """What the mail page asks the core's `setup` to do, and nothing else.

    `enable=False` switches the relay off and takes no other field: the core keeps
    the settings and the password for a later opt-in. With `enable=True` the caller
    names every value it wants stored, so what ends up in the configuration does
    not depend on what happened to be sticky. `password` is the one field that
    travels in the environment; `None` keeps the stored one.
    """

    enable: bool
    host: str | None = None
    port: int | None = None
    security: str | None = None
    user: str | None = None
    no_auth: bool = False
    sender: str | None = None
    sender_name: str | None = None
    password: str | None = field(default=None, repr=False)


def _plain(label: str, value: str) -> str:
    if not value or any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError(f"{label} must be a non-empty single line")
    return value


def smtp_setup_flags(spec: SmtpSetup) -> list[str]:
    """The `setup` options for `spec`. Raises ValueError for a spec that is not coherent.

    Each value is glued to its option (`--smtp-host=x`), so no value can become an
    option of its own. The core validates the values themselves; what is checked here
    is the shape the manager promises: coherent combinations, no control characters,
    no password anywhere in the result.
    """
    flags = [f"--enable-smtp={'true' if spec.enable else 'false'}"]
    given = (
        spec.host,
        spec.port,
        spec.security,
        spec.user,
        spec.sender,
        spec.sender_name,
        spec.password,
    )
    if not spec.enable:
        if spec.no_auth or any(value is not None for value in given):
            raise ValueError("switching the relay off takes no other setting")
        return flags
    if spec.no_auth and (spec.user or spec.password):
        raise ValueError("a mail server without login takes neither a login nor a password")
    if spec.host is None or spec.security is None or spec.port is None or spec.sender is None:
        raise ValueError("host, security, port and sender address are required")
    if spec.security not in SMTP_SECURITY_MODES:
        raise ValueError(f"security must be one of {', '.join(SMTP_SECURITY_MODES)}")
    if not 1 <= spec.port <= 65535:
        raise ValueError("port must be between 1 and 65535")
    flags.append(f"--smtp-host={_plain('host', spec.host)}")
    flags.append(f"--smtp-port={spec.port}")
    flags.append(f"--smtp-security={spec.security}")
    if spec.no_auth:
        flags.append("--smtp-no-auth")
    elif spec.user:
        flags.append(f"--smtp-user={_plain('login', spec.user)}")
    flags.append(f"--smtp-from={_plain('sender address', spec.sender)}")
    if spec.sender_name is not None:
        flags.append(f"--smtp-from-name={_plain('sender name', spec.sender_name)}")
    return flags


async def run_smtp_setup(
    spec: SmtpSetup,
    *,
    workspace_dir: str,
    config_dir: str,
    limit: float | None = SETUP_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    """Run the core's `setup` for the mail relay's options only.

    Exit 3 with `ERROR: ...` on stderr is the core refusing a value; nothing is
    written then. The password goes in the environment of the child and only there.
    """
    if "setup" not in ALLOWED_PY_WRITE_COMMANDS:  # pragma: no cover - the set is a constant
        raise ValueError("setup is not an allowed write command")
    return await _run_py(
        command="setup",
        workspace_dir=workspace_dir,
        config_dir=config_dir,
        extra_flags=smtp_setup_flags(spec),
        limit=limit,
        env_extra={SMTP_PASSWORD_ENV: spec.password or ""},
    )


@dataclass(frozen=True)
class ModuleSetup:
    """What the Modules tab asks the core's `setup` to do for one optional module.

    `module` is a key of `MODULE_SETUP_FLAGS`. `variant` is the LocalAI image variant and
    only makes sense when LocalAI is switched on; it is always named then, so what is stored
    does not depend on what happened to be sticky.
    """

    module: str
    enable: bool
    variant: str | None = None


def module_setup_flags(spec: ModuleSetup) -> list[str]:
    """The `setup` options for `spec`. Raises ValueError for a spec that is not coherent."""
    flag = MODULE_SETUP_FLAGS.get(spec.module)
    if flag is None:
        raise ValueError(f"module {spec.module!r} cannot be switched from here")
    flags = [f"{flag}={'true' if spec.enable else 'false'}"]
    if spec.module != "localai":
        if spec.variant is not None:
            raise ValueError("only LocalAI takes an image variant")
        return flags
    if not spec.enable:
        if spec.variant is not None:
            raise ValueError("switching LocalAI off takes no variant")
        return flags
    if spec.variant not in LOCALAI_VARIANTS:
        raise ValueError(f"variant must be one of {', '.join(LOCALAI_VARIANTS)}")
    flags.append(f"--localai-variant={spec.variant}")
    return flags


async def run_module_setup(
    spec: ModuleSetup,
    *,
    workspace_dir: str,
    config_dir: str,
    limit: float | None = SETUP_TIMEOUT_SECONDS,
) -> tuple[int, str, str]:
    """Run the core's `setup` for one optional module's options only.

    Exit 3 with `ERROR: ...` on stderr is the core refusing a value; nothing is written then.
    """
    if "setup" not in ALLOWED_PY_WRITE_COMMANDS:  # pragma: no cover - the set is a constant
        raise ValueError("setup is not an allowed write command")
    return await _run_py(
        command="setup",
        workspace_dir=workspace_dir,
        config_dir=config_dir,
        extra_flags=module_setup_flags(spec),
        limit=limit,
    )


async def _run_py(
    *,
    command: str,
    workspace_dir: str,
    config_dir: str,
    repo_root: str | None = None,
    extra_flags: list[str] | None = None,
    limit: float | None = None,
    env_extra: dict[str, str] | None = None,
) -> tuple[int, str, str]:
    """The process half of `run_py_cli` and `run_smtp_setup`; the callers check the allowlist."""
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
    # Never inherited: a password the manager's own environment happens to carry is
    # not the one the operator typed.
    env.pop(SMTP_PASSWORD_ENV, None)
    env["PAPAIA_CONFIG_DIR"] = config_dir
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = f"{tools}{os.pathsep}{existing}" if existing else str(tools)
    if env_extra:
        env.update(env_extra)

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
    try:
        async with asyncio.timeout(limit):
            raw_out, raw_err = await proc.communicate()
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise CtlError(
            f"python3 -m lib.cli {command} timed out after {limit:g}s",
            exit_code=TIMEOUT_EXIT_CODE,
        ) from exc
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
