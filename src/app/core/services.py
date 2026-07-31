"""Live status of the core stack's containers, grouped by `de.fidonis.module`.

The core Compose files label every container with `de.fidonis.module` (which
service bundle it belongs to) and `de.fidonis.role` (what it does inside that
bundle), and most of them define a healthcheck. That is enough to reconstruct
the shape of the running stack from a single `docker ps` -- no Compose file
parsing, no papaia-ctl call.

Three decisions here are load-bearing:

* `docker ps -a`, not `docker ps`. A stopped Keycloak has to read as *down*,
  and without `-a` its container is simply absent, which is indistinguishable
  from a profile that was never enabled.
* A container with no healthcheck counts as healthy while it runs. Most of the
  stack defines one, but treating its absence as a problem would paint half a
  working deployment yellow.
* `Exited (0)` alone does not mean "finished one-shot job". A service shut down
  through `papaia-ctl down` terminates just as cleanly as `localai-model-init`
  finishing its work, so the exit code has to be read together with the
  container's restart policy: `unless-stopped` / `always` marks something that
  was meant to keep running, and its exit is an outage no matter how clean.
  That policy is not part of `docker ps` output, hence the second, narrowly
  scoped `docker inspect` below.

Everything is read-only. Lifecycle control over core services belongs to
papaia-ctl, whose core-verb allowlist deliberately holds nothing but `backup`.
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

logger = logging.getLogger(__name__)

# Fields requested from `docker ps`, tab-separated. Tab rather than a printable
# separator because no field below can contain one -- names, labels and states
# are all constrained, and `.Status` / `.Ports` are generated text.
_PS_FORMAT = "\t".join(
    (
        "{{.Names}}",
        "{{.State}}",
        "{{.Status}}",
        '{{.Label "com.docker.compose.service"}}',
        '{{.Label "de.fidonis.module"}}',
        '{{.Label "de.fidonis.role"}}',
        "{{.Ports}}",
    )
)

_PS_TIMEOUT_SECONDS = 5

# Restart policy plus container name, for the exited containers whose exit code
# leaves their meaning open. `.Name` comes back with a leading slash.
_INSPECT_FORMAT = "\t".join(("{{.Name}}", "{{.HostConfig.RestartPolicy.Name}}"))

_INSPECT_TIMEOUT_SECONDS = 5

# Restart policies of a container that was meant to keep running. Everything
# else -- `no`, `on-failure`, or no policy at all -- describes a job that is
# allowed to finish.
_SERVICE_RESTART_POLICIES = frozenset({"always", "unless-stopped"})

# How long a `docker ps` result is reused. The header pill renders on every
# page and polls, so without this every request would fork a subprocess; five
# seconds is short enough that the polled views still feel live.
_CACHE_TTL_SECONDS = 5.0

# `Exited (0) 2 days ago` -- the exit code is what separates a completed
# one-shot container from a crashed service.
_EXIT_CODE_RE = re.compile(r"^Exited \((\d+)\)")

# Published host ports out of `0.0.0.0:8000->3080/tcp, :::8000->3080/tcp`.
_HOST_PORT_RE = re.compile(r":(\d+)->")

# Labels are namespaced by product; the prefix carries no information once the
# containers are grouped, so it is dropped for display.
_MODULE_PREFIX = "papaia-"

# Containers that carry no module label at all -- add-on containers sharing the
# project, or a core service whose label was dropped in a local edit.
UNGROUPED_MODULE = "other"


class ServiceHealth(StrEnum):
    """Derived state of a container or of a whole module."""

    STOPPED = "stopped"
    UNHEALTHY = "unhealthy"
    STARTING = "starting"
    UNKNOWN = "unknown"
    COMPLETED = "completed"
    HEALTHY = "healthy"


# Lower is worse. `worst()` walks this order, so it is the single place that
# decides what the header pill shows when several things are wrong at once.
_SEVERITY: dict[ServiceHealth, int] = {
    ServiceHealth.STOPPED: 0,
    ServiceHealth.UNHEALTHY: 1,
    ServiceHealth.STARTING: 2,
    ServiceHealth.UNKNOWN: 3,
    ServiceHealth.COMPLETED: 4,
    ServiceHealth.HEALTHY: 5,
}


@dataclass
class ServiceContainer:
    """One container of the core stack."""

    name: str
    service: str
    role: str
    state: str
    status_text: str
    ports: list[str] = field(default_factory=list)
    health: ServiceHealth = ServiceHealth.UNKNOWN


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
        broken = [c for c in self.containers if c.health == ServiceHealth.STOPPED]
        if broken:
            return f"{len(broken)} of {total} containers stopped"
        if self.health == ServiceHealth.UNHEALTHY:
            return "healthcheck failing"
        if self.health == ServiceHealth.STARTING:
            return "starting"
        if self.health == ServiceHealth.COMPLETED:
            return "completed"
        return f"{total} container" if total == 1 else f"{total} containers"


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
    """Worst module status across the stack -- what the header pill shows."""
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


def parse_ps_line(line: str) -> tuple[str, ServiceContainer] | None:
    """Parse one `docker ps` line into (module, container).

    Returns None for a line that does not carry all seven fields, which is
    what a truncated read or an unexpected format change looks like.
    """
    parts = line.split("\t")
    if len(parts) != 7:
        logger.debug("skipping unparseable docker ps line %r", line)
        return None

    name, state, status_text, service, module, role, ports = (p.strip() for p in parts)
    if not name:
        return None

    module = module.removeprefix(_MODULE_PREFIX) if module else UNGROUPED_MODULE
    return module, ServiceContainer(
        name=name,
        service=service or name,
        role=role,
        state=state,
        status_text=status_text,
        ports=_parse_ports(ports),
        health=derive_health(state, status_text),
    )


def parse_ps_output(output: str) -> list[ServiceModule]:
    """Group `docker ps` output into modules, worst module first.

    Ties are broken by name so the list does not reshuffle between polls when
    nothing has changed.
    """
    grouped: dict[str, ServiceModule] = {}
    for line in output.splitlines():
        if not line.strip():
            continue
        parsed = parse_ps_line(line)
        if parsed is None:
            continue
        module_name, container = parsed
        grouped.setdefault(module_name, ServiceModule(name=module_name)).containers.append(
            container
        )

    for module in grouped.values():
        module.containers.sort(key=lambda c: c.name)

    return _worst_first(grouped.values())


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


def query_containers(project: str) -> str | None:
    """Raw `docker ps -a` output for one Compose project, or None on failure.

    Mirrors `state.load_running_compose_projects`: an unreachable Docker
    socket is a normal state here (unit tests, a host mid-restore), so it is
    logged at debug level and surfaces as "unknown" rather than as an error.
    """
    try:
        result = subprocess.run(
            [
                "docker",
                "ps",
                "-a",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--format",
                _PS_FORMAT,
            ],
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


# project -> (monotonic timestamp, modules)
_CACHE: dict[str, tuple[float, list[ServiceModule]]] = {}


def load_modules(project: str) -> list[ServiceModule]:
    """Modules of one Compose project, cached for `_CACHE_TTL_SECONDS`.

    Blocking: call it from a thread, the way the routers do.
    """
    now = time.monotonic()
    cached = _CACHE.get(project)
    if cached is not None and now - cached[0] < _CACHE_TTL_SECONDS:
        return cached[1]

    output = query_containers(project)
    # A failed query keeps no cache entry: the next request should retry
    # rather than pin "unknown" in place for the TTL.
    if output is None:
        return []

    modules = parse_ps_output(output)
    completed = [
        c.name for m in modules for c in m.containers if c.health is ServiceHealth.COMPLETED
    ]
    modules = apply_restart_policies(modules, query_restart_policies(completed))
    _CACHE[project] = (now, modules)
    return modules
