"""The platform's word check over the manager's texts (PLT-27-A2…A5, interface spec 4.3).

Runs the kit's `papaia_kit.ui.words.check_module` over the export of the interface texts
(`i18n/export`, current per `python -m app.i18n.export --check`), the glossary map
`i18n/glossary-map.yaml` and the templates. Findings that are known and reviewed stand in
`i18n/words-known.yaml`, each group with its reason and the ticket that removes it. The check
fails on a finding that is not listed and on a listed finding that no longer occurs, so the
list only ever shrinks. Mandatory places (W2), restricted words in labels (W3) and
completeness (W4) are never listed: they have to pass.

    python -m app.i18n.wordcheck
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml
from papaia_kit.ui.words import WordFinding, check_module

from app.i18n.export import ROOT

KNOWN = ROOT / "i18n" / "words-known.yaml"
_TICKET = re.compile(r"MGR-\d{3}")
_LISTABLE = frozenset({"W1", "W5"})
_MAX_BYTES = 2 * 1024 * 1024


def finding_id(f: WordFinding) -> str:
    return f"{f.check} {f.location} {f.match}"


def known(path: Path | None = None) -> set[str]:
    """The listed findings; a malformed list stops the check."""
    path = path or KNOWN
    raw = path.read_bytes()
    if len(raw) > _MAX_BYTES:
        raise SystemExit(f"{path.name}: larger than {_MAX_BYTES} bytes")
    data = yaml.safe_load(raw.decode("utf-8"))
    if not isinstance(data, dict) or set(data) != {"version", "groups"} or data["version"] != 1:
        raise SystemExit(f"{path.name}: expected version 1 and groups")
    if not isinstance(data["groups"], list):
        raise SystemExit(f"{path.name}: groups is a list")
    out: set[str] = set()
    for group in data["groups"]:
        if not isinstance(group, dict) or set(group) != {"ticket", "reason", "findings"}:
            raise SystemExit(f"{path.name}: a group has exactly ticket, reason, findings")
        if not _TICKET.fullmatch(str(group["ticket"])) or not str(group["reason"]).strip():
            raise SystemExit(f"{path.name}: group without ticket or reason")
        if not isinstance(group["findings"], list):
            raise SystemExit(f"{path.name}: findings is a list")
        for item in group["findings"]:
            text = str(item)
            if text.split(" ", 1)[0] not in _LISTABLE:
                raise SystemExit(f"{path.name}: only W1 and W5 findings can be listed: {text}")
            if text in out:
                raise SystemExit(f"{path.name}: listed twice: {text}")
            out.add(text)
    return out


def findings() -> list[WordFinding]:
    return [f for f in check_module(ROOT) if f.severity == "error"]


def main() -> int:
    found_list = findings()
    found = {finding_id(f) for f in found_list}
    listed = known(KNOWN)
    new = found - listed
    stale = sorted(listed - found)
    for f in found_list:
        if finding_id(f) in new:
            print(f"wordcheck: {f.check} {f.location}: {f.message}")
    for item in stale:
        print(f"wordcheck: listed but no longer found, remove it: {item}")
    if new or stale:
        return 1
    print(f"wordcheck: no new finding ({len(found)} known, see i18n/words-known.yaml)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
