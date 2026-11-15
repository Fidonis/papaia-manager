"""One entry of the AI map as a form instead of a YAML file.

The entry editor writes the same two operator files as the file editor, only one entry at a time:

* an entry of the operator's own lives in `sources.yaml`; saving replaces it there, removing
  deletes it;
* an entry an add-on, a module or the inventory declares is changed through `overrides.yaml`:
  only the fields that differ from what is rendered now go into its override (merged over the
  fields already overridden), and resetting a field removes it from the override, so the
  declaration's value applies again with the next render.

Only the fields in `EDITABLE` are touched; everything else in an entry (governance, provenance,
fields a newer core knows) is kept as it is. Each change is returned as the file's new bytes
plus the revision it was computed from, and written by the same compare-and-swap job as a hand
edit (`knowledge_map_jobs.write_callback`).
"""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import yaml

from app.core import knowledge_map as km
from app.i18n import gettext as _

# The fields the form edits. Lists replace the declared list as a whole in an override (the
# core's merge rule), which is what a form that shows the whole list wants anyway.
EDITABLE = (
    "kind",
    "system",
    "coverage",
    "topics",
    "example_queries",
    "counter_examples",
    "not_covered",
    "source_of_truth_for",
    "required_roles",
    "on_denied",
    "access",
    "location",
    "delegate_when",
    "uses",
)
_LISTS_OF_TEXT = (
    "example_queries",
    "counter_examples",
    "source_of_truth_for",
    "required_roles",
    "uses",
)
_ACCESS_KEYS = (
    "via",
    "target",
    "collection",
    "location",
    "best_for",
    "freshness",
    "key_tools",
    "required_roles",
)
# What the core assumes for a field that is not declared; a form value equal to it is no change.
_DEFAULTS = {"kind": "source", "on_denied": "hidden"}
ON_DENIED = ("hidden", "acknowledge")
MAX_ITEMS = 50
MAX_TEXT = 2000
_SLUG_RE = re.compile(r"[^a-z0-9]+")
_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})


def _slug(text: str) -> str:
    plain = text.lower().translate(_UMLAUTS)
    return _SLUG_RE.sub("-", plain).strip("-")[:80]


def _text(value: Any) -> str:
    return str(value).strip()[:MAX_TEXT] if isinstance(value, str | int | float) else ""


def _texts(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    seen: list[str] = []
    for item in value[:MAX_ITEMS]:
        text = _text(item)
        if text and text not in seen:
            seen.append(text)
    return seen


def _topics(value: Any) -> list[dict[str, Any]]:
    topics: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if isinstance(item, str):
            item = {"id": item, "label": item}
        if not isinstance(item, dict):
            continue
        label = _text(item.get("label"))
        topic_id = _text(item.get("id")) or _slug(label)
        if not topic_id or any(t["id"] == topic_id for t in topics):
            continue
        topic: dict[str, Any] = {"id": topic_id, "label": label or topic_id}
        synonyms = _texts(item.get("synonyms"))
        if synonyms:
            topic["synonyms"] = synonyms
        topics.append(topic)
    return topics[:MAX_ITEMS]


def _not_covered(value: Any) -> list[dict[str, str]]:
    items: list[dict[str, str]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict) or not _text(item.get("what")):
            continue
        row = {"what": _text(item.get("what"))}
        if _text(item.get("instead")):
            row["instead"] = _text(item.get("instead"))
        items.append(row)
    return items[:MAX_ITEMS]


def _access(value: Any) -> list[dict[str, Any]]:
    paths: list[dict[str, Any]] = []
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        path: dict[str, Any] = {}
        for key in _ACCESS_KEYS:
            if key in ("key_tools", "required_roles"):
                texts = _texts(item.get(key))
                if texts:
                    path[key] = texts
            elif _text(item.get(key)):
                path[key] = _text(item.get(key))
        if path:
            path.setdefault("via", "mcp")
            paths.append(path)
    return paths[:MAX_ITEMS]


def clean(fields: dict[str, Any]) -> dict[str, Any]:
    """The editable fields of a form, normalised: unknown keys dropped, text trimmed, empty
    values left out (an empty field means "not declared", not "declared empty")."""
    out: dict[str, Any] = {}
    for key in EDITABLE:
        if key not in fields:
            continue
        value = fields[key]
        if key == "topics":
            cleaned: Any = _topics(value)
        elif key == "not_covered":
            cleaned = _not_covered(value)
        elif key == "access":
            cleaned = _access(value)
        elif key in _LISTS_OF_TEXT:
            cleaned = _texts(value)
        else:
            cleaned = _text(value)
        if cleaned:
            out[key] = cleaned
    return out


# ── reading ─────────────────────────────────────────────────────────────────────────────────


def _manifest(config_dir: str, entry_id: str) -> dict[str, Any] | None:
    manifests = Path(config_dir) / km.MAP_DIR / "manifests"
    if not manifests.is_dir():
        return None
    for path in sorted(manifests.glob("*.yaml")):
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if isinstance(data, dict) and str(data.get("id")) == entry_id:
            return data
    return None


def _own(document: dict[str, Any]) -> list[Any]:
    items = document.get("sources")
    if items is None:
        return []
    if not isinstance(items, list):
        raise km.KnowledgeMapError(_("sources can't be parsed; fix it first."))
    return items


def _overrides(document: dict[str, Any]) -> dict[str, Any]:
    items = document.get("sources")
    if items is None:
        return {}
    if not isinstance(items, dict):
        raise km.KnowledgeMapError(_("overrides can't be parsed; fix it first."))
    return items


def _origin(declared_by: str) -> str:
    if declared_by == "operator":
        return "own"
    if declared_by.startswith("inventory:"):
        return "inventory"
    return "declared"


def form(config_dir: str, entry_id: str) -> dict[str, Any]:
    """What the editor needs for one entry: its editable fields as they apply now, who
    declared it, which fields the operator changed, and the revision of the file a save
    writes."""
    sources_state = km.read_file(config_dir, "sources")
    overrides_state = km.read_file(config_dir, "overrides")
    own = next(
        (
            i
            for i in _own(km._parsed(sources_state))
            if isinstance(i, dict) and str(i.get("id")) == entry_id
        ),
        None,
    )
    if own is not None:
        return {
            "id": entry_id,
            "origin": "own",
            "declared_by": "operator",
            "fields": {k: copy.deepcopy(own[k]) for k in EDITABLE if k in own},
            "overridden": [],
            "file": "sources",
            "revision": sources_state.revision,
        }
    manifest = _manifest(config_dir, entry_id)
    if manifest is None:
        raise km.KnowledgeMapError(_("Unknown entry."))
    provenance = manifest.get("provenance")
    declared_by = str((provenance if isinstance(provenance, dict) else {}).get("declared_by", ""))
    override = _overrides(km._parsed(overrides_state)).get(entry_id)
    return {
        "id": entry_id,
        "origin": _origin(declared_by),
        "declared_by": declared_by,
        "fields": {k: copy.deepcopy(manifest[k]) for k in EDITABLE if k in manifest},
        "overridden": sorted(k for k in (override or {}) if k in EDITABLE)
        if isinstance(override, dict)
        else [],
        "file": "overrides",
        "revision": overrides_state.revision,
    }


# ── changing ────────────────────────────────────────────────────────────────────────────────


def _problems_of(entry: dict[str, Any]) -> list[str]:
    return km._check_sources({"sources": [entry]})


def create(config_dir: str, fields: dict[str, Any]) -> tuple[str, bytes, str, list[str]]:
    """A new entry of the operator's own, appended to `sources.yaml`."""
    entry_id = _text(fields.get("id"))
    entry = {"id": entry_id, **clean(fields)}
    entry.setdefault("kind", "source")
    known = {e["id"] for e in km.rendered(config_dir)["entries"]}
    state = km.read_file(config_dir, "sources")
    document = copy.deepcopy(km._parsed(state))
    items = document.setdefault("sources", [])
    if not isinstance(items, list):
        raise km.KnowledgeMapError(_("sources can't be parsed; fix it first."))
    known |= {str(i.get("id")) for i in items if isinstance(i, dict)}
    if entry_id in known:
        return (
            "sources",
            b"",
            state.revision,
            [_("The id %(id)s is taken; choose another one.") % {"id": entry_id}],
        )
    items.append(entry)
    return "sources", km._dump(document), state.revision, _problems_of(entry)


def update(
    config_dir: str, entry_id: str, fields: dict[str, Any], reset: list[str]
) -> tuple[str, bytes, str, list[str]]:
    """Save the form of an existing entry. Own entries are replaced in `sources.yaml`
    (non-editable keys kept); declared entries get an override of the changed fields."""
    current = form(config_dir, entry_id)
    wanted = clean(fields)
    if current["origin"] == "own":
        state = km.read_file(config_dir, "sources")
        document = copy.deepcopy(km._parsed(state))
        items = _own(document)
        for index, item in enumerate(items):
            if isinstance(item, dict) and str(item.get("id")) == entry_id:
                kept = {k: v for k, v in item.items() if k not in EDITABLE}
                entry = {"id": entry_id, **{k: v for k, v in kept.items() if k != "id"}, **wanted}
                items[index] = entry
                return "sources", km._dump(document), state.revision, _problems_of(entry)
        raise km.KnowledgeMapError(_("Unknown entry."))

    state = km.read_file(config_dir, "overrides")
    document = copy.deepcopy(km._parsed(state))
    overrides = document.setdefault("sources", {})
    if not isinstance(overrides, dict):
        raise km.KnowledgeMapError(_("overrides can't be parsed; fix it first."))
    existing = overrides.get(entry_id)
    patch = dict(existing) if isinstance(existing, dict) else {}
    effective = clean(current["fields"])
    for key in EDITABLE:
        if key in reset:
            patch.pop(key, None)
            continue
        if key not in fields:
            continue
        now, then = wanted.get(key, _DEFAULTS.get(key)), effective.get(key, _DEFAULTS.get(key))
        if now != then:
            # A field emptied in the form overrides the declaration with "nothing".
            empty: Any = [] if key in (*_LISTS_OF_TEXT, "topics", "not_covered", "access") else ""
            patch[key] = now if now is not None else empty
    if patch:
        overrides[entry_id] = patch
    else:
        overrides.pop(entry_id, None)
    if not overrides:
        document.pop("sources")
    merged = {"id": entry_id, **effective, **{k: v for k, v in patch.items() if k in EDITABLE}}
    for key in reset:
        # What the declaration says again is not known here; the render checks it.
        merged.pop(key, None)
    problems = km._check_overrides(document) + (_problems_of(merged) if not reset else [])
    return "overrides", km._dump(document), state.revision, problems


def remove(config_dir: str, entry_id: str) -> tuple[str, bytes, str]:
    """Delete an entry of the operator's own. A declared entry can't be removed here: it
    belongs to its add-on (remove the add-on, or restrict its roles)."""
    state = km.read_file(config_dir, "sources")
    document = copy.deepcopy(km._parsed(state))
    items = _own(document)
    kept = [i for i in items if not (isinstance(i, dict) and str(i.get("id")) == entry_id)]
    if len(kept) == len(items):
        raise km.KnowledgeMapError(
            _("Only entries of your own can be removed; this one is declared elsewhere.")
        )
    document["sources"] = kept
    return "sources", km._dump(document), state.revision
