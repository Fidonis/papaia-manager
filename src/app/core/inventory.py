"""The declared target state: which containers this deployment is meant to run.

`services.py` reads what Docker reports. That answers "is the stack up?" but
never "is something missing?" -- a service whose profile was never enabled and
one that was torn down look identical from `docker ps`, because in both cases
there is no container to report. This module supplies the other half: the set
of services the deployment says it should be running, read from the Compose
files themselves.

Three decisions here are load-bearing:

* Static YAML parsing, not `docker compose config`. The latter needs every
  enabled profile's env file to be rendered and forks a process per request,
  while the only fields needed here -- `profiles` and the two `de.fidonis.*`
  labels -- are literals in the shipped fragments. Nothing to interpolate.
* Profiles are read from `$PAPAIA_CONFIG_DIR/.env`, the same file `services.py`
  takes `COMPOSE_PROJECT_NAME` from. `deployment.yaml`'s `core.profiles` mirrors
  it, but the `.env` value is what `docker compose --env-file` actually acts on.
* A profile does not map onto a module. `librechat-websearch` alone brings up
  four of them, and `oauth2-proxy` is labelled `papaia-auth`. The mapping has to
  be read out of the fragments; guessing it from the profile name would quietly
  invent modules that do not exist.

Everything here is read-only and best-effort: a workspace that cannot be read
yields an empty inventory, which degrades the services page to the live-only
view it had before rather than breaking it.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.core.envfile import load_env_file

logger = logging.getLogger(__name__)

# Labels are namespaced by product; the prefix carries no information once the
# containers are grouped, so it is dropped for display.
_MODULE_PREFIX = "papaia-"

# Containers that carry no module label at all -- a core service whose label was
# dropped in a local edit, or a third-party add-on that never set one.
UNGROUPED_MODULE = "other"

# The profile papaia-manager itself runs under. It is the one group the panel
# must not offer as a target: stopping it removes the container serving the
# request, so the operation could never report its own outcome. The stack-wide
# actions reach it, but those run detached -- see `app.core.runner`.
SELF_PROFILE = "manager"


def module_display_name(label: str) -> str:
    """The `de.fidonis.module` label as it is shown and grouped by.

    Lives here rather than in `services.py` because the target state and the
    live view have to agree on it exactly -- a mismatch would render every
    module twice, once expected and once running.
    """
    return label.removeprefix(_MODULE_PREFIX) if label else UNGROUPED_MODULE


@dataclass(frozen=True)
class ExpectedService:
    """One Compose service this deployment is supposed to be running.

    `service` is the Compose service name, which is what
    `com.docker.compose.service` reports on the running container -- the key
    the live view is matched against.

    `profiles` is what the service declared, not what is active: the filtering
    against the active set happens in `core_inventory`, so anything that comes
    back from there is enabled by definition. It is kept because a profile is
    the only granularity `papaia-ctl start` and `stop` accept, which makes it
    the unit the services page can offer control over.
    """

    service: str
    module: str
    role: str
    profiles: frozenset[str] = frozenset()


def active_profiles(config_dir: str) -> set[str]:
    """Compose profiles enabled for this deployment, from the core `.env`.

    An absent or empty `COMPOSE_PROFILES` yields an empty set, and therefore an
    empty core inventory -- correct, since Compose would start nothing either.
    """
    env = load_env_file(Path(config_dir) / ".env")
    return {p.strip() for p in env.get("COMPOSE_PROFILES", "").split(",") if p.strip()}


def core_inventory(workspace_dir: str, profiles: set[str]) -> list[ExpectedService]:
    """Core services enabled by `profiles`, read from the shipped fragments.

    Follows the `include:` list of `papaia/src/docker-compose.yml` rather than
    globbing for compose files, so a fragment that is not part of the stack --
    an add-on checked out inside the workspace, a leftover from a local
    experiment -- cannot leak into the target state.
    """
    root = Path(workspace_dir) / "papaia" / "src" / "docker-compose.yml"
    document = _load_yaml(root)
    if document is None:
        return []

    expected: list[ExpectedService] = []
    for fragment in _include_paths(document, root.parent):
        for service, body in _services(_load_yaml(fragment)).items():
            declared = _profiles(body)
            # A service without `profiles` is unconditional in Compose. None of
            # the shipped fragments does this today, but reading it as "always
            # expected" is the only interpretation that matches Compose.
            if declared and not (declared & profiles):
                continue
            expected.append(_expected(service, body, fallback_module=UNGROUPED_MODULE))
    return expected


def group_modules(expected: Iterable[ExpectedService]) -> dict[str, frozenset[str]]:
    """Compose profile to the modules its services belong to.

    Pure aggregation over an inventory that has already been read, so the
    snapshot and the endpoints that validate a request can share one parse.

    The mapping is many-to-many by nature -- see the module docstring -- which
    is why the value is a set rather than a single name. Seven of the eight core
    profiles happen to cover exactly one module today; `librechat-websearch`
    covers four, and the page has to be able to say so before it stops them.
    """
    groups: dict[str, set[str]] = {}
    for item in expected:
        for profile in item.profiles:
            groups.setdefault(profile, set()).add(item.module)
    return {profile: frozenset(modules) for profile, modules in groups.items()}


def core_groups(workspace_dir: str, profiles: set[str]) -> dict[str, frozenset[str]]:
    """The service groups this deployment can be asked to act on.

    Reads the same fragments as `core_inventory` and is therefore the authority
    on which profile names a request may name: an allowlist derived from the
    shipped Compose files rather than from a pattern. A profile that is not
    active is absent here, and rightly so -- `papaia-ctl` would hand
    `docker compose` a profile whose env file setup never rendered.
    """
    return group_modules(core_inventory(workspace_dir, profiles))


def addon_inventory(addon_path: str, fallback_module: str) -> list[ExpectedService]:
    """Every service of one add-on's Compose file.

    Add-on fragments carry no `profiles` -- an installed and active add-on is
    expected in full -- so there is nothing to filter here.

    `de.fidonis.module` is not part of the add-on contract: neither `ADDON_API`
    nor the core documentation requires it. Both add-ons Fidonis ships set it,
    a third-party one out of a customer catalogue need not, hence the fallback
    to the add-on's own name. Without it such an add-on would land in the
    `other` bucket and read as a stray container rather than as itself.
    """
    document = _load_yaml(Path(addon_path) / "docker-compose.yml")
    return [
        _expected(service, body, fallback_module=fallback_module)
        for service, body in _services(document).items()
    ]


# ---------------------------------------------------------------------------
# YAML plumbing
# ---------------------------------------------------------------------------


def _expected(service: str, body: dict[str, Any], *, fallback_module: str) -> ExpectedService:
    labels = _labels(body)
    module = labels.get("de.fidonis.module")
    return ExpectedService(
        service=service,
        module=module_display_name(module) if module else fallback_module,
        role=labels.get("de.fidonis.role", ""),
        profiles=frozenset(_profiles(body)),
    )


def _load_yaml(path: Path) -> dict[str, Any] | None:
    """Parse one Compose file, or None if it cannot be read.

    Unreadable is a normal state here: the workspace is a bind mount that may
    be absent in unit tests or mid-restore, and a missing file must cost the
    caller its target state, not its page.
    """
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        logger.debug("could not read compose file %s: %s", path, exc)
        return None
    if not isinstance(document, dict):
        logger.debug("compose file %s is not a mapping", path)
        return None
    return document


def _include_paths(document: dict[str, Any], base: Path) -> list[Path]:
    """Resolve the `include:` entries of a Compose file.

    Compose accepts both the short form (a path string) and the long form
    (a mapping with `path`), and `path` may itself be a list. All three are
    handled because the core file's shape is not this module's to dictate.
    """
    paths: list[Path] = []
    for entry in document.get("include") or []:
        raw: Any = entry.get("path") if isinstance(entry, dict) else entry
        for item in raw if isinstance(raw, list) else [raw]:
            if isinstance(item, str):
                paths.append(base / item)
    return paths


def _services(document: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    services = (document or {}).get("services") or {}
    if not isinstance(services, dict):
        return {}
    return {name: body for name, body in services.items() if isinstance(body, dict)}


def _profiles(body: dict[str, Any]) -> set[str]:
    raw = body.get("profiles") or []
    return {p for p in raw if isinstance(p, str)} if isinstance(raw, list) else set()


def _labels(body: dict[str, Any]) -> dict[str, str]:
    """Container labels, from either Compose spelling.

    The mapping form is what the papAIa fragments use; the list form
    (`- key=value`) is equally valid Compose and costs two lines to support.
    """
    raw = body.get("labels")
    if isinstance(raw, dict):
        return {str(k): str(v) for k, v in raw.items()}
    if isinstance(raw, list):
        pairs = (str(item).partition("=") for item in raw)
        return {key.strip(): value.strip() for key, _, value in pairs if key.strip()}
    return {}
