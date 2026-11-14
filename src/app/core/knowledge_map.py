"""The knowledge map of the core's `knowledge-map` profile, as far as the manager shows it.

The core renders the map into `$PAPAIA_CONFIG_DIR/knowledge-map/` (`index.yaml`, one manifest
per entry under `manifests/`, the findings in `lints.yaml`) from the add-on and module
declarations, the connected MCP servers and chat agents, and three files the operator owns
under `overlay/knowledge-map/`:

* `sources.yaml` declares entries of the operator's own (own servers, places no tool
  reaches, Agent2Agent agents),
* `overrides.yaml` changes declared or inventoried entries,
* `goldset.yaml` is the set of test questions the map server evaluates against,
* `governance.yaml` documents owners, classification, personal data and model tiers (see
  `knowledge_map_governance`); it has a write permission of its own.

The manager reads the rendered map from disk (it is a file in the configuration directory, and
reading it needs no running service) and is the only writer of the three operator files from
the interface. Like the overlay editor it writes compare-and-swap: the revision is the SHA-256
of the file's bytes, checked again under a lock right before the rename. The map server reads
the rendered files itself and reloads on change, so a write followed by the core's render is
all it takes; nothing is restarted.

What the map server knows that is not on disk (routing as a role, usage, gold-set runs,
refinement suggestions) comes from its admin API, see `knowledge_map_client`.
"""

from __future__ import annotations

import contextlib
import copy
import hashlib
import os
import re
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from app.core.envfile import load_env_file
from app.core.inventory import profiles_in
from app.i18n import N_
from app.i18n import gettext as _

PROFILE = "knowledge-map"
MAP_DIR = "knowledge-map"
OPERATOR_DIR = "overlay/knowledge-map"
MODULE_ENV = "ai/knowledge-map/.env"
ADMIN_TOKEN_KEY = "KM_ADMIN_TOKEN"

MAX_BYTES = 256 * 1024
DEFAULT_MODE = 0o600
# Previous versions of an operator file, kept so that an accepted suggestion or an edit can be
# taken back by hand. Same place and rule as the overlay history.
HISTORY_DIR = "manager/knowledge-map-history"
HISTORY_KEEP = 20

KINDS = ("source", "reference", "mcp", "agent")
VIAS = ("mcp", "rag", "a2a", "none")
GOLD_KINDS = ("single", "multi", "abstention")


@dataclass(frozen=True)
class OperatorFile:
    """One file the page edits. Requests name it by key, never by path."""

    key: str
    filename: str
    label: str
    description: str
    # Whether the core has to render after a change. The gold set is read by the map server
    # directly and changes nothing on the map.
    renders: bool


OPERATOR_FILES: tuple[OperatorFile, ...] = (
    OperatorFile(
        "sources",
        "sources.yaml",
        N_("Own entries"),
        N_("Sources, references and Agent2Agent agents that no add-on declares."),
        True,
    ),
    OperatorFile(
        "overrides",
        "overrides.yaml",
        N_("Overrides"),
        N_("Changes to declared or inventoried entries: roles, delimitations, examples."),
        True,
    ),
    OperatorFile(
        "goldset",
        "goldset.yaml",
        N_("Gold set"),
        N_("Test questions with the sources that must answer them."),
        False,
    ),
    OperatorFile(
        "governance",
        "governance.yaml",
        N_("Governance"),
        N_("Owners, classification, personal data and model tiers. Documents decisions; "
           "it restricts no access."),
        True,
    ),
)
_BY_KEY = {f.key: f for f in OPERATOR_FILES}

_LOCK = threading.Lock()
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(\.[a-z0-9_-]+)*$")


class KnowledgeMapError(Exception):
    """A request the store cannot act on. The message is shown as it is."""


# ── profile and paths ───────────────────────────────────────────────────────────────────────


def active(config_dir: str) -> bool:
    """Whether the core runs the knowledge map; what the sidebar entry hangs off."""
    return PROFILE in profiles_in(load_env_file(Path(config_dir) / ".env"))


def admin_token(config_dir: str) -> str:
    """The bearer of the map server's admin API, read from the module's `.env` at request
    time like the other module secrets. Empty when setup has not generated one."""
    token = load_env_file(Path(config_dir) / MODULE_ENV).get(ADMIN_TOKEN_KEY, "").strip()
    return "" if token.startswith("GENERATE_") else token


def operator_file(key: str) -> OperatorFile:
    found = _BY_KEY.get(key)
    if found is None:
        raise KnowledgeMapError(_("Unknown knowledge-map file."))
    return found


def path_of(config_dir: str, key: str) -> Path:
    return Path(config_dir) / OPERATOR_DIR / operator_file(key).filename


def revision_of(raw: bytes | None) -> str:
    return "" if raw is None else hashlib.sha256(raw).hexdigest()


# ── the rendered map ────────────────────────────────────────────────────────────────────────


def _load(path: Path) -> Any:
    try:
        return yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, yaml.YAMLError):
        return None


def rendered(config_dir: str) -> dict[str, Any]:
    """Every rendered entry, the render's findings and when it ran. An entry that cannot be
    read is skipped: the page shows what is there, the core's render reports the rest."""
    base = Path(config_dir) / MAP_DIR
    entries: list[dict[str, Any]] = []
    manifests = base / "manifests"
    if manifests.is_dir():
        for path in sorted(manifests.glob("*.yaml")):
            data = _load(path)
            if isinstance(data, dict) and data.get("id"):
                entries.append(_entry_view(data))
    lints = _load(base / "lints.yaml")
    lints = lints if isinstance(lints, dict) else {}
    # The render keeps add-on proposals and agent models out of the manifests (they are
    # model-facing) and hands them to this view through lints.yaml.
    proposals = lints.get("proposals")
    agent_models = lints.get("agent_models")
    for entry in entries:
        proposal = proposals.get(entry["id"]) if isinstance(proposals, dict) else None
        if isinstance(proposal, dict):
            entry["governance_proposal"] = dict(proposal)
        model = agent_models.get(entry["id"]) if isinstance(agent_models, dict) else None
        if isinstance(model, dict):
            entry["model"] = dict(model)
    findings = [f for f in lints.get("findings") or [] if isinstance(f, dict)]
    warnings = [str(w) for w in lints.get("warnings") or []]
    return {
        "rendered": manifests.is_dir(),
        "generated_at": lints.get("generated_at") or _newest(entries),
        "entries": entries,
        "findings": findings,
        "warnings": warnings,
    }


def _newest(entries: list[dict[str, Any]]) -> str | None:
    stamps = [str(e["generated_at"]) for e in entries if e.get("generated_at")]
    return max(stamps) if stamps else None


def _access(data: dict[str, Any]) -> list[dict[str, Any]]:
    """The access paths of an entry in the list form, whichever form it was declared in."""
    raw = data.get("access")
    if isinstance(raw, list):
        return [p for p in raw if isinstance(p, dict)]
    legacy = raw if isinstance(raw, dict) else data
    server = legacy.get("mcp_server")
    if server:
        return [{"via": "mcp", "target": server, "key_tools": legacy.get("key_tools") or []}]
    if data.get("location"):
        return [{"via": "none", "location": data["location"]}]
    return []


def _mapping_of(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _entry_view(data: dict[str, Any]) -> dict[str, Any]:
    raw_provenance = data.get("provenance")
    provenance: dict[str, Any] = raw_provenance if isinstance(raw_provenance, dict) else {}
    return {
        "id": str(data["id"]),
        "kind": data.get("kind") or "source",
        "system": data.get("system") or data["id"],
        "coverage": data.get("coverage") or "",
        "topics": [
            {"id": t.get("id"), "label": t.get("label") or t.get("id")}
            for t in data.get("topics") or []
            if isinstance(t, dict)
        ],
        "not_covered": [n for n in data.get("not_covered") or [] if isinstance(n, dict)],
        "example_queries": [str(q) for q in data.get("example_queries") or []],
        "required_roles": [str(r) for r in data.get("required_roles") or []],
        "on_denied": data.get("on_denied") or "hidden",
        "source_of_truth_for": [str(t) for t in data.get("source_of_truth_for") or []],
        "access": _access(data),
        "uses": [str(u) for u in data.get("uses") or []],
        "delegate_when": data.get("delegate_when") or "",
        "undeclared": bool(data.get("undeclared")),
        "location": data.get("location") or "",
        "owner": data.get("owner") or "",
        "declared_by": provenance.get("declared_by") or provenance.get("addon") or "",
        # The effective handling label the render writes into a manifest; the add-on's
        # proposal and an agent's model come from lints.yaml (see `rendered`).
        "handling": _mapping_of(data.get("handling")),
        "governance_proposal": _mapping_of(data.get("governance_proposal")),
        "model": _mapping_of(data.get("model")),
        "generated_at": data.get("generated_at"),
        "valid_until": data.get("valid_until"),
    }


# ── the operator files ──────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class FileState:
    key: str
    raw: bytes | None
    revision: str
    modified: datetime | None

    @property
    def text(self) -> str:
        return self.raw.decode("utf-8", errors="replace") if self.raw else ""

    def as_json(self) -> dict[str, Any]:
        spec = operator_file(self.key)
        return {
            "key": self.key,
            "filename": spec.filename,
            "label": _(spec.label),
            "description": _(spec.description),
            "exists": self.raw is not None,
            "content": self.text,
            "revision": self.revision,
            "modified": self.modified.isoformat() if self.modified else None,
        }


def read_file(config_dir: str, key: str) -> FileState:
    path = path_of(config_dir, key)
    if path.is_symlink():
        raise KnowledgeMapError(_("This file is a symbolic link. Edit it on the host."))
    try:
        raw = path.read_bytes()
        modified = datetime.fromtimestamp(path.stat().st_mtime, tz=UTC)
    except FileNotFoundError:
        return FileState(key, None, "", None)
    except OSError as exc:
        raise KnowledgeMapError(
            _("The file can't be read (%(value)s).") % {"value": exc.strerror or exc}
        ) from exc
    return FileState(key, raw, revision_of(raw), modified)


def write_file(config_dir: str, key: str, data: bytes, *, expected_revision: str) -> FileState:
    """Replace the file if it still has the revision the caller read; keep the old bytes in
    the history first. Raises on a conflict, so a host edit is never overwritten."""
    path = path_of(config_dir, key)
    tmp = path.with_name(f".{path.name}.manager-tmp")
    with _LOCK:
        current = read_file(config_dir, key)
        if current.revision != expected_revision:
            raise KnowledgeMapError(
                _("The file changed after the page loaded it. Reload and try again.")
            )
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            if current.raw is not None:
                _save_history(config_dir, key, current.raw)
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, DEFAULT_MODE)
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            raise KnowledgeMapError(
                _("The file can't be written (%(value)s).") % {"value": exc.strerror or exc}
            ) from exc
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
    return read_file(config_dir, key)


def restore_file(config_dir: str, key: str, previous: bytes | None, written: str) -> bool:
    """Put back what was there before a write whose render failed. A file edited on the host
    since then is left alone (False)."""
    path = path_of(config_dir, key)
    with _LOCK:
        if read_file(config_dir, key).revision != written:
            return False
        try:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                tmp = path.with_name(f".{path.name}.manager-tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, DEFAULT_MODE)
                with os.fdopen(fd, "wb") as handle:
                    handle.write(previous)
                os.replace(tmp, path)
        except OSError:
            return False
    return True


def _save_history(config_dir: str, key: str, raw: bytes) -> None:
    directory = Path(config_dir) / HISTORY_DIR / key
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    stamp = datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%S%fZ")
    target = directory / f"{stamp}.bak"
    fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, DEFAULT_MODE)
    with os.fdopen(fd, "wb") as handle:
        handle.write(raw)
    for old in sorted(directory.glob("*.bak"))[:-HISTORY_KEEP]:
        with contextlib.suppress(OSError):
            old.unlink()


# ── checks ──────────────────────────────────────────────────────────────────────────────────


def check(key: str, content: str) -> list[str]:
    """What is wrong with a draft, before the core's render (the real validator) sees it.
    Shape only: the render also scans for instruction-shaped text and refuses duplicates."""
    operator_file(key)
    raw = content.encode("utf-8")
    if len(raw) > MAX_BYTES:
        return [_("The file is larger than %(value)s KiB.") % {"value": MAX_BYTES // 1024}]
    try:
        document = yaml.safe_load(content)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" ({mark.line + 1}:{mark.column + 1})" if mark is not None else ""
        return [_("Invalid YAML%(where)s: %(problem)s") % {
            "where": where, "problem": getattr(exc, "problem", None) or exc}]
    if document is None:
        return []
    if not isinstance(document, dict):
        return [_("The top level must be a mapping (key: value).")]
    if key == "governance":
        from app.core.knowledge_map_governance import check_document  # noqa: PLC0415 - cycle

        return check_document(document)
    if key == "overrides" and "governance" in document:
        return [_("Governance fields live in governance.yaml, not in overrides.yaml.")]
    return {"sources": _check_sources, "overrides": _check_overrides, "goldset": _check_gold}[
        key
    ](document)


def _check_sources(document: dict[str, Any]) -> list[str]:
    items = document.get("sources") or []
    if not isinstance(items, list):
        return [_("'sources' must be a list of entries.")]
    problems: list[str] = []
    seen: set[str] = set()
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict):
            problems.append(_("Entry %(n)s is not a mapping.") % {"n": index})
            continue
        entry_id = str(item.get("id") or "")
        if not _ID_RE.match(entry_id):
            problems.append(_("Entry %(n)s has no valid id.") % {"n": index})
        elif entry_id in seen:
            problems.append(_("The id %(id)s appears twice.") % {"id": entry_id})
        seen.add(entry_id)
        kind = item.get("kind", "source")
        if kind not in KINDS:
            problems.append(_("%(id)s: unknown kind %(kind)s.") % {"id": entry_id, "kind": kind})
        if not item.get("system") or not item.get("coverage"):
            problems.append(_("%(id)s needs a system and a coverage.") % {"id": entry_id})
        raw_access = item.get("access")
        paths: list[Any] = raw_access if isinstance(raw_access, list) else []
        for path in paths:
            via = path.get("via", "mcp") if isinstance(path, dict) else None
            if via not in VIAS:
                problems.append(
                    _("%(id)s: unknown access path %(via)s.") % {"id": entry_id, "via": via}
                )
            elif via != "none" and not path.get("target"):
                problems.append(
                    _("%(id)s: a %(via)s access path needs a target.")
                    % {"id": entry_id, "via": via}
                )
        if kind == "reference" and not (
            item.get("location") or any(isinstance(p, dict) and p.get("location") for p in paths)
        ):
            problems.append(_("%(id)s: a reference needs a location.") % {"id": entry_id})
    return problems


def _check_overrides(document: dict[str, Any]) -> list[str]:
    items = document.get("sources") or {}
    if not isinstance(items, dict):
        return [_("'sources' must map entry ids to their changes.")]
    problems: list[str] = []
    for entry_id, patch in items.items():
        if not isinstance(patch, dict):
            problems.append(_("The changes for %(id)s are not a mapping.") % {"id": entry_id})
        elif "id" in patch and patch["id"] != entry_id:
            problems.append(_("An override must not change the id (%(id)s).") % {"id": entry_id})
        elif "governance" in patch or "governance_proposal" in patch:
            problems.append(
                _("Governance fields live in governance.yaml, not in overrides.yaml.")
            )
    return problems


def _check_gold(document: dict[str, Any]) -> list[str]:
    items = document.get("questions") or []
    if not isinstance(items, list):
        return [_("'questions' must be a list.")]
    problems: list[str] = []
    for index, item in enumerate(items, start=1):
        if not isinstance(item, dict) or not str(item.get("q") or "").strip():
            problems.append(_("Question %(n)s has no text (q).") % {"n": index})
            continue
        kind = item.get("kind", "single")
        if kind not in GOLD_KINDS:
            problems.append(
                _("Question %(n)s: unknown kind %(kind)s.") % {"n": index, "kind": kind}
            )
        elif kind != "abstention" and not item.get("expect"):
            problems.append(_("Question %(n)s names no expected source.") % {"n": index})
    return problems


# ── accepting a suggestion ──────────────────────────────────────────────────────────────────


def _dump(document: dict[str, Any]) -> bytes:
    return yaml.safe_dump(
        document, sort_keys=False, allow_unicode=True, default_flow_style=False
    ).encode("utf-8")


def _parsed(state: FileState) -> dict[str, Any]:
    try:
        document = yaml.safe_load(state.text) if state.raw else None
    except yaml.YAMLError as exc:
        raise KnowledgeMapError(
            _("%(file)s can't be parsed; fix it first.") % {"file": state.key}
        ) from exc
    if document is None:
        return {}
    if not isinstance(document, dict):
        raise KnowledgeMapError(_("%(file)s can't be parsed; fix it first.") % {"file": state.key})
    return document


def suggestion_change(config_dir: str, suggestion: dict[str, Any]) -> tuple[str, bytes, str]:
    """(file key, new bytes, expected revision) that accepting `suggestion` writes.

    A suggestion carries its change ready-made (the map server built and counter-checked it):
    a patch for one entry goes into the overrides, merged key-wise over what is already
    overridden for that entry; a declaration goes into the own entries unless an entry with
    its id exists; a gold question is appended to the gold set. The written file is plain
    YAML; comments in it are not kept, the previous version is in the history."""
    if suggestion.get("patch") and suggestion.get("target"):
        state = read_file(config_dir, "overrides")
        document = copy.deepcopy(_parsed(state))
        sources = document.setdefault("sources", {})
        if not isinstance(sources, dict):
            raise KnowledgeMapError(_("overrides can't be parsed; fix it first."))
        current = sources.get(suggestion["target"])
        merged = dict(current) if isinstance(current, dict) else {}
        merged.update(suggestion["patch"])
        sources[suggestion["target"]] = merged
        return "overrides", _dump(document), state.revision
    if suggestion.get("declaration"):
        state = read_file(config_dir, "sources")
        document = copy.deepcopy(_parsed(state))
        items = document.setdefault("sources", [])
        if not isinstance(items, list):
            raise KnowledgeMapError(_("sources can't be parsed; fix it first."))
        known = {i.get("id") for i in items if isinstance(i, dict)}
        for item in suggestion["declaration"].get("sources") or []:
            if item.get("id") not in known:
                items.append(item)
        return "sources", _dump(document), state.revision
    if suggestion.get("gold"):
        state = read_file(config_dir, "goldset")
        document = copy.deepcopy(_parsed(state))
        items = document.setdefault("questions", [])
        if not isinstance(items, list):
            raise KnowledgeMapError(_("goldset can't be parsed; fix it first."))
        if all(i.get("q") != suggestion["gold"].get("q") for i in items if isinstance(i, dict)):
            items.append(suggestion["gold"])
        return "goldset", _dump(document), state.revision
    raise KnowledgeMapError(_("This suggestion carries no change."))
