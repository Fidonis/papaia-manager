"""What a restore point offers to restore, in product terms.

`backups.py` reads the catalogue and the manifest. This module turns one
manifest into something an operator can choose from, and works out what a
choice would cost.

Three decisions are load-bearing:

* **The module is the unit, not the volume.** A manifest written by a core that
  supports selection carries a `module` per artifact, resolved there from the
  Compose files. Volume names do not predict their owners -- `litellm-postgresql`
  is mounted by `litellm-db` -- so the grouping has to come from the manifest and
  is simply absent for a snapshot written before it existed.

* **The thirteen core volumes are not equals.** Two are logs and metrics, one is
  a search cache, one is tens of gigabytes of re-downloadable model weights, and
  one reads like configuration while the file anyone cares about is mounted over
  it from the config directory. Offering them as thirteen identical checkboxes
  would be an honest rendering of the manifest and a bad thing to put in front of
  someone recovering from an incident, so the presentation table below says which
  are ordinary, which are advanced, and which are not offered at all.

* **Keycloak is coupled to the configuration.** `bake_realm_secrets` writes the
  client secrets of every service into the realm JSON, and Keycloak imports the
  realm only on first start. Restoring its database alone leaves the realm
  holding the snapshot's secrets while the config directory keeps the current
  ones, so every OIDC client breaks at once -- this panel included, with no
  self-service way back. It is therefore offered only together with the
  configuration archive, which pushes the operation onto the whole-stack path.

Everything here is pure: a manifest dict in, plain data out. No Docker, no
subprocess, no filesystem beyond what the caller already read.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# Selector grammar, mirroring tools/lib/backup.py. Kept as a literal rather than
# imported: nothing under app/ imports lib.* (see backups.py), and a selector
# reaching an argv is exactly the place to spell the rule out locally.
SELECTOR_KINDS = ("module", "volume", "addon")
MAX_SELECTORS = 32

# The profile papaia-manager runs under. Never selectable.
SELF_PROFILE = "manager"

# The selector that pulls in the configuration archive. It is not a module and
# has no artifact of its own to match on -- it is how the UI says "this becomes
# a whole-stack restore".
CONFIG_SELECTOR = "config"


@dataclass(frozen=True)
class _Presentation:
    label: str
    summary: str
    # Behind the expert disclosure rather than offered up front.
    advanced: bool = False
    # Cannot be restored without the configuration archive.
    requires_config: bool = False
    hazards: tuple[str, ...] = ()


_MODULES: dict[str, _Presentation] = {
    "keycloak": _Presentation(
        label="Keycloak",
        summary="Logins, users and realm data",
        requires_config=True,
        hazards=(
            "The realm carries every service's client secret and is imported only on "
            "first start. Restoring the database on its own would leave those secrets "
            "out of step with the live configuration and no sign-in would work, this "
            "panel included, so the stack configuration is restored with it.",
            "Every active session ends. You will have to sign in again.",
        ),
    ),
    "librechat": _Presentation(
        label="LibreChat",
        summary="Conversations, uploads, search index and embeddings",
        hazards=(
            "Conversations, the search index, the embeddings and the uploaded files "
            "reference each other by id. Restoring the whole module keeps them "
            "consistent; picking single volumes below can leave hits on deleted "
            "conversations, empty retrieval results, or file records with no file.",
        ),
    ),
    "litellm": _Presentation(
        label="LiteLLM",
        summary="Model routing, virtual keys and spend",
        hazards=(
            "Every virtual key issued since the snapshot stops working. Their holders "
            "get 401 until the keys are reissued.",
        ),
    ),
    "localai": _Presentation(
        label="LocalAI",
        summary="Local accounts and downloaded models",
        hazards=(
            "Restoring the local account store can bring back accounts that were "
            "deleted since the snapshot.",
        ),
    ),
    "searxng": _Presentation(
        label="SearXNG",
        summary="Search settings and cache",
        advanced=True,
        hazards=(
            "The settings file itself is mounted over this volume from the "
            "configuration directory, so restoring it changes less than it looks like.",
        ),
    ),
}

# Per-volume labels, keyed by the compose volume key -- that is, the manifest
# target with the project prefix removed.
_VOLUMES: dict[str, _Presentation] = {
    "keycloak-postgresql": _Presentation("Users and realm", ""),
    "librechat-mongodb": _Presentation("Conversations", ""),
    "librechat-meilisearch": _Presentation("Search index", ""),
    "librechat-vectordb": _Presentation("Embeddings", ""),
    "librechat-uploads": _Presentation("Uploaded files", ""),
    "librechat-images": _Presentation("Generated images", ""),
    "litellm-postgresql": _Presentation("Keys, budgets and spend", ""),
    "localai-data": _Presentation("Local accounts and generated data", ""),
    "localai-models": _Presentation("Downloaded model weights", "", advanced=True),
    "searxng_config": _Presentation("Settings volume", "", advanced=True),
}

# Not offered individually: logs, scrape data and a search cache. They are still
# restored when their module is, which is harmless; what they must not do is sit
# in a picker looking like something worth choosing during an incident.
_HIDDEN_VOLUMES = frozenset({"librechat-logs", "litellm-prometheus", "searxng_data"})

# Stated rather than left as a gap. The reverse proxy declares no named volume
# at all -- its database and certificates are bind mounts under the config
# directory -- so an operator looking for it must be told where it went instead
# of concluding there is nothing to restore.
NOTES: tuple[str, ...] = (
    "The reverse proxy, its certificates and every rendered service config live in "
    "the stack configuration, not in a volume. They come back only with a full restore.",
)


@dataclass(frozen=True)
class ScopeItem:
    """One individually selectable artifact inside a group."""

    selector: str
    label: str
    kind: str
    target: str
    advanced: bool = False


@dataclass
class ScopeGroup:
    """One thing an operator can choose to restore.

    Mutable, unlike ScopeItem: a group is accumulated across the artifacts that
    belong to it, so its profile set and archive count only settle once the
    manifest has been walked.
    """

    selector: str
    kind: str
    name: str
    label: str
    summary: str
    profiles: list[str] = field(default_factory=list)
    hazards: tuple[str, ...] = ()
    advanced: bool = False
    requires_config: bool = False
    items: list[ScopeItem] = field(default_factory=list)
    archives: int = 0


def _volume_key(target: str, project: str) -> str:
    """The compose volume key behind a decorated volume name.

    The project is the artifact's own, not the core's: an add-on's volumes are
    prefixed with its directory basename, so using the core project would leave
    `paperless-dir_paperless-data` intact and read back as a label.
    """
    prefix = f"{project}_"
    return target[len(prefix):] if project and target.startswith(prefix) else target


def _title(name: str) -> str:
    return name.replace("-", " ").replace("_", " ").strip().capitalize() or name


def build_groups(manifest: dict[str, Any] | None) -> list[ScopeGroup]:
    """The selectable groups of one restore point, ordered core first.

    Empty when the manifest predates selection: without a module on the
    artifacts there is no honest grouping to offer, and reconstructing one from
    volume names is exactly what the core refuses to do.
    """
    if not manifest:
        return []
    core_project = str(manifest.get("core_project") or "")
    groups: dict[str, ScopeGroup] = {}

    for raw in manifest.get("artifacts") or []:
        if not isinstance(raw, dict):
            continue
        kind = str(raw.get("kind", ""))
        module = str(raw.get("module", ""))
        owner = str(raw.get("owner", ""))
        target = str(raw.get("target", ""))
        if kind == "configdir" or not module:
            continue

        is_addon = owner.startswith("addon:")
        selector = f"{'addon' if is_addon else 'module'}:{module}"
        if selector not in groups:
            known = None if is_addon else _MODULES.get(module)
            groups[selector] = ScopeGroup(
                selector=selector,
                kind="addon" if is_addon else "module",
                name=module,
                label=known.label if known else _title(module),
                summary=known.summary if known else "Application data and documents",
                hazards=known.hazards if known else (),
                advanced=known.advanced if known else False,
                requires_config=known.requires_config if known else False,
            )
        group = groups[selector]

        for profile in raw.get("profiles") or []:
            if profile and str(profile) not in group.profiles:
                group.profiles.append(str(profile))
        group.archives += 1

        if kind != "volume":
            continue
        key = _volume_key(target, str(raw.get("project") or core_project))
        if key in _HIDDEN_VOLUMES:
            continue
        known_volume = _VOLUMES.get(key)
        group.items.append(
            ScopeItem(
                selector=f"volume:{target}",
                label=known_volume.label if known_volume else _title(key),
                kind=kind,
                target=target,
                advanced=known_volume.advanced if known_volume else False,
            )
        )

    # Core before add-ons, ordinary before advanced, then alphabetical -- the
    # order the picker renders in, decided once here rather than in the template.
    return sorted(
        groups.values(), key=lambda g: (g.kind != "module", g.advanced, g.label.lower())
    )


def allowed_selectors(groups: list[ScopeGroup]) -> set[str]:
    """Every selector this restore point may be asked for.

    The allowlist is derived from the snapshot rather than from a pattern, the
    same way `ctl.profiles_flag` checks profile names against what the shipped
    Compose files declare. A well-formed selector naming something this snapshot
    does not contain is a 400, not a subprocess that fails later.
    """
    allowed = {CONFIG_SELECTOR}
    for group in groups:
        allowed.add(group.selector)
        allowed.update(item.selector for item in group.items)
    return allowed


def requires_full_restore(groups: list[ScopeGroup], selected: list[str]) -> bool:
    """True when the selection can only be carried out as a whole-stack restore.

    Either the configuration was chosen outright, or something that cannot be
    restored without it was.
    """
    if CONFIG_SELECTOR in selected:
        return True
    chosen = set(selected)
    return any(
        group.requires_config
        and (group.selector in chosen or any(i.selector in chosen for i in group.items))
        for group in groups
    )


def selected_profiles(groups: list[ScopeGroup], selected: list[str]) -> list[str]:
    """Core profiles a selection resolves to.

    A profile is the unit a scoped teardown acts on, so this -- not the module
    list -- is what the impact preview has to be built from. Selecting SearXNG
    stops `librechat-websearch`, which is nine containers across four modules.
    """
    chosen = set(selected)
    profiles: set[str] = set()
    for group in groups:
        if group.kind != "module":
            continue
        if group.selector in chosen or any(i.selector in chosen for i in group.items):
            profiles.update(group.profiles)
    return sorted(profiles)


def selected_addons(groups: list[ScopeGroup], selected: list[str]) -> list[str]:
    chosen = set(selected)
    return sorted(
        group.name
        for group in groups
        if group.kind == "addon"
        and (group.selector in chosen or any(i.selector in chosen for i in group.items))
    )


# Progress markers papaia-ctl emits on stdout, one per step:
#   RESTORE-STEP<TAB>phase<TAB>subject<TAB>state
# Parsed rather than the surrounding prose on purpose -- that text is written for
# an operator reading a terminal and is expected to be reworded, so parsing it
# would make every copy edit a breaking change.
_STEP_PREFIX = "RESTORE-STEP\t"

# Ordered as the operation runs, so the checklist reads top to bottom.
_PHASE_LABELS = {"teardown": "Stopping", "artifact": "Restoring", "restart": "Starting"}


@dataclass(frozen=True)
class RestoreStep:
    phase: str
    subject: str
    state: str

    @property
    def phase_label(self) -> str:
        return _PHASE_LABELS.get(self.phase, self.phase)

    @property
    def done(self) -> bool:
        return self.state == "ok"

    @property
    def failed(self) -> bool:
        return self.state in ("failed", "in-use")

    @property
    def running(self) -> bool:
        return self.state == "begin"


def parse_steps(log: str) -> list[RestoreStep]:
    """The per-step state a scoped restore reported, newest state per subject.

    A subject appears twice -- `begin` then an outcome -- so the later line
    replaces the earlier one in place rather than appending. That is what turns
    an append-only log into a checklist without the job model gaining a progress
    field it would then have to keep in sync.
    """
    order: list[tuple[str, str]] = []
    latest: dict[tuple[str, str], str] = {}
    for line in log.splitlines():
        if not line.startswith(_STEP_PREFIX):
            continue
        parts = line[len(_STEP_PREFIX):].split("\t")
        if len(parts) != 3:
            continue
        phase, subject, state = (p.strip() for p in parts)
        key = (phase, subject)
        if key not in latest:
            order.append(key)
        latest[key] = state
    return [RestoreStep(phase=p, subject=s, state=latest[(p, s)]) for p, s in order]


def strip_steps(log: str) -> str:
    """The log without the marker lines, which the checklist already renders."""
    return "\n".join(
        line for line in log.splitlines() if not line.startswith(_STEP_PREFIX)
    )


def group_to_dict(group: ScopeGroup) -> dict[str, Any]:
    return {
        "selector": group.selector,
        "kind": group.kind,
        "name": group.name,
        "label": group.label,
        "summary": group.summary,
        "profiles": sorted(group.profiles),
        "hazards": list(group.hazards),
        "advanced": group.advanced,
        "requires_config": group.requires_config,
        "archives": group.archives,
        "items": [
            {
                "selector": item.selector,
                "label": item.label,
                "kind": item.kind,
                "target": item.target,
                "advanced": item.advanced,
            }
            for item in group.items
        ],
    }
