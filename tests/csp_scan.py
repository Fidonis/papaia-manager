"""Count what stands between a template and the strict content security policy (T-UI-03).

The ratchet of tests/test_csp_ratchet.py compares these counts per template of
src/app/templates/ with tests/csp_baseline.json. Five kinds are counted:

- `inline_scripts`: `<script>` elements without `src` whose type the browser executes (a data
  island, `type="application/json"`, is not executable and is not counted);
- `on_attributes`: inline event handlers (`onclick`, `onchange`, ...);
- `alpine_unsupported`: Alpine attributes the CSP build of Alpine (tests/csp_baseline.json)
  rejects. The forms are those the browser measurement of that build found on the pages
  (tests/csp_browser.mjs --candidate): every `x-html`; an arrow function; a template literal;
  a reach for a global object (`window`, `document`, `Math`, `JSON`, `String`, ...); more
  than one statement (`;`), `return`, `if`, `new`, `typeof`, `function`, optional chaining;
  a method or getter written into an `x-data` object;
- `style_blocks`: `<style>` elements;
- `style_attributes`: `style="..."` attributes (Alpine's `:style` sets properties through the
  CSSOM, which the policy allows, and is not counted).

Jinja statements and comments are blanked and Jinja expressions replaced by a token before the
markup is parsed, so a value the server fills in never looks like markup. A test tool, never
part of the image.

Usage (from the repository root):
  python3 tests/csp_scan.py            print the counts of every template as JSON
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Iterable
from html.parser import HTMLParser
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TEMPLATES = REPO / "src" / "app" / "templates"
BASELINE = REPO / "tests" / "csp_baseline.json"

KINDS = (
    "inline_scripts",
    "on_attributes",
    "alpine_unsupported",
    "style_blocks",
    "style_attributes",
)

# Script types a browser does not execute: data blocks. Everything else, the empty type and
# `module` included, runs.
_DATA_TYPES = frozenset({"application/json", "application/ld+json", "text/template"})
_JINJA_BLOCK = re.compile(r"\{%.*?%\}|\{#.*?#\}", re.DOTALL)
_JINJA_VALUE = re.compile(r"\{\{.*?\}\}", re.DOTALL)
# Global objects the expressions of a converted page cannot reach (the list of
# tests/csp_browser.mjs).
_GLOBAL_NAMES = (
    "window", "document", "globalThis", "self", "Math", "JSON", "console", "localStorage",
    "sessionStorage", "navigator", "location", "history", "Intl", "Number", "String", "Object",
    "Array", "Date", "Promise", "Reflect", "setTimeout", "clearTimeout", "setInterval",
    "clearInterval", "fetch", "alert", "confirm", "prompt", "encodeURIComponent",
    "decodeURIComponent", "parseInt", "parseFloat", "isNaN", "htmx", "Alpine",
)  # fmt: skip
_GLOBALS = re.compile(r"(?<![\w$.])(" + "|".join(_GLOBAL_NAMES) + r")\b(?!\s*:)")
# Forms the parser of the CSP build does not take, searched with string literals removed.
_FORMS = re.compile(
    r"=>"
    r"|;\s*\S"
    r"|(?<![\w$.])(return|if|new|typeof|function|instanceof|delete|void)\b(?!\s*:)"
    r"|\?\."
    r"|(?<![\w$.])(get|set)\s+[A-Za-z_$][\w$]*\s*\("
    r"|[A-Za-z_$][\w$]*\s*\([^()]*\)\s*\{"
)
_STRINGS = re.compile(r"'(?:[^'\\]|\\.)*'|\"(?:[^\"\\]|\\.)*\"")


def _blank(match: re.Match[str]) -> str:
    # Keep the line count, so a reported position stays close to the source.
    return "\n" * match.group(0).count("\n")


def strip_jinja(source: str) -> str:
    """The markup of a template with Jinja statements, comments and values neutralised."""
    return _JINJA_VALUE.sub("jinja", _JINJA_BLOCK.sub(_blank, source))


def is_alpine_attribute(name: str) -> bool:
    return name.startswith(("x-", "@", ":"))


def alpine_rejected(name: str, value: str | None) -> bool:
    """Whether the CSP build of Alpine rejects this attribute."""
    if name == "x-html" or name.startswith("x-html."):
        return True
    if value is None:
        return False
    if "`" in value:
        return True
    code = _STRINGS.sub("''", value)
    return _FORMS.search(code) is not None or _GLOBALS.search(code) is not None


class _Counter(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.counts = dict.fromkeys(KINDS, 0)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._count(tag, attrs)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._count(tag, attrs)

    def _count(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        names = {name for name, _ in attrs}
        if tag == "script" and "src" not in names:
            kind = (dict(attrs).get("type") or "").strip().lower()
            if kind not in _DATA_TYPES:
                self.counts["inline_scripts"] += 1
        if tag == "style":
            self.counts["style_blocks"] += 1
        for name, value in attrs:
            if name.startswith("on") and len(name) > 2 and name[2:].isalpha():  # noqa: PLR2004
                self.counts["on_attributes"] += 1
            elif name == "style":
                self.counts["style_attributes"] += 1
            elif is_alpine_attribute(name) and alpine_rejected(name, value):
                self.counts["alpine_unsupported"] += 1


def count_markup(source: str) -> dict[str, int]:
    """The five counts of one template's source."""
    counter = _Counter()
    counter.feed(strip_jinja(source))
    counter.close()
    return counter.counts


def scan(templates: Path = TEMPLATES) -> dict[str, dict[str, int]]:
    """Counts of every template under `templates` with at least one finding, by relative path."""
    result: dict[str, dict[str, int]] = {}
    for path in sorted(templates.rglob("*.html")):
        counts = count_markup(path.read_text(encoding="utf-8"))
        if any(counts.values()):
            result[path.relative_to(templates).as_posix()] = counts
    return result


def totals(counts: Iterable[dict[str, int]]) -> dict[str, int]:
    total = dict.fromkeys(KINDS, 0)
    for row in counts:
        for kind in KINDS:
            total[kind] += row.get(kind, 0)
    return total


def compare(baseline: dict[str, dict[str, int]], measured: dict[str, dict[str, int]]) -> list[str]:
    """Every difference between baseline and measurement; empty when they are equal.

    A count above the baseline is a new finding. A count below it is progress that has to be
    written into the baseline in the same change, so the baseline only ever shrinks.
    """
    problems: list[str] = []
    for template in sorted(set(baseline) | set(measured)):
        was = baseline.get(template, {})
        now = measured.get(template, {})
        for kind in KINDS:
            before, after = was.get(kind, 0), now.get(kind, 0)
            if after > before:
                problems.append(f"{template}: {kind} rose from {before} to {after}")
            elif after < before:
                problems.append(
                    f"{template}: {kind} fell from {before} to {after}; "
                    "lower tests/csp_baseline.json in the same change"
                )
    return problems


def load_baseline(path: Path = BASELINE) -> dict[str, object]:
    doc: dict[str, object] = json.loads(path.read_text(encoding="utf-8"))
    return doc


def main() -> int:
    measured = scan()
    json.dump(
        {"templates": measured, "totals": totals(measured.values())},
        sys.stdout,
        indent=2,
        sort_keys=True,
    )
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
