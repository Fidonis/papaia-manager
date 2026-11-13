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
On a distributed installation only `keep` is offered: the core refuses `stop --volumes` there,
and `module-purge` would only see the controller's volumes.

LocalAI can also run as a process on an Apple Silicon Mac (`LOCALAI_RUNTIME=native`, see
`app.core.localai_runtime`). This panel cannot reach that process, and the core asks no
variant and no URL for it, so the row is read-only then: it names the command that changes
the runtime on the Mac, and a switch either way is refused. The runtime is not a variant (the
variants mirror the core's image table, see `tests/test_config_contract.py`).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.core import ctl, localai_runtime, nodes
from app.core.ctl import (
    LOCALAI_VARIANTS,
    MODULE_URL_FLAGS,
    PURGE_MODULES,
    CtlError,
    run_module_info,
)
from app.core.envfile import load_env_file
from app.core.inventory import core_inventory, profiles_in
from app.core.mail.settings import load_mail_settings, settings_json
from app.core.state import load_deployment_yaml
from app.i18n import N_, gettext

KEY_VARIANT = "LOCALAI_IMAGE_VARIANT"
DEFAULT_VARIANT = "cpu"

VARIANT_LABELS: dict[str, str] = {
    "cpu": N_("CPU (works everywhere)"),
    "nvidia-cuda-12": N_("NVIDIA CUDA 12"),
    "nvidia-cuda-13": N_("NVIDIA CUDA 13"),
    "intel": N_("Intel GPU"),
    "hipblas": N_("AMD ROCm"),
}

def variant_options() -> list[dict[str, str]]:
    """The LocalAI variants the page offers, labelled in the language of the request."""
    return [{"id": v, "label": gettext(VARIANT_LABELS[v])} for v in LOCALAI_VARIANTS]


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
            N_(
                "Runs language models on this host behind an OpenAI-compatible API. Sign-in goes "
                "through Keycloak."
            )
        ),
        switchable=True,
    ),
    ModuleDef(
        name="web-search",
        profile="librechat-websearch",
        title=N_("Web Search"),
        description=N_("Lets LibreChat search the web and read pages."),
        switchable=True,
    ),
    ModuleDef(
        name="rag",
        profile="rag",
        title=N_("RAG System"),
        description=N_("Vector database, its MCP server and the ingest service."),
        switchable=True,
        link="/connections",
        link_label=N_("Open the RAG pages"),
        link_needs_module=True,
        # LibreChat's configuration lists the RAG system's MCP server and the roles that may
        # use it; it reads the rendered file once, so it is recreated with the change.
        recreate=("librechat",),
        needs_core_modules=True,
    ),
    ModuleDef(
        name="mail",
        profile="smtp",
        title=N_("Mail Relay"),
        description=(
            N_(
                "One internal relay that holds the credentials of the real mail server. The "
                "server is set up on the Mail page, with the relay on or off."
            )
        ),
        switchable=True,
        link="/mail/server",
        link_label=N_("Mail server settings"),
        needs_core_modules=True,
    ),
    ModuleDef(
        name="manager",
        profile="manager",
        title="papaia-manager",
        description=N_("This panel."),
        note=N_("Can't switch itself off. Use the CLI on the host."),
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


def stored_runtime(config_dir: str) -> str:
    """Where LocalAI runs as the core stored it: `container` (the default) or `native`."""
    return localai_runtime.stored_runtime(config_dir)


def revision(config_dir: str) -> str:
    """A fingerprint of what the tab shows: the profile set, the LocalAI variant and the
    LocalAI runtime.

    The form carries it back, so a switch made from an older page (or on the host
    meanwhile) is refused instead of overwriting the newer state."""
    env = load_env_file(Path(config_dir) / ".env")
    profiles = ",".join(sorted(profiles_in(env)))
    material = f"{profiles}\n{env.get(KEY_VARIANT, '')}"
    # Appended only for `native`, so the revision of a container installation stays what it
    # was and a page loaded before an upgrade of the manager is not called stale.
    if localai_runtime.runtime(env) == localai_runtime.NATIVE:
        material += f"\n{localai_runtime.NATIVE}"
    return hashlib.sha256(material.encode()).hexdigest()[:16]


def service_count(
    workspace_dir: str, profile: str, skip: frozenset[str] = frozenset()
) -> tuple[int, int]:
    """(services, modules) the profile brings up, read from the shipped Compose fragments.
    `skip` names services that run no container here (see `inventory.core_inventory`)."""
    items = [i for i in core_inventory(workspace_dir, {profile}, skip) if profile in i.profiles]
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


def mail_defaults(config_dir: str) -> dict[str, object]:
    """What the mail dialog starts from: whatever the core has stored of the mail server, with
    only whether a password is stored (never the password)."""
    stored = settings_json(load_mail_settings(config_dir))
    return {
        key: stored[key]
        for key in ("host", "port", "security", "user", "login", "has_password")
        + ("from_address", "from_name")
    }


def state(config_dir: str, workspace_dir: str) -> list[dict[str, object]]:
    """What the tab renders: one row per module, in the order of `MODULES`."""
    on = active(config_dir)
    variant = stored_variant(config_dir)
    native = stored_runtime(config_dir) == localai_runtime.NATIVE
    manages = core_manages_modules(workspace_dir)
    distributed = nodes.is_distributed(config_dir)
    rows: list[dict[str, object]] = []
    for module in MODULES:
        services, modules = service_count(workspace_dir, module.profile)
        enabled = module.profile in on
        if native and module.name == "localai":
            containers, _ = service_count(
                workspace_dir, module.profile, localai_runtime.NATIVE_SKIPPED_SERVICES
            )
            rows.append(_native_row(module, enabled=enabled, services=containers))
            continue
        # A profile the core's Compose files don't have (a core older than the module): the
        # switch could only fail. The manager's own profile is always there.
        shipped = services > 0 or not module.switchable
        supported = shipped and (manages or not module.needs_core_modules)
        problem = (
            gettext("This papAIa core is too old to switch it from here.")
            if module.switchable and not supported
            else ""
        )
        rows.append(
            {
                "name": module.name,
                "profile": module.profile,
                "title": gettext(module.title),
                "description": gettext(module.description),
                "enabled": enabled,
                "switchable": module.switchable and (supported or enabled),
                "supported": supported,
                "problem": problem,
                "link": module.link if (enabled or not module.link_needs_module) else "",
                "link_label": gettext(module.link_label) if module.link_label else "",
                "note": gettext(module.note) if module.note else "",
                "services": services,
                "modules": modules,
                # The node the module's services are placed on; empty on a single host.
                "node": nodes.profile_node(config_dir, module.profile),
                "variant": variant if module.name == "localai" else None,
                "runtime": localai_runtime.CONTAINER if module.name == "localai" else None,
                "configured": mail_configured(config_dir) if module.name == "mail" else None,
                "mail_defaults": mail_defaults(config_dir) if module.name == "mail" else None,
                "can_delete": manages and module.name in PURGE_MODULES,
                # Set when switching off can only keep everything (see the module docstring).
                "keep_only": (
                    gettext(DISTRIBUTED_KEEP_ONLY)
                    if distributed and module.name in PURGE_MODULES
                    else ""
                ),
            }
        )
    return rows


# The note of the read-only LocalAI row, and the refusal of a switch, while it runs natively.
NATIVE_NOTE = N_(
    "LocalAI runs natively on the Mac, outside Docker, and is switched and started there: run "
    "`papaia-ctl setup` on the Mac (`--localai-runtime=container` moves it back into its "
    "container)."
)


def _native_row(module: ModuleDef, *, enabled: bool, services: int) -> dict[str, object]:
    """The LocalAI row while it runs natively: shown, never switched from here. `services`
    counts the containers, which is the model download job alone."""
    return {
        "name": module.name,
        "profile": module.profile,
        "title": gettext(module.title),
        "description": gettext(module.description),
        "enabled": enabled,
        "switchable": False,
        "supported": True,
        "problem": "",
        "link": "",
        "link_label": "",
        "note": gettext(NATIVE_NOTE),
        "services": services,
        "modules": 1 if services else 0,
        "node": "",
        "variant": None,
        "runtime": localai_runtime.NATIVE,
        "configured": None,
        "mail_defaults": None,
        "can_delete": False,
        "keep_only": "",
    }


def refuse_native(name: str, runtime: str) -> None:
    """Raises ModuleError for a switch of LocalAI while it runs natively."""
    if name == "localai" and runtime == localai_runtime.NATIVE:
        raise ModuleError(gettext(NATIVE_NOTE))


def validate_enable(
    name: str, variant: str | None, *, runtime: str = localai_runtime.CONTAINER
) -> str | None:
    """The variant to store for an enable request, or raises ModuleError.

    Only LocalAI has one; it is required and must be one of the variants this page offers.
    `runtime` is the stored LocalAI runtime (`stored_runtime`): a native LocalAI is not
    switched from here."""
    module = SWITCHABLE.get(name)
    if module is None:
        raise ModuleError(gettext("This module can't be switched from here."))
    refuse_native(name, runtime)
    if name != "localai":
        if variant is not None:
            raise ModuleError(gettext("Only LocalAI takes an accelerator variant."))
        return None
    if variant is None:
        raise ModuleError(gettext("Choose the accelerator variant for LocalAI."))
    if variant in HOST_ONLY_VARIANTS:
        raise ModuleError(
            gettext(
                "The %(variant)s variant is set on the host: run `papaia-ctl setup "
                "--localai-variant=%(variant)s`."
            ) % {"variant": variant}
        )
    if variant not in LOCALAI_VARIANTS:
        raise ModuleError(gettext("Unknown accelerator variant."))
    return variant


def validate_public_url(name: str, value: str | None) -> str | None:
    """The answer to the public-URL question of `name`, or None when none was given.

    Raises ModuleError for a module `setup` asks no URL for, or a URL it should not be given."""
    if value is None or not value.strip():
        return None
    if name not in MODULE_URL_FLAGS:
        raise ModuleError(gettext("This module takes no public URL."))
    try:
        return ctl.public_url(value)
    except ValueError as exc:
        raise ModuleError(gettext("The public URL is not valid: %(exc)s.") % {"exc": exc}) from exc


def check_enable(
    name: str, config_dir: str, workspace_dir: str, *, mail_given: bool = False
) -> None:
    """Raises ModuleError when the core would refuse to switch `name` on, or the module has
    nothing to start yet. The core checks again; this is the sentence before a job is queued.

    `mail_given` says the request carries the mail server (the dialog asks for it when none is
    stored), so the relay is not refused for lacking one."""
    module = SWITCHABLE[name]
    if module.needs_core_modules and not core_manages_modules(workspace_dir):
        raise ModuleError(gettext(
            "This papAIa core is too old to switch it from here (it needs 1.6.0)."
        ))
    services, _ = service_count(workspace_dir, module.profile)
    if services == 0:
        raise ModuleError(gettext(
            "This papAIa core has no %(title)s (the %(profile)s profile)."
        ) % {"title": gettext(module.title), "profile": module.profile})
    if name == "rag":
        clash = conflicting_addons(config_dir)
        if clash:
            raise ModuleError(
                gettext(
                    "The RAG system can't run next to the add-on %(value)s: both start the same "
                    "services and publish the same ports. Stop and remove the add-on first "
                    "(Add-ons page); its volumes are kept."
                ) % {"value": ', '.join(clash)}
            )
    if name == "mail" and not mail_given and not mail_configured(config_dir):
        raise ModuleError(
            gettext(
                "No mail server is set up yet. Enter it when switching the relay on, or on the "
                "Mail page (settings there are saved with the relay off)."
            )
        )


# Why the switch-off dialog offers no deletion on a distributed installation.
DISTRIBUTED_KEEP_ONLY = N_(
    "On a distributed installation the data and the settings stay: deleting them is not "
    "supported across nodes yet."
)


def validate_scope(
    name: str, scope: str, confirm: str | None, workspace_dir: str, *, config_dir: str
) -> str:
    """The deletion scope of a switch-off request, or raises ModuleError.

    Anything that deletes needs the module's name typed back, a core that can do it, and a
    single host. A native LocalAI is not switched off from here at all."""
    refuse_native(name, stored_runtime(config_dir))
    if scope not in SCOPES:
        raise ModuleError(gettext("Choose what to do with the data and the settings."))
    if scope == SCOPE_KEEP:
        return scope
    if nodes.is_distributed(config_dir):
        raise ModuleError(gettext(DISTRIBUTED_KEEP_ONLY))
    if not core_manages_modules(workspace_dir):
        raise ModuleError(
            gettext("This papAIa core can't delete a module's data (it needs 1.6.0).")
        )
    if (confirm or "").strip() != name:
        raise ModuleError(gettext(
            "Type the module name (%(name)s) to confirm the deletion."
        ) % {"name": name})
    return scope


async def module_info(name: str, *, workspace_dir: str, config_dir: str) -> dict[str, Any]:
    """What switching `name` off could delete, as the core reports it.

    A core without the command (or one that fails) gives an `available: False` document: the
    dialog then offers the plain switch-off, and nothing is deleted."""
    if name not in SWITCHABLE:
        raise ModuleError(gettext("This module can't be switched from here."))
    if not core_manages_modules(workspace_dir):
        return {"available": False, "reason": gettext(
            "This papAIa core can't delete a module's data."
        )}
    try:
        code, out, err = await run_module_info(
            name, workspace_dir=workspace_dir, config_dir=config_dir
        )
    except (CtlError, OSError) as exc:
        return {"available": False, "reason": gettext(
            "The core did not answer: %(exc)s"
        ) % {"exc": exc}}
    if code != 0:
        line = next((ln.strip() for ln in (err or out).splitlines() if ln.strip()), "")
        return {"available": False, "reason": line.removeprefix("ERROR:").strip()[:300]}
    try:
        document = json.loads(out)
    except ValueError:
        return {"available": False, "reason": gettext("The core's answer could not be read.")}
    if not isinstance(document, dict):
        return {"available": False, "reason": gettext("The core's answer could not be read.")}
    return {"available": True, **document}
