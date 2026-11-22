"""The platform's shared frame around every page of the manager (shell contract of papaia-kit).

The manager renders no frame of its own any more: `templates/base.html` calls the kit's
Jinja2 macro `shell` from `pk/shell.html`, and each page is the content of `main`. The input
of the frame is a `papaia.shell-input/1` document built here and checked by the kit's
`parse_shell_input()` (schema, own links only, catalog reader with content hashes). Module
names, symbols, the switcher and the fixed texts come from the platform catalog and the kit,
so the frame reads and looks the same in every module.

The frame is display only. A module the switcher leaves out, or a navigation row an account
does not see, grants or withholds nothing: every route checks the roles itself on every
request.

`python -m app.shell --input <file>` prints the document for one shell input as the manager
renders it (the module command `render-shell` of the markup contract, K3).
"""
from __future__ import annotations

import argparse
import logging
import re
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from markupsafe import Markup
from papaia_kit.catalog import Catalog
from papaia_kit.ui import ShellInput, ShellInputError, parse_shell_input, render_shell
from papaia_kit.ui.glossary import load_glossary
from papaia_kit.ui.shell import INPUT_FORMAT, MAX_INPUT_BYTES

log = logging.getLogger(__name__)

SURFACE_ID = "manager"
ICON = "manager"
THEMES = ("light", "dark", "system")
SIDEBARS = ("pinned", "collapsed")
LANGUAGE_SETTINGS = ("browser", "de", "en")
APPEARANCES = ("light", "dark")
# Where the frame keeps what the footer forms chose: the sidebar in a cookie (shell
# contract 7), the appearance in the signed session until the preference service holds it.
SIDEBAR_COOKIE = "pk_sidebar"
APPEARANCE_KEY = "pk_appearance"
MAX_LABEL = 200
MAX_BRAND = 60
MAX_ROLES = 256
MAX_ROLE = 128
MAX_CRUMBS = 8
_CONTROL = re.compile(r"[\x00-\x1f\x7f]+")
_SPACE = re.compile(r"\s+")


@dataclass(frozen=True)
class NavRow:
    """One row of the manager's navigation; `prefixes` are the paths it is current for."""

    href: str
    label: str
    prefixes: tuple[str, ...]


@dataclass(frozen=True)
class NavFlags:
    """What decides which rows an account sees. Presentation only; the routes check roles."""

    admin: bool = False
    rag: bool = False
    knowledge_map: bool = False
    users: bool = False
    nodes: bool = False


def own_module() -> dict[str, Any]:
    """The manager as the frame names it; the display names are the glossary's."""
    titles = load_glossary().fixed.modules[SURFACE_ID]
    return {"id": SURFACE_ID, "title": {"de": titles["de"], "en": titles["en"]}, "icon": ICON}


def label(text: object, fallback: str, limit: int = MAX_LABEL) -> str:
    """A text of the frame: one line, at most `limit` characters, never empty."""
    cleaned = _SPACE.sub(" ", _CONTROL.sub(" ", str(text or ""))).strip()[:limit].strip()
    return cleaned or fallback


def roles_of(roles: object) -> list[str]:
    """The account's roles for the switcher. Anything but a short plain string is dropped."""
    if not isinstance(roles, list | tuple):
        return []
    out: list[str] = []
    for role in roles:
        if (
            isinstance(role, str)
            and 0 < len(role) <= MAX_ROLE
            and not _CONTROL.search(role)
            and role not in out
        ):
            out.append(role)
    return out[:MAX_ROLES]


def frame_language(locale: str) -> str:
    """The frame speaks German or English; every other interface language reads English."""
    return "de" if locale.split("_")[0].split("-")[0].lower() == "de" else "en"


def language_setting(stored: str | None) -> str:
    """The stored language as the account menu marks it; no choice is `browser`."""
    if stored is None:
        return "browser"
    lang = frame_language(stored)
    return lang if lang == stored.split("_")[0].lower() else "browser"


def own_path(href: str) -> str | None:
    """A breadcrumb or navigation target as an own path the contract accepts, else None."""
    if not href.startswith("/") or href.startswith("//") or "\\" in href:
        return None
    path = quote(href, safe="/?=&%-._~")
    return path if len(path) <= 2048 else None


def nav_groups(flags: NavFlags, gettext: Callable[[str], str]) -> list[tuple[str, list[NavRow]]]:
    """The manager's navigation after the fixed first row (the dashboard is the overview).

    The same groups as before the shared frame, split by what an entry acts on: what is
    running now (Monitor), what extends the installation (Extensions), the RAG system and the
    knowledge map when the core runs them, and what concerns the deployment as a whole
    (System). An account without the admin role sees the overview only.
    """
    _ = gettext
    if not flags.admin:
        return []
    groups: list[tuple[str, list[NavRow]]] = [
        (
            _("Monitor"),
            [
                NavRow("/services", _("Services"), ("/services",)),
                NavRow("/host", _("Host"), ("/host",)),
                NavRow("/models", _("Models & Usage"), ("/models",)),
                NavRow("/jobs", _("Jobs"), ("/jobs",)),
            ],
        ),
        (
            _("Extensions"),
            [
                NavRow("/addons", _("Add-Ons"), ("/addons", "/modules")),
                NavRow("/catalogs", _("Catalogs"), ("/catalogs",)),
            ],
        ),
    ]
    if flags.rag:
        groups.append(
            (
                "RAG",
                [
                    NavRow("/connections", _("Connections"), ("/connections",)),
                    NavRow("/collections", _("Collections"), ("/collections",)),
                    NavRow("/embedding", _("Embedding"), ("/embedding",)),
                    # Jobs, runs, credentials, leftovers and the catalog file are the tabs
                    # of one section under /ingest/.
                    NavRow("/ingest/jobs", _("Ingest Jobs"), ("/ingest/",)),
                ],
            )
        )
    if flags.knowledge_map:
        groups.append(
            (_("Knowledge"), [NavRow("/knowledge-map", _("AI Map"), ("/knowledge-map",))])
        )
    system = [
        NavRow("/backup", _("Backup / Restore"), ("/backup",)),
        NavRow("/upgrade", _("Upgrade"), ("/upgrade",)),
    ]
    if flags.users:
        system.append(NavRow("/users", _("Users"), ("/users",)))
    system.append(NavRow("/mail", _("Mail"), ("/mail",)))
    if flags.nodes:
        system.append(NavRow("/nodes", _("Nodes"), ("/nodes",)))
    system += [
        NavRow("/audit", _("Audit log"), ("/audit",)),
        NavRow("/settings", _("Settings"), ("/settings",)),
    ]
    groups.append((_("System"), system))
    return groups


def current_row(path: str, groups: Sequence[tuple[str, Sequence[NavRow]]]) -> str:
    """The row a path belongs to: the longest matching prefix, else the overview."""
    best, length = "/", 0
    for _heading, rows in groups:
        for row in rows:
            for prefix in row.prefixes:
                hit = path == prefix or path.startswith(prefix.rstrip("/") + "/")
                if hit and len(prefix) > length:
                    best, length = row.href, len(prefix)
    return best


def plain(value: Any) -> Any:
    """A catalog read by the kit (read-only mappings, tuples) as plain JSON data."""
    if isinstance(value, Mapping):
        return {str(k): plain(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [plain(v) for v in value]
    return value


def document(
    *,
    catalog: Catalog | None,
    installation: str,
    suffix: str,
    person: str,
    roles: Sequence[str],
    lang: str,
    language: str,
    theme: str,
    sidebar: str,
    csrf: str,
    title: str,
    breadcrumb: Sequence[Mapping[str, str]],
    path: str,
    groups: Sequence[tuple[str, Sequence[NavRow]]],
) -> dict[str, Any]:
    """A `papaia.shell-input/1` document for one page of the manager."""
    crumbs: list[dict[str, str]] = []
    for crumb in list(breadcrumb)[:MAX_CRUMBS]:
        entry = {"label": label(crumb.get("label"), "…")}
        href = own_path(str(crumb.get("href") or ""))
        if href is not None:
            entry["href"] = href
        crumbs.append(entry)
    page: dict[str, Any] = {"title": label(title, installation)}
    if crumbs:
        page["breadcrumb"] = crumbs
    doc: dict[str, Any] = {
        "format": INPUT_FORMAT,
        "module": own_module(),
        "catalog": plain(catalog.data) if catalog is not None else None,
        "person": {"display_name": label(person, "?"), "roles": roles_of(list(roles))},
        "locale": lang if lang in ("de", "en") else "en",
        "language_setting": language if language in LANGUAGE_SETTINGS else "browser",
        "theme": theme if theme in THEMES else "system",
        "sidebar": sidebar if sidebar in SIDEBARS else "pinned",
        "csrf_token": csrf,
        "page": page,
        "nav": {
            "current": current_row(path, groups),
            "groups": [
                {"label": heading, "items": [{"label": r.label, "href": r.href} for r in rows]}
                for heading, rows in groups
                if rows
            ],
        },
    }
    if catalog is None or catalog.branding is None:
        branding = {"name": label(installation, "papAIa", MAX_BRAND)}
        if suffix.strip():
            branding["suffix"] = label(suffix, "", MAX_BRAND)
        doc["branding"] = branding
    return doc


def checked(doc: dict[str, Any]) -> ShellInput:
    """The checked input. A catalog the shell refuses is left out, never half used."""
    try:
        return parse_shell_input(doc)
    except ShellInputError as exc:
        if doc.get("catalog") is None or "branding" not in doc:
            raise
        log.warning("shell input refused with the catalog: %s", "; ".join(exc.problems[:5]))
    return parse_shell_input({**doc, "catalog": None})


def render_input(doc: Mapping[str, Any], content: Markup | str = "") -> str:
    """K3: render a reference input as the manager, the way every page is rendered."""
    return render_shell(parse_shell_input({**doc, "module": own_module()}), content)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="render-shell", description=__doc__.splitlines()[0])
    parser.add_argument("--input", type=Path, required=True, help="papaia.shell-input/1 file")
    args = parser.parse_args(argv)
    try:
        with args.input.open("rb") as handle:
            raw = handle.read(MAX_INPUT_BYTES + 1)
        if len(raw) > MAX_INPUT_BYTES:
            raise ValueError("input too large")
        # The kit decodes the bytes itself: duplicate members and non-JSON constants are
        # refused there, which json.loads would let through.
        parsed = parse_shell_input(raw)
        sys.stdout.write(render_input(parsed.document))
    except ShellInputError as exc:
        for problem in exc.problems:
            print(f"  {problem}", file=sys.stderr)
        print("shell input rejected", file=sys.stderr)
        return 1
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
