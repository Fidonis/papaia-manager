"""Live status of the stack's containers, grouped by `de.fidonis.module`.

The Compose files label every container with `de.fidonis.module` (which service
bundle it belongs to) and `de.fidonis.role` (what it does inside that bundle),
and most of them define a healthcheck. That is enough to reconstruct the shape
of the running stack from a single `docker ps` -- no papaia-ctl call.

What `docker ps` cannot answer is what is *not* there: a service whose profile
was never enabled and one whose container `papaia-ctl down` removed both show up
as nothing at all. `inventory.py` supplies that half, and `merge_expected` folds
it in, so the page reports the target state against the live one.

Five decisions here are load-bearing:

* `docker ps -a`, not `docker ps`. A stopped Keycloak has to read as *down*,
  and without `-a` its container is simply absent, which is indistinguishable
  from a service that was never deployed.
* No `--filter` at all, and partitioning by the `com.docker.compose.project`
  label in Python. Add-ons run in their own Compose project (one per add-on
  directory), so a project filter excludes them; filtering on
  `label=de.fidonis.module` instead would drop every container that carries no
  module label, which is exactly the case the `other` bucket exists for. One
  unfiltered call answers both, and several papAIa environments on one host stay
  separated because everything outside the known projects is discarded.
* A container with no healthcheck counts as healthy while it runs. Most of the
  stack defines one, but treating its absence as a problem would paint half a
  working deployment yellow. This weighs heavier for add-ons, which rarely
  define healthchecks at all.
* `Exited (0)` alone does not mean "finished one-shot job". A service shut down
  through `papaia-ctl down` terminates just as cleanly as `localai-model-init`
  finishing its work, so the exit code has to be read together with the
  container's restart policy: `unless-stopped` / `always` marks something that
  was meant to keep running, and its exit is an outage no matter how clean.
  That policy is not part of `docker ps` output, hence the second, narrowly
  scoped `docker inspect` below.
* An unreachable Docker socket yields an empty snapshot, never a stack full of
  missing services. Not knowing is not the same as knowing it is gone.

Everything is read-only. Lifecycle control over core services belongs to
papaia-ctl, whose core-verb allowlist deliberately holds nothing but `backup`;
add-ons are controlled from `/addons`.
"""
from __future__ import annotations

import logging
import re
import subprocess
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from app.core.envfile import load_env_file
from app.core.inventory import (
    ExpectedService,
    active_profiles,
    addon_inventory,
    core_inventory,
    module_display_name,
)
from app.core.state import deployment_addons_by_name, load_deployment_yaml

logger = logging.getLogger(__name__)

# Fields requested from `docker ps`, tab-separated. Tab rather than a printable
# separator because no field below can contain one -- names, labels and states
# are all constrained, and `.Status` / `.Ports` are generated text.
_PS_FORMAT = "\t".join(
    (
        "{{.Names}}",
        "{{.State}}",
        "{{.Status}}",
        '{{.Label "com.docker.compose.project"}}',
        '{{.Label "com.docker.compose.service"}}',
        '{{.Label "de.fidonis.module"}}',
        '{{.Label "de.fidonis.role"}}',
        "{{.Ports}}",
    )
)

_PS_FIELD_COUNT = 8

_PS_TIMEOUT_SECONDS = 5

# Restart policy plus container name, for the exited containers whose exit code
# leaves their meaning open. `.Name` comes back with a leading slash.
_INSPECT_FORMAT = "\t".join(("{{.Name}}", "{{.HostConfig.RestartPolicy.Name}}"))

_INSPECT_TIMEOUT_SECONDS = 5

# Restart policies of a container that was meant to keep running. Everything
# else -- `no`, `on-failure`, or no policy at all -- describes a job that is
# allowed to finish.
_SERVICE_RESTART_POLICIES = frozenset({"always", "unless-stopped"})

# How long a `docker ps` result is reused. The header pills render on every page
# and poll, and `/addons` reads the same snapshot, so without this a single page
# load would fork several subprocesses; five seconds is short enough that the
# polled views still feel live.
_CACHE_TTL_SECONDS = 5.0

# `Exited (0) 2 days ago` -- the exit code is what separates a completed
# one-shot container from a crashed service.
_EXIT_CODE_RE = re.compile(r"^Exited \((\d+)\)")

# Published host ports out of `0.0.0.0:8000->3080/tcp, :::8000->3080/tcp`.
_HOST_PORT_RE = re.compile(r":(\d+)->")

# What a declared-but-absent service reports instead of Docker's status text.
_MISSING_STATE = "missing"
_MISSING_STATUS_TEXT = "not deployed"


class ServiceHealth(StrEnum):
    """Derived state of a container or of a whole module."""

    MISSING = "missing"
    STOPPED = "stopped"
    UNHEALTHY = "unhealthy"
    STARTING = "starting"
    UNKNOWN = "unknown"
    COMPLETED = "completed"
    HEALTHY = "healthy"


# Lower is worse. `worst()` walks this order, so it is the single place that
# decides what the header pill shows when several things are wrong at once.
# `MISSING` outranks `STOPPED`: a container that exited at least got as far as
# being created, and its logs are still there to look at.
_SEVERITY: dict[ServiceHealth, int] = {
    ServiceHealth.MISSING: 0,
    ServiceHealth.STOPPED: 1,
    ServiceHealth.UNHEALTHY: 2,
    ServiceHealth.STARTING: 3,
    ServiceHealth.UNKNOWN: 4,
    ServiceHealth.COMPLETED: 5,
    ServiceHealth.HEALTHY: 6,
}


@dataclass
class ServiceContainer:
    """One container of the stack.

    A declared service with no container is represented here too, with an empty
    `name` -- Docker never assigned one -- and `MISSING` health. Everything the
    page shows about it (`service`, `role`) comes from the Compose file.
    """

    name: str
    service: str
    role: str
    state: str
    status_text: str
    ports: list[str] = field(default_factory=list)
    health: ServiceHealth = ServiceHealth.UNKNOWN

    @property
    def sort_key(self) -> str:
        """Containers sort by name; missing ones fall back to the service name.

        Without the fallback every placeholder would sort to the front on its
        empty name, splitting a module's list at an arbitrary point.
        """
        return self.name or self.service


@dataclass
class ServiceModule:
    """All containers sharing one `de.fidonis.module` label."""

    name: str
    containers: list[ServiceContainer] = field(default_factory=list)

    @property
    def health(self) -> ServiceHealth:
        return worst(c.health for c in self.containers)

    @property
    def summary(self) -> str:
        """Short human-readable verdict for the module header row."""
        total = len(self.containers)
        missing = sum(1 for c in self.containers if c.health == ServiceHealth.MISSING)
        stopped = sum(1 for c in self.containers if c.health == ServiceHealth.STOPPED)
        if missing == total and total:
            return "not deployed"
        if missing and stopped:
            return f"{missing + stopped} of {total} containers missing or stopped"
        if missing:
            return f"{missing} of {total} containers not deployed"
        if stopped:
            return f"{stopped} of {total} containers stopped"
        if self.health == ServiceHealth.UNHEALTHY:
            return "healthcheck failing"
        if self.health == ServiceHealth.STARTING:
            return "starting"
        if self.health == ServiceHealth.COMPLETED:
            return "completed"
        return f"{total} container" if total == 1 else f"{total} containers"


@dataclass
class StackSnapshot:
    """One reading of the whole host, split into the parts this manager owns.

    `running_projects` covers *every* Compose project on the host, not just the
    two sections above: `/addons` uses it to tell an installed add-on from a
    running one, including add-ons that are not in the deployment manifest.
    """

    core: list[ServiceModule] = field(default_factory=list)
    addons: list[ServiceModule] = field(default_factory=list)
    running_projects: set[str] = field(default_factory=set)


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------


def worst(values: Iterable[ServiceHealth]) -> ServiceHealth:
    """Return the most severe health value, ignoring completed one-shots.

    A finished `Exited (0)` container says nothing about whether its module is
    serving traffic, so it must not outrank the long-running container next to
    it -- `localai` is model-init plus the inference engine, and only the
    second one matters. When *everything* has completed there is nothing left
    to fall back on, and the group is reported as completed.

    An empty input is `UNKNOWN`: it means Docker told us nothing, not that
    everything is fine.
    """
    items = list(values)
    if not items:
        return ServiceHealth.UNKNOWN
    serving = [v for v in items if v != ServiceHealth.COMPLETED]
    if not serving:
        return ServiceHealth.COMPLETED
    return min(serving, key=lambda v: _SEVERITY[v])


def overall_health(modules: list[ServiceModule]) -> ServiceHealth:
    """Worst module status across a section -- what a header pill shows."""
    return worst(m.health for m in modules)


def count_by_health(modules: list[ServiceModule]) -> dict[ServiceHealth, int]:
    """Number of modules per health value, for the summary row."""
    counts = dict.fromkeys(ServiceHealth, 0)
    for module in modules:
        counts[module.health] += 1
    return counts


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def derive_health(state: str, status_text: str) -> ServiceHealth:
    """Map Docker's state plus status text onto a health value.

    `state` is the machine-readable field (`running`, `exited`, ...); the
    healthcheck result only appears in the human-readable `status_text`, as
    one of `(healthy)`, `(unhealthy)` or `(health: starting)`.
    """
    if state == "running":
        if "(unhealthy)" in status_text:
            return ServiceHealth.UNHEALTHY
        if "(health: starting)" in status_text:
            return ServiceHealth.STARTING
        # `(healthy)` or no healthcheck defined at all.
        return ServiceHealth.HEALTHY
    if state == "created":
        return ServiceHealth.STARTING
    if state == "restarting":
        # A crash loop keeps flipping back to `running`; reporting it as
        # starting would let a container that never stays up look fine.
        return ServiceHealth.UNHEALTHY
    if state == "exited":
        match = _EXIT_CODE_RE.match(status_text)
        if match and match.group(1) == "0":
            return ServiceHealth.COMPLETED
        return ServiceHealth.STOPPED
    if state in ("paused", "dead", "removing"):
        return ServiceHealth.STOPPED
    logger.debug("unrecognised container state %r", state)
    return ServiceHealth.UNKNOWN


def parse_ps_line(line: str) -> tuple[str, str, ServiceContainer] | None:
    """Parse one `docker ps` line into (project, module, container).

    Returns None for a line that does not carry all eight fields, which is
    what a truncated read or an unexpected format change looks like.
    """
    parts = line.split("\t")
    if len(parts) != _PS_FIELD_COUNT:
        logger.debug("skipping unparseable docker ps line %r", line)
        return None

    name, state, status_text, project, service, module, role, ports = (
        p.strip() for p in parts
    )
    if not name:
        return None

    return (
        project,
        module_display_name(module),
        ServiceContainer(
            name=name,
            service=service or name,
            role=role,
            state=state,
            status_text=status_text,
            ports=_parse_ports(ports),
            health=derive_health(state, status_text),
        ),
    )


def parse_ps_output(output: str) -> dict[str, list[ServiceModule]]:
    """Group `docker ps` output by Compose project, then by module.

    Containers with no project label are collected under the empty string, so
    a caller that only knows about named projects drops them without having to
    special-case anything.

    Each project's modules come back worst first, ties broken by name so the
    list does not reshuffle between polls when nothing has changed.
    """
    grouped: dict[str, dict[str, ServiceModule]] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        parsed = parse_ps_line(line)
        if parsed is None:
            continue
        project, module_name, container = parsed
        modules = grouped.setdefault(project, {})
        modules.setdefault(module_name, ServiceModule(name=module_name)).containers.append(
            container
        )

    return {
        project: _worst_first(modules.values()) for project, modules in grouped.items()
    }


def merge_expected(
    modules: list[ServiceModule], expected: Iterable[ExpectedService]
) -> list[ServiceModule]:
    """Add a placeholder for every declared service Docker did not report.

    Matching is by Compose service name, which is unique inside a project --
    that is why this works per project and not across the host: `paperless` and
    `paperless-connect` both define a service called `paperless-mcp`.

    A module that exists only in the target state is created here, which is what
    makes an enabled-but-never-started profile visible at all. Containers Docker
    reported that are *not* in the target state are left alone: they are running,
    which is a fact the page has no business hiding just because the manifest
    disagrees.
    """
    by_name = {m.name: m for m in modules}
    seen = {c.service for m in modules for c in m.containers}

    for item in expected:
        if item.service in seen:
            continue
        module = by_name.get(item.module)
        if module is None:
            module = ServiceModule(name=item.module)
            by_name[item.module] = module
        module.containers.append(
            ServiceContainer(
                name="",
                service=item.service,
                role=item.role,
                state=_MISSING_STATE,
                status_text=_MISSING_STATUS_TEXT,
                health=ServiceHealth.MISSING,
            )
        )

    for module in by_name.values():
        module.containers.sort(key=lambda c: c.sort_key)
    return _worst_first(by_name.values())


def parse_inspect_output(output: str) -> dict[str, str]:
    """Map container name to restart policy from `docker inspect` output.

    Docker reports the name with a leading slash; the rest of this module
    works with the bare name `docker ps` prints.
    """
    policies: dict[str, str] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) != 2:
            logger.debug("skipping unparseable docker inspect line %r", line)
            continue
        name, policy = (p.strip() for p in parts)
        if name:
            policies[name.removeprefix("/")] = policy
    return policies


def apply_restart_policies(
    modules: list[ServiceModule], policies: Mapping[str, str]
) -> list[ServiceModule]:
    """Re-read completed containers in the light of their restart policy.

    `Exited (0)` is where a finished one-shot job and a service someone shut
    down look exactly alike. The restart policy separates them: a container
    Docker was told to keep alive has no business being gone, so its exit is
    an outage regardless of the code it exited with.

    A container missing from `policies` keeps its parsed value. That is the
    conservative direction -- when Docker gives no answer, an unfounded outage
    on the header pill is worse than a stopped container reading as completed.

    Returns the list re-sorted, since a container turning stopped can change
    where its module belongs.
    """
    for module in modules:
        for container in module.containers:
            if (
                container.health is ServiceHealth.COMPLETED
                and policies.get(container.name) in _SERVICE_RESTART_POLICIES
            ):
                container.health = ServiceHealth.STOPPED
    return _worst_first(modules)


def _worst_first(modules: Iterable[ServiceModule]) -> list[ServiceModule]:
    """Modules ordered worst first, ties broken by name.

    The name tiebreak keeps the list from reshuffling between polls when
    nothing has changed.
    """
    return sorted(modules, key=lambda m: (_SEVERITY[m.health], m.name))


def _parse_ports(raw: str) -> list[str]:
    """Published host ports, deduplicated and in the order Docker listed them.

    Docker reports the IPv4 and IPv6 binding of the same publish separately;
    showing `8000` once is the useful part.
    """
    seen: list[str] = []
    for match in _HOST_PORT_RE.finditer(raw):
        port = match.group(1)
        if port not in seen:
            seen.append(port)
    return seen


# ---------------------------------------------------------------------------
# Docker query
# ---------------------------------------------------------------------------


def compose_project(config_dir: str) -> str:
    """Compose project name of this deployment, from the core `.env`.

    Several papAIa environments can share a host (`papaia-dev`, `papaia-demo`,
    ...), so the project name is what keeps one manager from reporting on
    another environment's containers.
    """
    env = load_env_file(Path(config_dir) / ".env")
    return env.get("COMPOSE_PROJECT_NAME") or "papaia"


def query_containers() -> str | None:
    """Raw `docker ps -a` output for the whole host, or None on failure.

    Unfiltered on purpose -- see the module docstring. An unreachable Docker
    socket is a normal state here (unit tests, a host mid-restore), so it is
    logged at debug level and surfaces as "unknown" rather than as an error.
    """
    try:
        result = subprocess.run(
            ["docker", "ps", "-a", "--format", _PS_FORMAT],
            capture_output=True,
            text=True,
            timeout=_PS_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("could not reach Docker socket: %s", exc)
        return None

    if result.returncode != 0:
        logger.debug("docker ps failed: %s", result.stderr)
        return None
    return result.stdout


def query_restart_policies(names: list[str]) -> dict[str, str]:
    """Restart policy per container name, empty on failure.

    Asked only about the containers whose state is ambiguous, so in a healthy
    deployment this is one lookup for `localai-model-init` and nothing else.
    """
    if not names:
        return {}

    try:
        result = subprocess.run(
            ["docker", "inspect", "--type", "container", "--format", _INSPECT_FORMAT, *names],
            capture_output=True,
            text=True,
            timeout=_INSPECT_TIMEOUT_SECONDS,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError) as exc:
        logger.debug("could not reach Docker socket: %s", exc)
        return {}

    # A container removed between `ps` and `inspect` makes Docker exit non-zero
    # while still reporting the ones it did find, so stdout is worth parsing
    # either way.
    if result.returncode != 0:
        logger.debug("docker inspect reported an error: %s", result.stderr)
    return parse_inspect_output(result.stdout)


# ---------------------------------------------------------------------------
# Snapshot
# ---------------------------------------------------------------------------


def addon_projects(config_dir: str, workspace_dir: str) -> dict[str, list[ExpectedService]]:
    """Compose project name to declared services, for every active add-on.

    The project name is the add-on directory's basename, because that is what
    `lib/sh/addon.sh` pins with `docker compose -p` -- and what
    `state.compute_status` already matches against. Deriving it the same way in
    both places is what keeps `/addons` and `/services` from disagreeing.

    `path` is absolute for everything papaia-ctl writes; a relative one is
    resolved against the workspace, the root the manifest is relative to.
    """
    deployment = load_deployment_yaml(config_dir)
    projects: dict[str, list[ExpectedService]] = {}
    for name, entry in deployment_addons_by_name(deployment).items():
        if not entry.get("active", False):
            continue
        raw_path = str(entry.get("path", ""))
        if not raw_path:
            continue
        path = Path(raw_path)
        if not path.is_absolute():
            path = Path(workspace_dir) / path
        projects[path.name] = addon_inventory(str(path), fallback_module=name)
    return projects


def build_snapshot(config_dir: str, workspace_dir: str, output: str) -> StackSnapshot:
    """Turn one `docker ps` reading plus the declared state into a snapshot.

    Split out from `load_snapshot` so the whole merge is testable without a
    Docker daemon -- which is also the state CI runs in.
    """
    by_project = parse_ps_output(output)
    running = {
        project
        for project, modules in by_project.items()
        if project
        and any(c.state == "running" for m in modules for c in m.containers)
    }

    core_project = compose_project(config_dir)
    core = merge_expected(
        by_project.get(core_project, []),
        core_inventory(workspace_dir, active_profiles(config_dir)),
    )

    addons: list[ServiceModule] = []
    for project, expected in sorted(addon_projects(config_dir, workspace_dir).items()):
        addons.extend(merge_expected(by_project.get(project, []), expected))

    return StackSnapshot(core=core, addons=addons, running_projects=running)


# (monotonic timestamp, snapshot). One manager process serves one deployment,
# so a single entry is all this ever needs.
_CACHE: tuple[float, StackSnapshot] | None = None


def load_snapshot(config_dir: str, workspace_dir: str) -> StackSnapshot:
    """The whole stack as it is and as it should be, cached for the TTL.

    Blocking: call it from a thread, the way the routers do.
    """
    global _CACHE

    now = time.monotonic()
    if _CACHE is not None and now - _CACHE[0] < _CACHE_TTL_SECONDS:
        return _CACHE[1]

    output = query_containers()
    # A failed query keeps no cache entry: the next request should retry rather
    # than pin "unknown" in place for the TTL. It also returns *nothing* rather
    # than the declared state -- reporting every service as missing because the
    # socket was unreachable would invent an outage out of ignorance.
    if output is None:
        return StackSnapshot()

    snapshot = build_snapshot(config_dir, workspace_dir, output)
    completed = [
        c.name
        for section in (snapshot.core, snapshot.addons)
        for m in section
        for c in m.containers
        if c.health is ServiceHealth.COMPLETED
    ]
    policies = query_restart_policies(completed)
    snapshot.core = apply_restart_policies(snapshot.core, policies)
    snapshot.addons = apply_restart_policies(snapshot.addons, policies)

    _CACHE = (now, snapshot)
    return snapshot


def invalidate_snapshot() -> None:
    """Drop the cached reading because something just changed Docker state.

    The TTL exists to keep polled views from forking a subprocess per request,
    not to defer known-stale data. Every papaia-ctl run goes through
    `core.ctl`, which calls this when the subprocess exits -- so an add-on that
    was just started reads as running immediately instead of up to five seconds
    later, both on the services page and in `compute_status`.
    """
    global _CACHE

    _CACHE = None
