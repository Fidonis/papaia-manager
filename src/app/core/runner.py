"""Detached container that runs papaia-ctl operations which outlive the manager.

Two operations cannot run as a job in this process, for the same reason.
`papaia-ctl restore` tears the core stack down with `docker compose down` before
unpacking any archive, and a stack-wide `stop` or `restart` does the same by
definition -- while papaia-manager is itself a service of that core project
(profile `manager`). Either one started here would be SIGKILLed the moment
teardown removed its own container, after the stack is down and before the work
is done.

So the manager starts papaia-ctl in a *separate* container and steps out of the
way. That container is built from the manager's own container spec: same image,
same binds, same user, same supplementary groups. Cloning the spec rather than
re-deriving it means path parity and Docker socket access hold by construction,
and the compose fragment stays the single place those mounts are declared.

The two runner flavours are told apart by a label and a name prefix, bundled in
`RunnerKind`, so a stack restart in flight can never be mistaken for a restore
and vice versa. Profile-scoped group actions do *not* come through here: they
leave the manager's own profile alone and therefore run as ordinary jobs.

State lives in Docker, not on disk. The runner is started without `--rm`, so
after it exits `docker inspect` still yields its status and exit code and
`docker logs` still yields its output -- both readable by a manager container
that was removed and recreated in the middle of the operation. A progress file
would not survive: restore replaces $PAPAIA_CONFIG_DIR wholesale, so anything
written there is destroyed by the very operation it tracks. The durable
cross-restore record remains papaia-ctl's own `backup.log`, which lives in the
backup directory and is never restored over.
"""
from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Any

from app.core.backups import is_valid_restore_point_id
from app.core.ctl import papaia_ctl_path

logger = logging.getLogger(__name__)

# Label the compose fragment puts on the manager container. Used to find our own
# container without depending on a container name or on $HOSTNAME, neither of
# which is stable across compose project names and dev/production compose files.
SELF_LABEL = "de.fidonis.module=papaia-manager"

# Label and name prefix of the runner. The label makes the runner discoverable
# after a manager restart; the name makes the operation idempotent -- one runner
# per target, and `docker run` refuses a duplicate name outright.
RUNNER_LABEL_KEY = "de.fidonis.module"
RUNNER_LABEL_VALUE = "papaia-restore"
RUNNER_NAME_PREFIX = "papaia-restore-"

# Label carrying what the runner was started for. Read back by
# `_status_from_inspect` so a runner found after a manager restart can still say
# what it is doing, without parsing its command line.
TARGET_LABEL_KEY = "de.fidonis.runner-target"

STACK_LABEL_VALUE = "papaia-stack"
STACK_NAME_PREFIX = "papaia-stack-"

# Stack-wide actions. `restart` is composed rather than dispatched, because
# papaia-ctl has no restart verb -- see `_stack_command`.
STACK_ACTIONS = frozenset({"start", "stop", "restart"})


@dataclass(frozen=True)
class RunnerKind:
    """What a runner is for, and how to find it again.

    Restore and stack runners share every mechanic and differ only in these two
    strings. Passing the kind around rather than branching on a flag is what
    keeps `find_runner` from ever returning a restore when asked for a stack
    operation -- the label filter does the separating.
    """

    label_value: str
    name_prefix: str


RESTORE_KIND = RunnerKind(RUNNER_LABEL_VALUE, RUNNER_NAME_PREFIX)
STACK_KIND = RunnerKind(STACK_LABEL_VALUE, STACK_NAME_PREFIX)

# How much of the runner's output the status endpoint returns. A restore logs one
# line per artifact plus the stack start, so this covers a full run with room to
# spare while keeping the polled payload small.
LOG_TAIL_LINES = 500

_DOCKER_TIMEOUT = 30.0


class RunnerError(Exception):
    """Raised when the runner cannot be inspected, started or removed."""


@dataclass
class ContainerSpec:
    """The parts of a container's configuration the runner has to inherit."""

    image: str
    binds: list[str]
    user: str = ""
    group_add: list[str] | None = None
    extra_hosts: list[str] | None = None


@dataclass
class RunnerStatus:
    """Live or final state of a runner.

    `target` is the restore point id for a restore runner and the action for a
    stack runner -- whatever the name after the prefix stands for.
    """

    name: str
    target: str
    status: str  # docker's State.Status: created|running|exited|dead|...
    exit_code: int | None
    started_at: str
    finished_at: str

    @property
    def is_running(self) -> bool:
        return self.status in ("created", "running", "restarting", "paused")

    @property
    def succeeded(self) -> bool:
        return not self.is_running and self.exit_code == 0


def runner_name(target: str, kind: RunnerKind = RESTORE_KIND) -> str:
    return f"{kind.name_prefix}{target}"


async def _docker(*args: str) -> str:
    """Run a docker CLI command and return stdout, raising on failure."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker",
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            stdin=asyncio.subprocess.DEVNULL,
        )
    except (FileNotFoundError, OSError) as exc:
        # No docker CLI on PATH -- the manager is running outside its image. Same
        # class of failure as an unreachable daemon, so it surfaces the same way
        # rather than as an unhandled exception on a polled endpoint.
        raise RunnerError(f"cannot invoke docker: {exc}") from exc
    try:
        raw_out, raw_err = await asyncio.wait_for(proc.communicate(), timeout=_DOCKER_TIMEOUT)
    except TimeoutError as exc:
        proc.kill()
        raise RunnerError(f"docker {args[0]} timed out after {_DOCKER_TIMEOUT:.0f}s") from exc
    if proc.returncode != 0:
        detail = raw_err.decode(errors="replace").strip() or f"exit code {proc.returncode}"
        raise RunnerError(f"docker {args[0]} failed: {detail}")
    return raw_out.decode(errors="replace")


async def self_spec() -> ContainerSpec:
    """Inspect this container and return the spec a runner must inherit.

    Raises RunnerError when the manager is not running as a labelled container --
    outside Docker there is no spec to clone, and guessing one would produce a
    runner with the wrong mounts, which is worse than refusing.
    """
    listing = await _docker("ps", "--filter", f"label={SELF_LABEL}", "--format", "{{.ID}}")
    ids = [line.strip() for line in listing.splitlines() if line.strip()]
    if not ids:
        raise RunnerError(
            "cannot locate the papaia-manager container "
            f"(no running container labelled {SELF_LABEL}); "
            "restore is only available when the manager runs in Docker"
        )
    # More than one match means several manager containers share the host (a dev
    # container next to a deployed one). The first is ours often enough to be
    # useless as a guess, so name the ambiguity instead of picking blindly.
    if len(ids) > 1:
        raise RunnerError(
            f"found {len(ids)} containers labelled {SELF_LABEL}; "
            "cannot tell which one to clone the restore runner from"
        )
    return _spec_from_inspect(json.loads(await _docker("inspect", ids[0])))


def _spec_from_inspect(payload: Any) -> ContainerSpec:
    """Build a ContainerSpec from a `docker inspect` payload."""
    if not isinstance(payload, list) or not payload:
        raise RunnerError("docker inspect returned no container")
    entry = payload[0]
    if not isinstance(entry, dict):
        raise RunnerError("docker inspect returned an unexpected shape")
    host_config = entry.get("HostConfig") or {}
    config = entry.get("Config") or {}
    image = str(config.get("Image") or entry.get("Image") or "")
    if not image:
        raise RunnerError("the manager container reports no image to reuse")
    return ContainerSpec(
        image=image,
        binds=[str(b) for b in host_config.get("Binds") or []],
        user=str(config.get("User") or ""),
        group_add=[str(g) for g in host_config.get("GroupAdd") or []],
        extra_hosts=[str(h) for h in host_config.get("ExtraHosts") or []],
    )


def _base_run_args(spec: ContainerSpec, *, name: str, kind: RunnerKind, target: str) -> list[str]:
    """The `docker run` flags every runner shares, up to but not including the image."""
    args: list[str] = [
        "run",
        "--detach",
        "--name",
        name,
        "--label",
        f"{RUNNER_LABEL_KEY}={kind.label_value}",
        "--label",
        f"{TARGET_LABEL_KEY}={target}",
        # The runner outlives the manager but must not outlive its own command:
        # an operation that failed has to stay failed and visible, not be retried
        # by the daemon against a half-torn-down stack.
        "--restart",
        "no",
    ]
    for bind in spec.binds:
        args += ["--volume", bind]
    if spec.user:
        args += ["--user", spec.user]
    for group in spec.group_add or []:
        args += ["--group-add", group]
    for host in spec.extra_hosts or []:
        args += ["--add-host", host]
    args.append(spec.image)
    return args


def build_run_args(
    spec: ContainerSpec,
    *,
    restore_point: str,
    workspace_dir: str,
    config_dir: str,
    backup_dir: str,
    restart_clean: bool = False,
) -> list[str]:
    """Assemble the full `docker run` argv for a restore runner.

    Split out from starting it so the argument shape is unit-testable without a
    Docker daemon -- the flags here are the whole security surface of this
    module.
    """
    if not is_valid_restore_point_id(restore_point):
        raise ValueError(f"restore point {restore_point!r} is not a valid snapshot id")

    args = _base_run_args(
        spec,
        name=runner_name(restore_point, RESTORE_KIND),
        kind=RESTORE_KIND,
        target=restore_point,
    )
    # `-y` is mandatory: papaia-ctl refuses to restore without confirmation and
    # there is no TTY here to confirm on. The operator's confirmation happened in
    # the browser, which is what this flag stands in for.
    args += [
        "bash",
        str(papaia_ctl_path(workspace_dir)),
        "restore",
        f"--config-dir={config_dir}",
        f"--backup-dir={backup_dir}",
        f"--restore-point={restore_point}",
        "-y",
    ]
    if restart_clean:
        args.append("--restart-clean")
    return args


def _stack_command(action: str, ctl: str, config_dir: str, clean_up: bool) -> list[str]:
    """The command a stack runner executes, as argv.

    `restart` is two papaia-ctl invocations rather than one verb, because the CLI
    has none -- the same composition `save-config` performs for an add-on. It has
    to run under a shell so that the second invocation is skipped when the first
    fails; leaving a stack down is recoverable, restarting one that never came
    down cleanly is not.

    `clean_up` attaches to the stop half, which is the only half that has a flag
    for it. On a restart that turns the operation into a full recreate: the
    containers are removed and built again from the rendered configuration,
    rather than merely stopped and started.

    Nothing here is interpolated from a request: `config_dir` comes from settings
    and `action` is checked against `STACK_ACTIONS` by the caller.
    """
    stop = f"bash {ctl} stop --config-dir={config_dir}" + (" --clean-up" if clean_up else "")
    start = f"bash {ctl} start --config-dir={config_dir}"
    if action == "restart":
        return ["bash", "-c", f"{stop} && {start}"]
    return ["bash", "-c", stop if action == "stop" else start]


def build_stack_run_args(
    spec: ContainerSpec,
    *,
    action: str,
    workspace_dir: str,
    config_dir: str,
    clean_up: bool = False,
) -> list[str]:
    """Assemble the full `docker run` argv for a stack-wide runner.

    No `--addons`: `papaia-ctl stop` accepts it, and passing it would take every
    add-on down with the core stack. Add-ons are controlled one at a time, by
    design, so a stack action leaves them running.
    """
    if action not in STACK_ACTIONS:
        raise ValueError(f"action {action!r} is not in {sorted(STACK_ACTIONS)}")
    if clean_up and action == "start":
        raise ValueError("--clean-up needs something to stop")

    args = _base_run_args(
        spec,
        name=runner_name(action, STACK_KIND),
        kind=STACK_KIND,
        target=action,
    )
    return args + _stack_command(
        action, str(papaia_ctl_path(workspace_dir)), config_dir, clean_up
    )


async def find_runner(kind: RunnerKind = RESTORE_KIND) -> RunnerStatus | None:
    """Return the current runner of this kind -- running or finished -- or None.

    `docker ps -a` is queried by label rather than by name so a runner started
    before the manager was recreated is still found.
    """
    listing = await _docker(
        "ps",
        "--all",
        "--filter",
        f"label={RUNNER_LABEL_KEY}={kind.label_value}",
        "--format",
        "{{.ID}}",
    )
    ids = [line.strip() for line in listing.splitlines() if line.strip()]
    if not ids:
        return None
    statuses = [
        _status_from_inspect(json.loads(await _docker("inspect", cid)), kind) for cid in ids
    ]
    # A still-running runner is the one that matters; otherwise report the most
    # recently started, which is the outcome the operator is waiting for.
    running = [s for s in statuses if s.is_running]
    if running:
        return running[0]
    return sorted(statuses, key=lambda s: s.started_at)[-1]


def _status_from_inspect(payload: Any, kind: RunnerKind = RESTORE_KIND) -> RunnerStatus:
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise RunnerError("docker inspect returned an unexpected shape for the runner")
    entry = payload[0]
    state = entry.get("State") or {}
    labels = ((entry.get("Config") or {}).get("Labels")) or {}
    name = str(entry.get("Name") or "").lstrip("/")
    # `de.fidonis.restore-point` is what runners started before this label was
    # generalised carry. A restore recreates the manager mid-operation, so the
    # new code routinely inspects a runner the old code started.
    target = str(labels.get(TARGET_LABEL_KEY) or labels.get("de.fidonis.restore-point") or "")
    if not target and name.startswith(kind.name_prefix):
        target = name[len(kind.name_prefix) :]
    status = str(state.get("Status") or "unknown")
    exit_code = state.get("ExitCode")
    return RunnerStatus(
        name=name,
        target=target,
        status=status,
        # While the container runs, docker reports ExitCode 0, which would read
        # as success. Only a container that has stopped has an exit code worth
        # reporting.
        exit_code=int(exit_code) if isinstance(exit_code, int) and status != "running" else None,
        started_at=str(state.get("StartedAt") or ""),
        finished_at=str(state.get("FinishedAt") or ""),
    )


async def start_restore(
    *,
    restore_point: str,
    workspace_dir: str,
    config_dir: str,
    backup_dir: str,
    restart_clean: bool = False,
) -> RunnerStatus:
    """Start a detached restore runner and return its initial status."""
    spec = await self_spec()
    args = build_run_args(
        spec,
        restore_point=restore_point,
        workspace_dir=workspace_dir,
        config_dir=config_dir,
        backup_dir=backup_dir,
        restart_clean=restart_clean,
    )
    logger.info("starting restore runner for %s (clean=%s)", restore_point, restart_clean)
    await _docker(*args)
    status = await find_runner()
    if status is None:
        raise RunnerError("the restore runner exited before it could be inspected")
    return status


async def runner_log(name: str, *, tail: int = LOG_TAIL_LINES) -> str:
    """Return the runner's combined output.

    papaia-ctl writes progress to stdout and warnings to stderr, and the operator
    needs both in order, so the two streams are merged the same way the job log
    merges them for addon verbs.
    """
    try:
        return await _docker("logs", "--tail", str(tail), name)
    except RunnerError as exc:
        logger.warning("cannot read restore runner log: %s", exc)
        return ""


async def clear_runner(name: str, kind: RunnerKind = RESTORE_KIND) -> None:
    """Remove a finished runner so the next operation of its kind can start.

    Refuses while it is still running: `docker rm` would need `--force`, and
    killing papaia-ctl mid-operation leaves the stack in exactly the half-done
    state this whole module exists to avoid.
    """
    status = await find_runner(kind)
    if status is not None and status.name == name and status.is_running:
        raise RunnerError(f"runner {name} is still running")
    await _docker("rm", name)


async def start_stack_action(
    *,
    action: str,
    workspace_dir: str,
    config_dir: str,
    clean_up: bool = False,
) -> RunnerStatus:
    """Start a detached stack-wide runner and return its initial status."""
    spec = await self_spec()
    args = build_stack_run_args(
        spec,
        action=action,
        workspace_dir=workspace_dir,
        config_dir=config_dir,
        clean_up=clean_up,
    )
    logger.info("starting stack runner for %s (clean_up=%s)", action, clean_up)
    await _docker(*args)
    status = await find_runner(STACK_KIND)
    if status is None:
        raise RunnerError("the stack runner exited before it could be inspected")
    return status


def status_to_dict(
    status: RunnerStatus | None, log: str = "", *, target_key: str = "restore_point"
) -> dict[str, Any]:
    """JSON shape for the status endpoint and the polled partial.

    `target_key` names the target field the way its consumer already reads it --
    `restore_point` for the backup page, `action` for the stack controls -- so
    neither template has to learn the other's vocabulary.
    """
    if status is None:
        return {"active": False, "runner": None}
    return {
        "active": True,
        "runner": status.name,
        target_key: status.target,
        "status": status.status,
        "running": status.is_running,
        "succeeded": status.succeeded,
        "exit_code": status.exit_code,
        "started_at": status.started_at,
        "finished_at": status.finished_at,
        "log": log,
    }
