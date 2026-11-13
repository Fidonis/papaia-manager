"""Where LocalAI runs: in its container, or as a process on an Apple Silicon Mac.

The core stores the choice as `LOCALAI_RUNTIME` in the root `.env` (`container`, the
default, or `native`). With `native`, LocalAI runs on the Mac's GPU outside Docker: the
`localai` service is scaled to no replicas, `localai-model-init` still runs and fills a
bind-mounted folder in place of the `localai-models` volume, and LiteLLM reaches the process
at `host.docker.internal`.

This panel runs in a container, so it cannot ask the Mac itself: code of the core called from
here sees Linux, and there is no `launchctl` and no host binary to look at. Two things are
read instead, and both are the core's: the stored runtime, from the `.env`, and the doctor
check `localai_native`, which probes the process over HTTP (at `host.docker.internal` from a
container). The check is `pass` or `warn`, never `fail`: a LocalAI that is not running is a
normal state on a Mac, so its result is shown on the Services and Host pages but is no
verdict on the host.

Starting and stopping the process is not done from here; that needs a way onto the host.

Nothing here imports the manager's settings: `app.core.images` doubles as the upgrade
runner's entry point and reads the runtime too.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.envfile import load_env_file

RUNTIME_KEY = "LOCALAI_RUNTIME"
CONTAINER = "container"
NATIVE = "native"

# The Compose service that has no container with the native runtime, as the core's
# `status.native_skip` says.
NATIVE_SKIPPED_SERVICES: frozenset[str] = frozenset({"localai"})

# The volumes the native runtime does not use: the process keeps its data in its home on the
# Mac, and the models are a bind mount there.
NATIVE_UNUSED_VOLUMES: frozenset[str] = frozenset({"localai-data", "localai-models"})

# The doctor check that probes the native process.
CHECK_NAME = "localai_native"


def runtime(env: Mapping[str, str]) -> str:
    """The runtime an already parsed root `.env` names. Anything but `native` is the
    container, as the core reads it."""
    return NATIVE if env.get(RUNTIME_KEY, "").strip() == NATIVE else CONTAINER


def stored_runtime(config_dir: str) -> str:
    """The runtime the core stored in `$PAPAIA_CONFIG_DIR/.env`, `container` when none."""
    return runtime(load_env_file(Path(config_dir) / ".env"))


def is_native(config_dir: str) -> bool:
    return stored_runtime(config_dir) == NATIVE


def skipped_services(config_dir: str) -> frozenset[str]:
    """The core services that have no container in this deployment."""
    return NATIVE_SKIPPED_SERVICES if is_native(config_dir) else frozenset()


@dataclass(frozen=True)
class NativeCheck:
    """The doctor check `localai_native`, as far as this panel uses it.

    `ready` is whether `/readyz` answered; None when the core did not say (an older shape,
    or a check that stopped before probing, such as a missing workspace). `summary` is the
    core's own sentence and is shown as it is: it names the script to start the process
    with, and later cores add what launchd knows."""

    status: str
    summary: str
    ready: bool | None = None
    url: str = ""
    installed: str = ""
    pinned: str = ""

    @property
    def running(self) -> bool:
        """Whether LocalAI answers. A `pass` always does; a `warn` only when the core says
        so (an outdated binary that still runs)."""
        return self.ready is True or (self.ready is None and self.status == "pass")


def parse_check(entry: Any) -> NativeCheck | None:
    """The check out of one entry of `doctor --json`'s `checks`, or None when it is not the
    check, or says nothing about a native LocalAI (`skip`: the container runtime, or LocalAI
    switched off)."""
    if not isinstance(entry, dict) or entry.get("name") != CHECK_NAME:
        return None
    status = entry.get("status")
    if status not in ("pass", "warn", "fail"):
        return None
    raw = entry.get("details")
    details = raw if isinstance(raw, dict) else {}
    ready = details.get("ready")
    summary = entry.get("summary")

    def text(key: str) -> str:
        value = details.get(key)
        return value if isinstance(value, str) else ""

    return NativeCheck(
        status=str(status),
        summary=summary.strip() if isinstance(summary, str) else "",
        ready=ready if isinstance(ready, bool) else None,
        url=text("url"),
        installed=text("installed"),
        pinned=text("pinned"),
    )


def find_check(checks: Any) -> NativeCheck | None:
    """The check out of `doctor --json`'s `checks` list, or None when it has none to show."""
    if not isinstance(checks, list):
        return None
    return next((found for found in map(parse_check, checks) if found is not None), None)
