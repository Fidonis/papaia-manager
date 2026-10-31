"""The machines of a distributed installation, and how to run something on one.

A single-host installation has no topology block and none of this runs: `is_distributed`
reads `deployment.yaml` and nothing else, so no process is started and no page appears, and
every call site keeps its default `node=None`, the path it always took. That also holds for
a core that predates topologies.

In a distributed installation the core owns the channel: keys, pinned host keys and the
verbs of a node's forced command. `SshExecutor` therefore does not open SSH connections of
its own. It goes through the core's `node-exec`, the same code `papaia-ctl` uses, and the
SSH client it needs is in the image. The keys are in the configuration tree, under
`nodes/<name>/ssh/`; the manager and its runners already mount that tree, and neither the
keys nor their paths are ever written to a log.

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
_DOCTOR_LIMIT_SECONDS = 45.0
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
    except (OSError, yaml.YAMLError, AttributeError):
        # The core reports a broken file its own way; here it is not a distributed one.
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

    def run_ctl(self, verb: str, flags: list[str] | None = None) -> AsyncGenerator[str, None]:
        """Stream a stack verb; `CtlError` after a non-zero exit."""
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

    async def run_ctl(
        self, verb: str, flags: list[str] | None = None
    ) -> AsyncGenerator[str, None]:
        lines = await ctl.run_core_verb(
            verb=verb,
            workspace_dir=self.workspace_dir,
            config_dir=self.config_dir,
            extra_flags=flags,
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

    async def run_ctl(
        self, verb: str, flags: list[str] | None = None
    ) -> AsyncGenerator[str, None]:
        lines = await ctl.run_core_verb(
            verb=verb,
            workspace_dir=self.workspace_dir,
            config_dir=self.config_dir,
            extra_flags=flags,
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

    @property
    def links_ok(self) -> bool:
        return self.links_known and all(link.ok for link in self.links)


def _links_from_doctor(text: str) -> tuple[LinkState, ...] | None:
    """The link probes of a node's `doctor` answer; None when it is unreadable."""
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
    # Through node-exec on every node, the controller too: `doctor` there is the machine's
    # own report, and only the node verb answers for its links.
    try:
        code, out, _err = await ctl.run_py_cli(
            command="doctor",
            workspace_dir=workspace_dir,
            config_dir=config_dir,
            limit=_DOCTOR_LIMIT_SECONDS,
            node=name,
        )
    except ctl.CtlError:
        return None
    return _links_from_doctor(out) if code == 0 else None


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
) -> str:
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
    return out


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


async def remove_node(name: str, *, workspace_dir: str, config_dir: str) -> None:
    await _write(
        "node-unregister", [f"--name={name}"], workspace_dir=workspace_dir, config_dir=config_dir
    )


async def pin_host_key(
    name: str, fingerprint: str, *, workspace_dir: str, config_dir: str
) -> None:
    await _write(
        "node-pin",
        [f"--name={name}", f"--fingerprint={fingerprint}"],
        workspace_dir=workspace_dir,
        config_dir=config_dir,
    )
