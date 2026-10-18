"""The optional core modules: which are on, which the Modules tab may switch.

The core owns the answer. `COMPOSE_PROFILES` in `$PAPAIA_CONFIG_DIR/.env` is what Compose
acts on and what `papaia-ctl setup --enable-...` edits (`deployment.yaml` only mirrors it),
so the state is read from there and a change is asked of `setup`
(`app.core.ctl.run_module_setup`). The manager does not write the file.

Two modules are switchable here. The others are listed so the tab shows the whole picture:
the RAG system and the mail relay have pages of their own (and their own setup options), and
the manager runs this page.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from app.core.ctl import LOCALAI_VARIANTS
from app.core.envfile import load_env_file
from app.core.inventory import core_inventory, profiles_in

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


@dataclass(frozen=True)
class ModuleDef:
    """One optional module the page lists."""

    name: str
    profile: str
    title: str
    description: str
    # Switchable here, or only shown. `link` says where an unswitchable one is managed.
    switchable: bool = False
    link: str = ""
    note: str = ""


MODULES: tuple[ModuleDef, ...] = (
    ModuleDef(
        name="localai",
        profile="localai",
        title="LocalAI",
        description="Run language models on this host. Adds LocalAI to LiteLLM.",
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
        link="/connections",
        note="Managed on the RAG pages.",
    ),
    ModuleDef(
        name="mail",
        profile="smtp",
        title="Mail relay",
        description="One internal relay that holds the credentials of the real mail server.",
        link="/mail/server",
        note="Managed on the Mail page.",
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


def state(config_dir: str, workspace_dir: str) -> list[dict[str, object]]:
    """What the tab renders: one row per module, in the order of `MODULES`."""
    on = active(config_dir)
    variant = stored_variant(config_dir)
    rows: list[dict[str, object]] = []
    for module in MODULES:
        services, modules = service_count(workspace_dir, module.profile)
        rows.append(
            {
                "name": module.name,
                "profile": module.profile,
                "title": module.title,
                "description": module.description,
                "enabled": module.profile in on,
                "switchable": module.switchable,
                "link": module.link,
                "note": module.note,
                "services": services,
                "modules": modules,
                "variant": variant if module.name == "localai" else None,
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
