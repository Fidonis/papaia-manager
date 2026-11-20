"""The Flow link of an add-on at its installation.

An add-on's manifest can carry a `flow:` section: events that start or wake a workflow in papaia
Flow, and actions a workflow may call. Flow treats every declaration as a proposal. The core's
`papaia-ctl addon install` (from the release with `--approve-flow-link`) shows what Flow read and,
on confirmation, asks Flow to approve exactly that declaration with a person token of the
installing administrator. Flow then decides by its own policy whether one administrator is
enough; otherwise the link waits for a second person.

This module is the manager's share of that: the summary the install dialog shows (a preview from
the catalog's manifest; the text that decides is the one Flow prints into the job log), whether
Flow is installed, whether the core can be asked to approve, and the outcome read back from what
the core printed. The manager interprets nothing of the section beyond showing it.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from app.core.ctl import FLOW_TOKEN_ENV

FLOW_ADDON = "papaia-flow"
SECTION = "flow"
APPROVE_FLAG = "--approve-flow-link"
# Where the core reads the person token from: the environment of the child only, since an
# argument would show in the process list.
TOKEN_ENV = FLOW_TOKEN_ENV

# The dialog shows a declaration, not a document: each value is cut, and a long list says how
# many entries it did not show.
MAX_TEXT = 200
MAX_ITEMS = 50

_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class FlowEvent:
    name: str
    format: str
    data_class: str


@dataclass(frozen=True)
class FlowAction:
    name: str
    identity: str
    effect: str
    data_class: str
    address: str


@dataclass(frozen=True)
class FlowLinkPreview:
    events: tuple[FlowEvent, ...]
    actions: tuple[FlowAction, ...]
    more_events: int = 0
    more_actions: int = 0


def _text(value: Any) -> str:
    """A plain, single-line, bounded string; anything that is not a string is nothing."""
    if not isinstance(value, str):
        return ""
    return _SPACE.sub(" ", value).strip()[:MAX_TEXT]


def _address(action: Mapping[str, Any]) -> str:
    if action.get("via") == "rest":
        method = _text(action.get("method"))
        target = _text(f"{_text(action.get('base_url'))}{_text(action.get('path'))}")
        return f"{method} {target}".strip()
    server, tool = _text(action.get("server")), _text(action.get("tool"))
    if server and tool:
        return f"{server}/{tool}"
    return server or tool


def _mappings(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, Mapping)]


def preview(manifest: Mapping[str, Any]) -> FlowLinkPreview | None:
    """What the link of this manifest may do, or None when it declares no link."""
    section = manifest.get(SECTION)
    if not isinstance(section, Mapping):
        return None
    events = _mappings(section.get("events"))
    actions = _mappings(section.get("actions"))
    return FlowLinkPreview(
        events=tuple(
            FlowEvent(
                name=_text(e.get("name")),
                format=_text(e.get("format")),
                data_class=_text(e.get("data_class_hint")),
            )
            for e in events[:MAX_ITEMS]
        ),
        actions=tuple(
            FlowAction(
                name=_text(a.get("name")),
                identity=_text(a.get("identity")),
                effect=_text(a.get("effect")),
                data_class=_text(a.get("data_class_max")),
                address=_address(a),
            )
            for a in actions[:MAX_ITEMS]
        ),
        more_events=max(0, len(events) - MAX_ITEMS),
        more_actions=max(0, len(actions) - MAX_ITEMS),
    )


def flow_installed(deployment: Mapping[str, Any]) -> bool:
    """Whether the workflow module is an active add-on, the core's own test for it."""
    entries = deployment.get("addons") or []
    return any(
        isinstance(entry, Mapping) and entry.get("name") == FLOW_ADDON and entry.get("active")
        for entry in entries
    )


@lru_cache(maxsize=4)
def _knows_flag(script: str, mtime_ns: int) -> bool:
    del mtime_ns  # part of the cache key: a core upgraded in place is read again
    try:
        return APPROVE_FLAG in Path(script).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def core_approves_at_install(workspace_dir: str) -> bool:
    """Whether the core in the workspace takes `--approve-flow-link`.

    An older core refuses an unknown option of `addon install` and would fail the whole
    installation, so the flag is passed only to a core whose installer names it."""
    script = Path(workspace_dir) / "papaia" / "tools" / "lib" / "sh" / "addon.sh"
    try:
        return _knows_flag(str(script), script.stat().st_mtime_ns)
    except OSError:
        return False


def outcome(name: str, lines: Iterable[str]) -> tuple[str, list[str]]:
    """The outcome of the link of `name` as the core printed it, and Flow's reasons.

    One of approved, proposal (with the reasons), not_approved, error, no_token, not_read,
    unchanged, skipped, or none when the core printed nothing about a link. The core's wording
    is the contract here (`flow_link.link_at_install` in the core); a line it changes reads as
    none and the job log still has the text."""
    proposal = f"Flow link of {name} stays a proposal for a second person:"
    checks = (
        (f"Flow link of {name} approved at install.", "approved"),
        (f"Flow link of {name} not approved; it stays a proposal.", "not_approved"),
        (f"WARNING: Flow link of {name} not approved:", "error"),
        (f"NOTE: {name} declares a Flow link; without a token", "no_token"),
        (f"WARNING: Flow did not show the link of {name} ", "not_read"),
        (f"Flow link of {name}: nothing to approve", "unchanged"),
        (f"NOTE: {name} declares a Flow link; Flow is not installed.", "skipped"),
    )
    for raw in lines:
        line = raw.strip()
        if line.startswith(proposal):
            reasons = [r.strip() for r in line[len(proposal):].split(",") if r.strip()]
            return "proposal", reasons
        for prefix, result in checks:
            if line.startswith(prefix):
                return result, []
    return "none", []
