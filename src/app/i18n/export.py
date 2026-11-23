"""Export of the interface texts for the platform's word check (PLT-27).

The kit's word check (`papaia_kit.ui.words`) reads every text of a module's interface as
`i18n/export/<lang>.json` at the repository root: entries `{key, text, kind}`. English is the
source language of the catalogue (the message ids), German comes from
`app/locale/de/LC_MESSAGES/messages.po`.

A text gets its kind from where it is shown, because the word check is stricter on labels
than on sentences:

* `nav`: the rows and group headings of the manager's navigation (`app.shell.nav_groups`);
* `title`: page titles (`{% block page_title %}`) and headings `h1`…`h3`;
* `tab`: an element with `role="tab"` or the class `tab`;
* `button`: a `button` or an element with the class `btn`;
* `text`: everything else.

A text used in several places takes the strictest kind (nav, title, tab, button, text).
Keys are stable: named for navigation rows, titles and tabs (the glossary map
`i18n/glossary-map.yaml` points at them), else derived from the English text.

    python -m app.i18n.export            write the export
    python -m app.i18n.export --check    fail when the export is not current
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from html.parser import HTMLParser
from pathlib import Path

from babel.messages.pofile import read_po

from app import shell

APP = Path(__file__).resolve().parent.parent
ROOT = APP.parent.parent
TEMPLATES = APP / "templates"
PO = APP / "locale" / "de" / "LC_MESSAGES" / "messages.po"
EXPORT = ROOT / "i18n" / "export"

KINDS = ("nav", "title", "tab", "button", "text")
_RANK = {kind: rank for rank, kind in enumerate(KINDS)}
_TITLE_TAGS = frozenset({"h1", "h2", "h3"})
_VOID = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "wbr"}
)

_TRANS = re.compile(r"\{%-?\s*trans\b[^%]*%\}(.*?)\{%-?\s*endtrans\s*-?%\}", re.DOTALL)
_CALL = re.compile(r"""\b_\(\s*(?:'((?:[^'\\]|\\.)*)'|"((?:[^"\\]|\\.)*)")\s*[,)]""")
_JINJA = re.compile(r"\{#.*?#\}|\{%.*?%\}|\{\{.*?\}\}", re.DOTALL)
_VAR = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
_PAGE_TITLE = re.compile(r"\{%-?\s*block\s+page_title\s*-?%\}(.*?)\{%-?\s*endblock", re.DOTALL)
_MARK = re.compile(r"\x01(\d+)\x02")
_SLUG = re.compile(r"[^a-z0-9]+")


def _msgid(body: str) -> str:
    """The message id of a `{% trans %}` body as `trimmed` extraction writes it."""
    text = _VAR.sub(lambda m: f"%({m.group(1)})s", body)
    return " ".join(text.split())


class _Kinds(HTMLParser):
    """Records the kind of the element around each marked text of a template."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[str] = []
        self.found: dict[int, str] = {}

    def _note(self, data: str, kind: str) -> None:
        for mark in _MARK.finditer(data):
            self.found[int(mark.group(1))] = kind

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        classes = set((values.get("class") or "").split())
        if values.get("role") == "tab" or "tab" in classes:
            kind = "tab"
        elif tag == "button" or "btn" in classes:
            kind = "button"
        elif tag in _TITLE_TAGS:
            kind = "title"
        else:
            kind = self.stack[-1] if self.stack else "text"
        for _name, value in attrs:
            if value:
                self._note(value, "text")
        if tag not in _VOID:
            self.stack.append(kind)

    def handle_endtag(self, tag: str) -> None:
        if tag not in _VOID and self.stack:
            self.stack.pop()

    def handle_data(self, data: str) -> None:
        self._note(data, self.stack[-1] if self.stack else "text")


def template_kinds(source: str) -> dict[str, str]:
    """Message id -> kind for the texts of one template."""
    ids: list[str] = []

    def mark(msgid: str) -> str:
        ids.append(msgid)
        return f"\x01{len(ids) - 1}\x02"

    titles = {
        _msgid(m.group(1))
        for block in _PAGE_TITLE.finditer(source)
        for m in _TRANS.finditer(block.group(1))
    }
    marked = _TRANS.sub(lambda m: mark(_msgid(m.group(1))), source)
    marked = _JINJA.sub(
        lambda m: "".join(mark(c.group(1) or c.group(2) or "") for c in _CALL.finditer(m.group(0))),
        marked,
    )
    parser = _Kinds()
    parser.feed(marked)
    parser.close()
    out: dict[str, str] = {msgid: "title" for msgid in titles}
    for index, kind in parser.found.items():
        msgid = ids[index]
        if msgid and (msgid not in out or _RANK[kind] < _RANK[out[msgid]]):
            out[msgid] = kind
    return out


def _slug(text: str) -> str:
    return _SLUG.sub("-", text.lower()).strip("-")


def named() -> dict[str, tuple[str, str]]:
    """English text -> (key, kind) for the navigation, titles and tabs."""
    out: dict[str, tuple[str, str]] = {}
    groups = shell.nav_groups(
        shell.NavFlags(admin=True, rag=True, knowledge_map=True, users=True, nodes=True),
        lambda text: text,
    )
    for heading, rows in groups:
        out.setdefault(heading, (f"nav.group.{_slug(heading)}", "nav"))
        for row in rows:
            out.setdefault(row.label, (f"nav.{_slug(row.href)}", "nav"))
    for msgid, kind in sorted(kinds_of_templates().items()):
        if kind in ("title", "tab") and msgid not in out:
            out[msgid] = (f"{kind}.{_slug(msgid)}", kind)
    return out


def kinds_of_templates() -> dict[str, str]:
    """Message id -> strictest kind over every template."""
    out: dict[str, str] = {}
    for path in sorted(TEMPLATES.rglob("*.html")):
        for msgid, kind in template_kinds(path.read_text(encoding="utf-8")).items():
            if _RANK[kind] < _RANK[out.get(msgid, "text")]:
                out[msgid] = kind
    return out


def key_of(text: str) -> str:
    return "text." + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _pairs(catalog_entries: Iterator[object]) -> Iterator[tuple[str, str, bool]]:
    """(English, German, fuzzy) for every message, plural forms one by one."""
    for message in catalog_entries:
        msgid = getattr(message, "id", None)
        string = getattr(message, "string", None)
        fuzzy = bool(getattr(message, "fuzzy", False))
        if not msgid:
            continue
        if isinstance(msgid, tuple | list):
            strings = list(string) if isinstance(string, tuple | list) else [string]
            for index, form in enumerate(msgid):
                german = strings[min(index, len(strings) - 1)] if strings else ""
                yield str(form), str(german or ""), fuzzy
        else:
            yield str(msgid), str(string or ""), fuzzy


def build() -> dict[str, list[dict[str, object]]]:
    with PO.open("rb") as handle:
        catalog = read_po(handle, locale="de")
    names = named()
    kinds = kinds_of_templates()
    out: dict[str, list[dict[str, object]]] = {"de": [], "en": []}
    seen: set[str] = set()
    for english, german, fuzzy in _pairs(iter(catalog)):
        key, kind = names.get(english, (key_of(english), kinds.get(english, "text")))
        if key in seen:
            continue
        seen.add(key)
        out["en"].append({"key": key, "text": english, "kind": kind})
        entry: dict[str, object] = {"key": key, "text": german, "kind": kind}
        if fuzzy:
            entry["fuzzy"] = True
        out["de"].append(entry)
    for entries in out.values():
        entries.sort(key=lambda e: str(e["key"]))
    return out


def render(entries: list[dict[str, object]]) -> str:
    """A JSON array with one entry per line, so a changed text is a one-line diff."""
    rows = [json.dumps(e, ensure_ascii=False, separators=(", ", ": ")) for e in entries]
    return "[\n" + ",\n".join(rows) + "\n]\n"


def stale(export: Mapping[str, list[dict[str, object]]], folder: Path = EXPORT) -> list[str]:
    """The export files that differ from `export`."""
    out = []
    for lang, entries in export.items():
        path = folder / f"{lang}.json"
        if not path.is_file() or path.read_text(encoding="utf-8") != render(entries):
            out.append(f"i18n/export/{lang}.json")
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m app.i18n.export", description=__doc__.splitlines()[0]
    )
    parser.add_argument("--check", action="store_true", help="fail when the export is not current")
    args = parser.parse_args(argv)
    export = build()
    if args.check:
        missing = stale(export)
        if missing:
            print(f"export: not current: {', '.join(missing)} (run python -m app.i18n.export)")
            return 1
        return 0
    EXPORT.mkdir(parents=True, exist_ok=True)
    for lang, entries in export.items():
        (EXPORT / f"{lang}.json").write_text(render(entries), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
