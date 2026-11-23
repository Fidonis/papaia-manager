#!/usr/bin/env python3
"""Gate over the report of the kit's design check (`pk-conform --design`).

`pk-conform --design` checks pages of a running manager against the kit's design rules
(font, spacing scale, target gap, primary action, measure, narrow text, outline, reflow)
and axe in both themes; it accepts known axe findings below "serious" through `--known`
but no known design findings. This gate reads its report (`papaia.pk-design/1`) and
`ui/design-known.yaml`, which lists the design findings that only the page rebuild
removes, each with its ticket:

- a design finding passes only when an entry names its check and page (the page as in
  ui/pages.yaml; `{name}` stands for one path segment);
- an entry that matches no finding is stale and fails, so the list shrinks with every
  rebuilt page;
- an axe finding of impact serious or critical, which the kit's run never accepts as
  known, passes only when an entry names it as check `axe-<rule>` on its page; every
  other failed accessibility part of the report (a finding below serious that
  `--known` does not list) fails. The list is the manager's debt from before the shared
  design layer: no new finding joins it, and an entry goes once its page is rebuilt.

The list has a strict YAML subset: a list of flat mappings with exactly the fields check,
page, ticket and reason, plain or double-quoted values without escapes, comments on own
lines. Messages name checks, pages, themes, widths and line numbers only, never other
values of the files.

Usage: scripts/check_design.py --report FILE [--known ui/design-known.yaml]
Exit codes: 0 pass, 1 finding not listed, stale entry or accessibility failure,
2 a file is missing, unreadable, too large or malformed.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_FILE = 256 * 1024
MAX_REPORT = 16 * 1024 * 1024

# The design checks of pk-conform --design in kit 0.2.2 (contract/design-review.md).
CHECKS = frozenset(
    {
        "font",
        "spacing-scale",
        "target-gap",
        "primary-action",
        "measure",
        "narrow-text",
        "outline",
        "reflow",
    }
)
# An axe rule of the accessibility part of the report, as a check of the known list.
AXE_PREFIX = "axe-"
_AXE_RULE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")

_FIELDS: dict[str, re.Pattern[str]] = {
    "check": re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*"),
    "page": re.compile(r"/[A-Za-z0-9._~/{}-]{0,200}"),
    # An issue (`<repo>#<number>`) or, until the issue exists, the plan's ticket id.
    "ticket": re.compile(r"[A-Za-z0-9._/-]{1,80}#[0-9]{1,7}|[A-Z]{2,6}-[0-9]{3}[a-z]?"),
    "reason": re.compile(r".{10,500}"),
}
_LINE = re.compile(r"(- |  )([a-z_]+):(?: (.*))?")
_PLACEHOLDER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")


class KnownListError(ValueError):
    """design-known.yaml is malformed."""


class ReportError(ValueError):
    """The report is not a papaia.pk-design/1 document."""


@dataclass(frozen=True)
class KnownEntry:
    check: str
    page: str
    ticket: str
    reason: str

    def matches(self, check: str, page: str) -> bool:
        if check != self.check:
            return False
        if self.page == page:
            return True
        if "{" not in self.page:
            return False
        parts = _PLACEHOLDER.split(self.page)
        pattern = "[^/?#]+".join(re.escape(p) for p in parts)
        return re.fullmatch(pattern, page.split("?", 1)[0]) is not None


@dataclass(frozen=True)
class Finding:
    page: str
    theme: str
    # The width of a design finding (desktop, phone, reflow), the state of an axe finding.
    viewport: str
    check: str


@dataclass
class Result:
    listed: list[Finding] = field(default_factory=list)
    unlisted: list[Finding] = field(default_factory=list)
    stale: list[KnownEntry] = field(default_factory=list)
    a11y_failed: bool = False

    @property
    def ok(self) -> bool:
        return not (self.unlisted or self.stale or self.a11y_failed)


def _scalar(raw: str, where: str) -> str:
    text = raw.strip()
    if text.startswith('"'):
        if not re.fullmatch(r'"[^"\\]*"', text):
            raise KnownListError(f"{where}: quoted value without escapes expected")
        return text[1:-1]
    if re.match(r"[\[{&*!|>'%@`]", text) or " #" in text:
        raise KnownListError(f'{where}: plain value or "quoted" value expected')
    return text


def parse_known(text: str) -> list[KnownEntry]:
    """Read design-known.yaml strictly; raise KnownListError on any other form."""
    if len(text.encode("utf-8")) > MAX_FILE:
        raise KnownListError("design-known.yaml: larger than 256 KiB")
    raw: list[dict[str, str]] = []
    for number, line in enumerate(text.split("\n"), start=1):
        where = f"design-known.yaml line {number}"
        if re.fullmatch(r"\s*(#.*)?", line):
            continue
        match = _LINE.fullmatch(line)
        if match is None:
            raise KnownListError(f'{where}: expected "- key: value" or "  key: value"')
        lead, key, value = match.group(1), match.group(2), match.group(3) or ""
        if lead == "- ":
            raw.append({})
        if not raw:
            raise KnownListError(f'{where}: entry must start with "- "')
        if key not in _FIELDS:
            raise KnownListError(f"{where}: unknown field {key}")
        if key in raw[-1]:
            raise KnownListError(f"{where}: field {key} given twice")
        raw[-1][key] = _scalar(value, where)
    entries: list[KnownEntry] = []
    seen: set[tuple[str, str]] = set()
    for index, values in enumerate(raw, start=1):
        for name, pattern in _FIELDS.items():
            if name not in values:
                raise KnownListError(f"design-known.yaml entry {index}: missing {name}")
            if not pattern.fullmatch(values[name]):
                raise KnownListError(f"design-known.yaml entry {index}: {name} has the wrong form")
        key = (values["check"], values["page"])
        if key in seen:
            raise KnownListError(f"design-known.yaml entry {index}: duplicate check and page")
        seen.add(key)
        entries.append(KnownEntry(**values))
    return entries


def _text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 500:
        raise ReportError(f"report: {what} must be a non-empty string")
    return value


def _findings(doc: object) -> list[Finding]:
    if not isinstance(doc, dict) or doc.get("format") != "papaia.pk-design/1":
        raise ReportError("report: format papaia.pk-design/1 expected")
    if doc.get("status") not in ("pass", "fail"):
        raise ReportError("report: status must be pass or fail")
    pages = doc.get("pages")
    if not isinstance(pages, list):
        raise ReportError("report: pages must be a list")
    found: list[Finding] = []
    for index, page in enumerate(pages, start=1):
        if not isinstance(page, dict) or not isinstance(page.get("findings"), list):
            raise ReportError(f"report: page {index} malformed")
        path = _text(page.get("page"), f"page {index} path")
        theme = _text(page.get("theme"), f"page {index} theme")
        viewport = _text(page.get("viewport"), f"page {index} viewport")
        for item in page["findings"]:
            if not isinstance(item, dict):
                raise ReportError(f"report: finding of page {index} malformed")
            check = _text(item.get("check"), f"finding of page {index} check")
            found.append(Finding(path, theme, viewport, check))
    return found


def _a11y(doc: dict[str, Any]) -> tuple[list[Finding], bool]:
    """The blocking axe findings as `axe-<rule>` findings, and whether anything else failed.

    `blocking` holds the serious and critical findings, one per rule, page, theme and
    state; `unlisted` the findings below serious that `--known` does not list.
    """
    a11y = doc.get("a11y")
    if not isinstance(a11y, dict) or not isinstance(a11y.get("ok"), bool):
        raise ReportError("report: a11y.ok must be true or false")
    blocking = a11y.get("blocking", [])
    unlisted = a11y.get("unlisted", [])
    if not isinstance(blocking, list) or not isinstance(unlisted, list):
        raise ReportError("report: a11y.blocking and a11y.unlisted must be lists")
    found: list[Finding] = []
    for index, item in enumerate(blocking, start=1):
        if not isinstance(item, dict):
            raise ReportError(f"report: blocking finding {index} malformed")
        rule = _text(item.get("id"), f"blocking finding {index} id")
        if not _AXE_RULE.fullmatch(rule):
            raise ReportError(f"report: blocking finding {index} id has the wrong form")
        page = _text(item.get("page"), f"blocking finding {index} page")
        theme = _text(item.get("theme"), f"blocking finding {index} theme")
        state = _text(item.get("state"), f"blocking finding {index} state")
        found.append(Finding(page, theme, state, AXE_PREFIX + rule))
    # A failed part with nothing named in it cannot be listed, so it fails as a whole.
    other = bool(unlisted) or (not a11y["ok"] and not blocking)
    return found, other


def evaluate(doc: Any, known: Sequence[KnownEntry]) -> Result:
    """Sort the findings of a pk-design report into listed and unlisted ones."""
    findings = _findings(doc)
    blocking, a11y_failed = _a11y(doc)
    result = Result(a11y_failed=a11y_failed)
    used: set[KnownEntry] = set()
    for finding in findings + blocking:
        entry = next((k for k in known if k.matches(finding.check, finding.page)), None)
        if entry is None:
            result.unlisted.append(finding)
        else:
            used.add(entry)
            result.listed.append(finding)
    result.stale = [k for k in known if k not in used]
    return result


def _read(path: Path, limit: int) -> str:
    with path.open("rb") as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError(f"{path.name}: too large")
    return data.decode("utf-8")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--report", required=True, type=Path, help="report of pk-conform --design")
    parser.add_argument("--known", type=Path, default=Path("ui/design-known.yaml"))
    args = parser.parse_args(argv)
    try:
        known = parse_known(_read(args.known, MAX_FILE))
        result = evaluate(json.loads(_read(args.report, MAX_REPORT)), known)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        # ValueError covers JSON errors, KnownListError and ReportError; a decoding
        # error would quote bytes of the file, so only its kind is named.
        message = "not UTF-8" if isinstance(exc, UnicodeDecodeError) else str(exc)
        print(f"check-design: cannot read the input: {message}", file=sys.stderr)
        return 2
    for f in result.unlisted:
        print(f"{f.page} ({f.theme}, {f.viewport}) {f.check}: not listed", file=sys.stderr)
    for k in result.stale:
        print(f"{k.page} {k.check}: stale entry ({k.ticket}), remove it", file=sys.stderr)
    if result.a11y_failed:
        print("accessibility: the report has unlisted findings below serious", file=sys.stderr)
    if result.ok:
        print(f"check-design: pass ({len(result.listed)} listed finding(s) wait for their ticket)")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
