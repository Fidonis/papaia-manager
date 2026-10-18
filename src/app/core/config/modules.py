"""The optional core modules: which are on, which the Modules tab may switch.

The core owns the answer. `COMPOSE_PROFILES` in `$PAPAIA_CONFIG_DIR/.env` is what Compose
acts on and what `papaia-ctl setup --enable-...` edits (`deployment.yaml` only mirrors it),
so the state is read from there and a change is asked of `setup`
(`app.core.ctl.run_module_setup`, `run_smtp_setup`). The manager does not write the file.

Four modules are switched here, and this tab is the only place that switches them: LocalAI,
web search, the RAG system and the mail relay. The RAG and Mail pages hold their settings and
work with the module off as well. The manager runs this page and can't switch itself off.

Switching a module off can also delete what it leaves behind. The core knows what that is
(`module-info`): the named volumes the module owns and the settings it keeps. Three results
are offered (`SCOPES`), and no fourth: settings can't be deleted and the data kept for the
RAG system (its stored credentials are encrypted with a secret that belongs to the data), and
a module whose settings were deleted but whose data stayed would not be one anybody can use.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core.ctl import LOCALAI_VARIANTS, PURGE_MODULES, CtlError, run_module_info
from app.core.envfile import load_env_file
from app.core.inventory import core_inventory, profiles_in
from app.core.mail.settings import load_mail_settings
from app.core.state import load_deployment_yaml

KEY_VARIANT = "LOCALAI_IMAGE_VARIANT"
DEFAULT_VARIANT = "cpu"

VARIANT_LABELS: dict[str, str] = {
    "cpu": "CPU (works everywhere)",
    "nvidia-cuda-12": "NVIDIA CUDA 12",
    "nvidia-cuda-13": "NVIDIA CUDA 13",
    "intel": "Intel GPU",
    "hipblas": "AMD ROCm",
}

# Offered by the core, not by this page: see `app.core.ctl.LOCALAI_VARIANTS`.
HOST_ONLY_VARIANTS = ("vulkan",)

# What a switch-off may delete. `keep` removes the containers only.
SCOPE_KEEP = "keep"
SCOPE_DATA = "data"
SCOPE_ALL = "all"
SCOPES = (SCOPE_KEEP, SCOPE_DATA, SCOPE_ALL)

# Add-ons whose services clash with the RAG system's (the core refuses to switch it on while
# one of them is active; asking first gives the operator the sentence before a job is queued).
RAG_CONFLICTING_ADDONS = ("qdrant", "qdrant-connect", "qdrant-ingest")

# The core file that ships the module operations (`module-info`, `module-purge`, `stop
# --volumes` and mail settings that are kept while the relay is off). Asked as the existence
# of the file, like `host_health.is_supported` asks for `doctor`, not as a version.
_CORE_MODULES_FILE = ("tools", "lib", "modules.py")


@dataclass(frozen=True)
class ModuleDef:
    """One optional module the page lists."""

    name: str
    profile: str
    title: str
    description: str
    # Switchable here, or only shown.
    switchable: bool = False
    # A page of its own for the module's settings (and what it says about it).
    link: str = ""
    link_label: str = "Open"
    # Whether the link only works while the module is on (the RAG pages are not served
    # without the RAG system).
    link_needs_module: bool = False
    note: str = ""
    # Other profiles whose containers read this module's rendered configuration: they are
    # recreated after the module is switched on or off.
    recreate: tuple[str, ...] = ()
    # The module needs the core to ship the module operations, not only its own profile.
    needs_core_modules: bool = False


MODULES: tuple[ModuleDef, ...] = (
    ModuleDef(
        name="localai",
        profile="localai",
        title="LocalAI",
        description=(
            "Runs language models on this host behind an OpenAI-compatible API. "
            "Sign-in goes through Keycloak."
        ),
        switchable=True,
    ),
    ModuleDef(
        name="web-search",
        profile="librechat-websearch",
        title="Web search",
        description="Lets LibreChat search the web and read pages.",
        switchable=True,
    ),
    ModuleDef(
        name="rag",
        profile="rag",
        title="RAG system",
        description="Vector database, its MCP server and the ingest service.",
        switchable=True,
        link="/connections",
        link_label="Open the RAG pages",
        link_needs_module=True,
        # LibreChat's configuration lists the RAG system's MCP server and the roles that may
        # use it; it reads the rendered file once, so it is recreated with the change.
        recreate=("librechat",),
        needs_core_modules=True,
    ),
    ModuleDef(
        name="mail",
        profile="smtp",
        title="Mail relay",
        description=(
            "One internal relay that holds the credentials of the real mail server. "
            "The server is set up on the Mail page, with the relay on or off."
        ),
        switchable=True,
        link="/mail/server",
        link_label="Mail server settings",
        needs_core_modules=True,
    ),
    ModuleDef(
        name="manager",
        profile="manager",
        title="papaia-manager",
        description="This panel.",
        note="Can't switch itself off. Use the CLI on the host.",
    ),
)

SWITCHABLE: dict[str, ModuleDef] = {m.name: m for m in MODULES if m.switchable}

# Every module the core can purge is switchable here, and the other way round.
assert set(SWITCHABLE) == set(PURGE_MODULES)  # noqa: S101 - a constant, checked at import


class ModuleError(Exception):
    """A request the page cannot act on. The message is shown as it is."""


def active(config_dir: str) -> set[str]:
    """The Compose profiles that are switched on."""
    return profiles_in(load_env_file(Path(config_dir) / ".env"))


def stored_variant(config_dir: str) -> str:
    """The LocalAI image variant the core stored, `cpu` when none."""
    value = load_env_file(Path(config_dir) / ".env").get(KEY_VARIANT, "").strip()
    return value or DEFAULT_VARIANT


def revision(config_dir: str) -> str:
    """A fingerprint of what the tab shows: the profile set and the LocalAI variant.

    The form carries it back, so a switch made from an older page (or on the host
    meanwhile) is refused instead of overwriting the newer state."""
    env = load_env_file(Path(config_dir) / ".env")
    profiles = ",".join(sorted(profiles_in(env)))
    material = f"{profiles}\n{env.get(KEY_VARIANT, '')}"
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def service_count(workspace_dir: str, profile: str) -> tuple[int, int]:
    """(services, modules) the profile brings up, read from the shipped Compose fragments."""
    items = [i for i in core_inventory(workspace_dir, {profile}) if profile in i.profiles]
    return len(items), len({i.module for i in items})


def core_manages_modules(workspace_dir: str) -> bool:
    """Whether the core ships the module operations (delete a module's data or settings)."""
    return Path(workspace_dir).joinpath("papaia", *_CORE_MODULES_FILE).is_file()


def conflicting_addons(config_dir: str) -> list[str]:
    """The active add-ons that would clash with the RAG system, by name."""
    try:
        deployment = load_deployment_yaml(config_dir)
    except (OSError, ValueError):
        return []
    found = {
        str(entry.get("name"))
        for entry in (deployment.get("addons") or [])
        if isinstance(entry, dict) and entry.get("active")
    }
    return sorted(found & set(RAG_CONFLICTING_ADDONS))


def mail_configured(config_dir: str) -> bool:
    """Whether the core holds a mail server and a sender address to switch the relay on with."""
    return load_mail_settings(config_dir).configured


def state(config_dir: str, workspace_dir: str) -> list[dict[str, object]]:
    """What the tab renders: one row per module, in the order of `MODULES`."""
    on = active(config_dir)
    variant = stored_variant(config_dir)
    manages = core_manages_modules(workspace_dir)
    rows: list[dict[str, object]] = []
    for module in MODULES:
        services, modules = service_count(workspace_dir, module.profile)
        enabled = module.profile in on
        # A profile the core's Compose files don't have (a core older than the module): the
        # switch could only fail. The manager's own profile is always there.
        shipped = services > 0 or not module.switchable
        supported = shipped and (manages or not module.needs_core_modules)
        problem = (
            "This papAIa core is too old to switch it from here."
            if module.switchable and not supported
            else ""
        )
        rows.append(
            {
                "name": module.name,
                "profile": module.profile,
                "title": module.title,
                "description": module.description,
                "enabled": enabled,
                "switchable": module.switchable and (supported or enabled),
                "supported": supported,
                "problem": problem,
                "link": module.link if (enabled or not module.link_needs_module) else "",
                "link_label": module.link_label,
                "note": module.note,
                "services": services,
                "modules": modules,
                "variant": variant if module.name == "localai" else None,
                "configured": mail_configured(config_dir) if module.name == "mail" else None,
                "can_delete": manages and module.name in PURGE_MODULES,
            }
        )
    return rows


def validate_enable(name: str, variant: str | None) -> str | None:
    """The variant to store for an enable request, or raises ModuleError.

    Only LocalAI has one; it is required and must be one of the variants this page offers."""
    module = SWITCHABLE.get(name)
    if module is None:
        raise ModuleError("This module can't be switched from here.")
    if name != "localai":
        if variant is not None:
            raise ModuleError("Only LocalAI takes an accelerator variant.")
        return None
    if variant is None:
        raise ModuleError("Choose the accelerator variant for LocalAI.")
    if variant in HOST_ONLY_VARIANTS:
        raise ModuleError(
            f"The {variant} variant is set on the host: run "
            f"`papaia-ctl setup --localai-variant={variant}`."
        )
    if variant not in LOCALAI_VARIANTS:
        raise ModuleError("Unknown accelerator variant.")
    return variant


def check_enable(name: str, config_dir: str, workspace_dir: str) -> None:
    """Raises ModuleError when the core would refuse to switch `name` on, or the module has
    nothing to start yet. The core checks again; this is the sentence before a job is queued."""
    module = SWITCHABLE[name]
    if module.needs_core_modules and not core_manages_modules(workspace_dir):
        raise ModuleError("This papAIa core is too old to switch it from here (it needs 1.6.0).")
    services, _ = service_count(workspace_dir, module.profile)
    if services == 0:
        raise ModuleError(f"This papAIa core has no {module.title} (the {module.profile} profile).")
    if name == "rag":
        clash = conflicting_addons(config_dir)
        if clash:
            raise ModuleError(
                f"The RAG system can't run next to the add-on {', '.join(clash)}: both start "
                "the same services and publish the same ports. Stop and remove the add-on "
                "first (Add-ons page); its volumes are kept."
            )
    if name == "mail" and not mail_configured(config_dir):
        raise ModuleError(
            "No mail server is set up yet. Enter it on the Mail page (Settings there are "
            "saved with the relay off), then switch the relay on."
        )


def validate_scope(name: str, scope: str, confirm: str | None, workspace_dir: str) -> str:
    """The deletion scope of a switch-off request, or raises ModuleError.

    Anything that deletes needs the module's name typed back, and a core that can do it."""
    if scope not in SCOPES:
        raise ModuleError("Choose what to do with the data and the settings.")
    if scope == SCOPE_KEEP:
        return scope
    if not core_manages_modules(workspace_dir):
        raise ModuleError("This papAIa core can't delete a module's data (it needs 1.6.0).")
    if (confirm or "").strip() != name:
        raise ModuleError(f"Type the module name ({name}) to confirm the deletion.")
    return scope


async def module_info(name: str, *, workspace_dir: str, config_dir: str) -> dict[str, Any]:
    """What switching `name` off could delete, as the core reports it.

    A core without the command (or one that fails) gives an `available: False` document: the
    dialog then offers the plain switch-off, and nothing is deleted."""
    if name not in SWITCHABLE:
        raise ModuleError("This module can't be switched from here.")
    if not core_manages_modules(workspace_dir):
        return {"available": False, "reason": "This papAIa core can't delete a module's data."}
    try:
        code, out, err = await run_module_info(
            name, workspace_dir=workspace_dir, config_dir=config_dir
        )
    except (CtlError, OSError) as exc:
        return {"available": False, "reason": f"The core did not answer: {exc}"}
    if code != 0:
        line = next((ln.strip() for ln in (err or out).splitlines() if ln.strip()), "")
        return {"available": False, "reason": line.removeprefix("ERROR:").strip()[:300]}
    try:
        document = json.loads(out)
    except ValueError:
        return {"available": False, "reason": "The core's answer could not be read."}
    if not isinstance(document, dict):
        return {"available": False, "reason": "The core's answer could not be read."}
    return {"available": True, **document}
