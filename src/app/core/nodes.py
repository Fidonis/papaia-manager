"""The machines of a distributed installation, and how to run something on one.

A single-host installation has no topology block and none of this runs: `is_distributed`
reads `deployment.yaml` and nothing else, so no process is started and no page appears, and
every call site keeps its default `node=None`, the path it always took. That also holds for
a core that predates topologies.

In a distributed installation the core owns the channel: keys, pinned host keys and the
verbs of a node's forced command. `SshExecutor` therefore does not open SSH connections of
its own. It goes through the core's `node-exec`, the same code `papaia-ctl` uses, and the
SSH client it needs is in the image. The keys are in the configuration tree, under
`nodes/<name>/ssh/`; the manager and its runners already mount that tree. A private key is
never read here, so none reaches a log; a key's path can, in a message of the core.

What a node's forced command does not offer stays on the controller: `docker` (a node's
containers are in the core's merged `status`) and Git (a node gets its release as an
archive). The call sites take `node` and refuse it rather than pretend.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from app.core import ctl, runner
from app.core.state import load_deployment_yaml
from app.i18n import gettext as _

logger = logging.getLogger(__name__)

DISTRIBUTED = "distributed"
LOCAL_EXECUTOR = "local"

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,30}\Z")
_FINGERPRINT_RE = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}=?\Z")

# How long one poll may take per node, and how long its result is reused: the page refreshes
# on a timer, and every refresh is an SSH round trip to every node.
_CHECK_LIMIT_SECONDS = 60.0
_LINKS_LIMIT_SECONDS = 45.0
_OVERVIEW_TTL_SECONDS = 30.0


class NodeError(Exception):
    """A node that does not exist, or a request the core refused."""


def valid_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))


def valid_fingerprint(value: str) -> bool:
    return bool(_FINGERPRINT_RE.match(value))


def _topology(config_dir: str) -> dict[str, Any]:
    try:
        block = load_deployment_yaml(config_dir).get("topology")
    except (OSError, ValueError, yaml.YAMLError, AttributeError):
        # The core reports a broken file its own way; here it is not a distributed one.
        # ValueError covers a file that is not UTF-8.
        return {}
    return block if isinstance(block, dict) else {}


def is_distributed(config_dir: str) -> bool:
    return _topology(config_dir).get("mode") == DISTRIBUTED


@dataclass(frozen=True)
class NodeRef:
    """A node as `deployment.yaml` describes it."""

    name: str
    address: str
    executor: str
    controller: bool

    @property
    def remote(self) -> bool:
        return self.executor != LOCAL_EXECUTOR


def controller_name(config_dir: str) -> str:
    return str(_topology(config_dir).get("controller") or "")


def registry(config_dir: str) -> dict[str, NodeRef]:
    """The installation's nodes by name; empty on a single host."""
    block = _topology(config_dir)
    if block.get("mode") != DISTRIBUTED:
        return {}
    raw = block.get("nodes")
    if not isinstance(raw, dict):
        return {}
    controller = str(block.get("controller") or "")
    return {
        str(name): NodeRef(
            name=str(name),
            address=str((body or {}).get("address") or ""),
            executor=str((body or {}).get("executor") or "ssh"),
            controller=str(name) == controller,
        )
        for name, body in raw.items()
    }


class Executor(Protocol):
    """Run this on that node. Output and exit codes come back as they do locally."""

    def run_ctl(self, verb: str) -> AsyncGenerator[str, None]:
        """Stream a stack verb for everything on this node; `CtlError` after a non-zero
        exit. It takes no arguments: `papaia-ctl` flags and a node verb's service names are
        different things, and selecting services is the core's `--profiles`."""
        ...

    async def run_py(self, command: str, *, limit: float | None = None) -> tuple[int, str, str]:
        """Exit code, stdout and stderr of a read-only command."""
        ...

    async def docker(self, *args: str) -> str:
        """Stdout of a docker command; refused where there is no raw docker."""
        ...

    async def put_bundle(self) -> None:
        """Hand the node the bundle the last render made for it."""
        ...


@dataclass(frozen=True)
class LocalExecutor:
    """This machine: every call takes the path it took before there were nodes."""

    workspace_dir: str
    config_dir: str

    async def run_ctl(self, verb: str) -> AsyncGenerator[str, None]:
        lines = await ctl.run_core_verb(
            verb=verb, workspace_dir=self.workspace_dir, config_dir=self.config_dir
        )
        async for line in lines:
            yield line

    async def run_py(self, command: str, *, limit: float | None = None) -> tuple[int, str, str]:
        return await ctl.run_py_cli(
            command=command,
            workspace_dir=self.workspace_dir,
            config_dir=self.config_dir,
            limit=limit,
        )

    async def docker(self, *args: str) -> str:
        return await runner._docker(*args)  # noqa: SLF001 - the one module that wraps it

    async def put_bundle(self) -> None:
        """Nothing to send: the controller reads its bundle from its own tree."""


@dataclass(frozen=True)
class SshExecutor:
    """A remote node, through the core's `node-exec` and the node's forced command."""

    node: str
    workspace_dir: str
    config_dir: str

    async def run_ctl(self, verb: str) -> AsyncGenerator[str, None]:
        lines = await ctl.run_core_verb(
            verb=verb,
            workspace_dir=self.workspace_dir,
            config_dir=self.config_dir,
            node=self.node,
        )
        async for line in lines:
            yield line

    async def run_py(self, command: str, *, limit: float | None = None) -> tuple[int, str, str]:
        return await ctl.run_py_cli(
            command=command,
            workspace_dir=self.workspace_dir,
            config_dir=self.config_dir,
            limit=limit,
            node=self.node,
        )

    async def docker(self, *args: str) -> str:
        return await runner._docker(*args, node=self.node)  # noqa: SLF001

    async def put_bundle(self) -> None:
        code, out, err = await ctl.run_node_command(
            command="node-push",
            workspace_dir=self.workspace_dir,
            config_dir=self.config_dir,
            extra_flags=[f"--name={self.node}"],
            limit=300.0,
        )
        if code != 0:
            raise NodeError(_last_line(err or out) or _("node-push failed"))


def executor_for(name: str, *, workspace_dir: str, config_dir: str) -> Executor:
    """The executor of a node. An installation's controller, and a single host, are local."""
    nodes = registry(config_dir)
    if not nodes:
        return LocalExecutor(workspace_dir, config_dir)
    node = nodes.get(name)
    if node is None:
        raise NodeError(_("no node named '%(name)s'") % {"name": name})
    if node.remote:
        return SshExecutor(name, workspace_dir, config_dir)
    return LocalExecutor(workspace_dir, config_dir)


def _last_line(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    return lines[-1].removeprefix("ERROR: ")[:300] if lines else ""


# ---------------------------------------------------------------------------
# The upgrade lock
# ---------------------------------------------------------------------------

_LOCK_FILE = "upgrade.lock"


def lock_reason(config_dir: str, workspace_dir: str) -> str:
    """Why `start`, `backup` and the like are refused now, or "" when they are not.

    The same two reasons the core has (`cluster.skew`), read from the same files, so a page
    can say so instead of queueing a job that fails. Reads files only: empty on a single host.
    """
    known = registry(config_dir)
    if not known:
        return ""
    base = Path(config_dir)
    lock = base / _LOCK_FILE
    if lock.is_file():
        try:
            data = json.loads(lock.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = {}
        versions = {"source": data.get("from", "?"), "target": data.get("to", "?")}
        restore_point = data.get("restore_point")
        if restore_point:
            return _(
                "An upgrade from %(source)s to %(target)s has not finished. Run it again, or"
                " restore its restore point %(restore_point)s."
            ) % {**versions, "restore_point": restore_point}
        return _(
            "An upgrade from %(source)s to %(target)s has not finished, and it took no restore"
            " point. Run it again, or clear the lock with papaia-ctl start --addons --unlock on"
            " the host papaia-manager runs on."
        ) % versions
    from app.core import upgrade  # noqa: PLC0415 - upgrade reaches this module through runner

    # The checkout first, as the core reads it; the recorded version answers for one without.
    current = upgrade.current_version(config_dir, workspace_dir)
    version = current.checkout or current.recorded
    if not version:
        return ""
    behind = []
    for node in known.values():
        if not node.remote:
            continue
        try:
            state = json.loads(
                (base / "nodes" / node.name / "state.json").read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            continue
        release = state.get("release") if isinstance(state, dict) else None
        if release and release != version:
            behind.append(f"{node.name} ({release})")
    if behind:
        return _(
            "The controller runs %(version)s, but these nodes do not: %(nodes)s. Move all nodes"
            " together with an upgrade."
        ) % {"version": version, "nodes": ", ".join(behind)}
    return ""


# ---------------------------------------------------------------------------
# What the Nodes page shows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LinkState:
    peer: str
    ok: bool
    reason: str = ""


@dataclass(frozen=True)
class NodeState:
    """One node: what the configuration says, and what it answered just now."""

    name: str
    address: str
    executor: str
    controller: bool
    services: tuple[str, ...]
    host_key_pinned: bool
    reachable: bool
    reason: str = ""
    version: str = ""
    links: tuple[LinkState, ...] = field(default=())
    links_known: bool = False
    # The node answered and holds no bundle yet: it runs nothing.
    no_bundle: bool = False

    @property
    def links_ok(self) -> bool:
        return self.links_known and bool(self.links) and all(link.ok for link in self.links)

    @property
    def no_links(self) -> bool:
        return self.links_known and not self.links


def _links_from_answer(text: str) -> tuple[LinkState, ...] | None:
    """The link probes a node answered with; None when the answer is unreadable."""
    lines = text.strip().splitlines()
    try:
        data = json.loads(lines[-1])
    except (ValueError, IndexError):
        return None
    links = data.get("links") if isinstance(data, dict) else None
    if not isinstance(links, list):
        return None
    return tuple(
        LinkState(
            peer=str(item.get("peer", "")),
            ok=bool(item.get("ok")),
            reason=str(item.get("reason") or ""),
        )
        for item in links
        if isinstance(item, dict)
    )


async def _links(name: str, *, workspace_dir: str, config_dir: str) -> tuple[LinkState, ...] | None:
    # Through node-exec on every node, the controller too. The `links` verb and not `doctor`:
    # this runs on every refresh, and `doctor` also has Docker size every volume.
    try:
        code, out, _err = await ctl.run_py_cli(
            command="links",
            workspace_dir=workspace_dir,
            config_dir=config_dir,
            limit=_LINKS_LIMIT_SECONDS,
            node=name,
        )
    except ctl.CtlError:
        return None
    return _links_from_answer(out) if code == 0 else None


async def _node_state(row: dict[str, Any], *, workspace_dir: str, config_dir: str) -> NodeState:
    raw_check = row.get("check")
    check: dict[str, Any] = raw_check if isinstance(raw_check, dict) else {}
    reachable = bool(check.get("reachable"))
    name = str(row.get("name", ""))
    links = (
        await _links(name, workspace_dir=workspace_dir, config_dir=config_dir)
        if reachable
        else None
    )
    return NodeState(
        name=name,
        address=str(row.get("address", "")),
        executor=str(row.get("executor", "")),
        controller=bool(row.get("controller")),
        services=tuple(str(s) for s in row.get("services") or []),
        host_key_pinned=bool(row.get("host_key_pinned")),
        reachable=reachable,
        reason=str(check.get("reason") or ""),
        version=str(check.get("papaia_version") or row.get("release") or ""),
        links=links or (),
        links_known=links is not None,
        no_bundle=reachable and not row.get("controller") and not check.get("bundle_version"),
    )


_overview_cache: tuple[float, list[NodeState]] | None = None


def invalidate_overview() -> None:
    global _overview_cache
    _overview_cache = None


async def overview(
    *, workspace_dir: str, config_dir: str, fresh: bool = False
) -> list[NodeState]:
    """Every node with its answer. A node that does not answer is a state of the list."""
    global _overview_cache
    now = time.monotonic()
    if (
        not fresh
        and _overview_cache is not None
        and now - _overview_cache[0] < _OVERVIEW_TTL_SECONDS
    ):
        return _overview_cache[1]
    code, out, err = await ctl.run_py_cli(
        command="node-list",
        workspace_dir=workspace_dir,
        config_dir=config_dir,
        extra_flags=["--json", "--check"],
        limit=_CHECK_LIMIT_SECONDS,
    )
    if code != 0:
        # The list is what enrolling and removing a node start from: without the nodes'
        # answers it still is one.
        logger.warning("node-list --check failed: %s", _last_line(err or out))
        code, out, err = await ctl.run_py_cli(
            command="node-list",
            workspace_dir=workspace_dir,
            config_dir=config_dir,
            extra_flags=["--json"],
            limit=_CHECK_LIMIT_SECONDS,
        )
    if code != 0:
        raise NodeError(_last_line(err or out) or _("the core could not list the nodes"))
    try:
        rows = json.loads(out)
    except ValueError as exc:
        raise NodeError(_("the core's node list is unreadable")) from exc
    states = await asyncio.gather(
        *(
            _node_state(row, workspace_dir=workspace_dir, config_dir=config_dir)
            for row in rows
            if isinstance(row, dict)
        )
    )
    _overview_cache = (time.monotonic(), list(states))
    return list(states)


@dataclass(frozen=True)
class HostKey:
    kind: str
    fingerprint: str


async def host_keys(name: str, *, workspace_dir: str, config_dir: str) -> list[HostKey]:
    """The SSH host keys a node offers right now, for the administrator to confirm."""
    code, out, err = await ctl.run_py_cli(
        command="node-keyscan",
        workspace_dir=workspace_dir,
        config_dir=config_dir,
        extra_flags=[f"--name={name}"],
        limit=_CHECK_LIMIT_SECONDS,
    )
    if code != 0:
        raise NodeError(_last_line(err or out) or _("the node offered no host key"))
    keys = []
    for line in out.splitlines():
        parts = line.strip().split("\t")
        if len(parts) == 3 and parts[0] == "HOSTKEY":
            keys.append(HostKey(kind=parts[1], fingerprint=parts[2]))
    return keys


# ---------------------------------------------------------------------------
# Changing the set of nodes
# ---------------------------------------------------------------------------


async def _write(
    command: str, flags: list[str], *, workspace_dir: str, config_dir: str, limit: float = 120.0
) -> tuple[str, str]:
    """(stdout, stderr) of a command that worked; `NodeError` with its reason otherwise."""
    code, out, err = await ctl.run_node_command(
        command=command,
        workspace_dir=workspace_dir,
        config_dir=config_dir,
        extra_flags=flags,
        limit=limit,
    )
    invalidate_overview()
    if code != 0:
        raise NodeError(_last_line(err or out) or _("%(command)s failed") % {"command": command})
    return out, err


def join_archive_path(config_dir: str, name: str) -> str:
    return f"{config_dir.rstrip('/')}/nodes/{name}/papaia-join-{name}.tar.gz"


def join_commands(config_dir: str, node: NodeState) -> str:
    """What an administrator runs to set a node up from its join archive."""
    name = node.name
    return (
        f"scp {join_archive_path(config_dir, name)} root@{node.address}:\n"
        f"tar -xzf papaia-join-{name}.tar.gz && sudo ./papaia-join-{name}/install.sh"
    )


@dataclass(frozen=True)
class NewNode:
    name: str
    address: str
    ssh_user: str = ""
    ssh_port: int = 0
    root: str = ""


async def add_node(node: NewNode, *, workspace_dir: str, config_dir: str) -> str:
    """Register a node and write its join archive. Returns the archive's path."""
    flags = [f"--name={node.name}", f"--address={node.address}"]
    if node.ssh_user:
        flags.append(f"--ssh-user={node.ssh_user}")
    if node.ssh_port:
        flags.append(f"--ssh-port={node.ssh_port}")
    if node.root:
        flags.append(f"--root={node.root}")
    await _write("node-register", flags, workspace_dir=workspace_dir, config_dir=config_dir)
    archive = join_archive_path(config_dir, node.name)
    await _write(
        "node-join-bundle",
        [f"--name={node.name}", f"--output={archive}"],
        workspace_dir=workspace_dir,
        config_dir=config_dir,
    )
    return archive


async def remove_node(name: str, *, workspace_dir: str, config_dir: str) -> list[str]:
    """Remove a node. Returns what the core says is left to do; a removal that worked can
    still have left a gateway running and a certificate accepted."""
    out, err = await _write(
        "node-unregister", [f"--name={name}"], workspace_dir=workspace_dir, config_dir=config_dir
    )
    lines = [line.strip() for line in (*err.splitlines(), *out.splitlines()) if line.strip()]
    return [line.removeprefix("WARNING: ") for line in lines if not line.startswith("Node ")]


async def send_bundle(name: str, *, workspace_dir: str, config_dir: str) -> None:
    """Hand a node its release and its bundle, the step after enrolling it."""
    try:
        await executor_for(name, workspace_dir=workspace_dir, config_dir=config_dir).put_bundle()
    finally:
        invalidate_overview()


async def pin_host_key(
    name: str, fingerprint: str, *, workspace_dir: str, config_dir: str
) -> None:
    await _write(
        "node-pin",
        [f"--name={name}", f"--fingerprint={fingerprint}"],
        workspace_dir=workspace_dir,
        config_dir=config_dir,
    )
