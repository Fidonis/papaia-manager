"""The shipped languages: one gettext catalogue per directory under `app/locale/`.

A language is a directory `locale/<code>/LC_MESSAGES/messages.po` and nothing else. The `.po`
files are read when the process starts and compiled to an in-memory `.mo`, so there is no
build step and nothing binary in the repository. English is the language the source is
written in: its message ids are the texts, it needs no catalogue and is always offered.
"""
from __future__ import annotations

import gettext
import io
import logging
import re
import threading
from dataclasses import dataclass
from pathlib import Path

from babel import Locale
from babel.messages.mofile import write_mo
from babel.messages.pofile import read_po

logger = logging.getLogger(__name__)

# Resolved from this module rather than the working directory, for the same reason as the
# static directory in `app.templating`: it is read long after whatever cwd the process
# started in.
LOCALE_DIR = Path(__file__).resolve().parent.parent / "locale"

SOURCE_LOCALE = "en"

# `de`, `pt_BR`. A directory with any other name is not a language.
_CODE_RE = re.compile(r"^[a-z]{2,3}(_[A-Z]{2})?$")


@dataclass(frozen=True, slots=True)
class LocaleInfo:
    """A language the interface is offered in."""

    code: str
    # The language's name in that language ("Deutsch"), for the picker.
    name: str


def _display_name(code: str) -> str:
    try:
        name = Locale.parse(code).get_display_name(code) or code
    except Exception:  # noqa: BLE001 -- an unknown code must not take the page down
        return code
    return name[:1].upper() + name[1:]


def catalog_path(code: str, locale_dir: Path = LOCALE_DIR) -> Path:
    return locale_dir / code / "LC_MESSAGES" / "messages.po"


def shipped_codes(locale_dir: Path = LOCALE_DIR) -> list[str]:
    """The codes of the catalogues on disk, without the source language."""
    try:
        entries = sorted(locale_dir.iterdir())
    except OSError:
        return []
    return [
        entry.name
        for entry in entries
        if _CODE_RE.match(entry.name)
        and entry.name != SOURCE_LOCALE
        and catalog_path(entry.name, locale_dir).is_file()
    ]


def _compile(code: str, path: Path) -> gettext.NullTranslations | None:
    """Read one `.po` and compile it in memory; None when it cannot be read."""
    try:
        with path.open("rb") as handle:
            catalog = read_po(handle, locale=code)
        mo = io.BytesIO()
        # Fuzzy entries are guesses of the extraction tool, not translations.
        write_mo(mo, catalog, use_fuzzy=False)
        mo.seek(0)
        return gettext.GNUTranslations(mo)
    except Exception as exc:  # noqa: BLE001 -- a broken catalogue must not stop the manager
        logger.warning("catalogue %s unreadable, the language is left out: %s", path, exc)
        return None


class Catalogs:
    """The loaded translations, by language code."""

    def __init__(self, locale_dir: Path = LOCALE_DIR) -> None:
        self._source = gettext.NullTranslations()
        self._by_code: dict[str, gettext.NullTranslations] = {}
        for code in shipped_codes(locale_dir):
            loaded = _compile(code, catalog_path(code, locale_dir))
            if loaded is not None:
                self._by_code[code] = loaded
        self.locales: list[LocaleInfo] = [
            LocaleInfo(SOURCE_LOCALE, _display_name(SOURCE_LOCALE)),
            *(LocaleInfo(code, _display_name(code)) for code in self._by_code),
        ]

    def codes(self) -> list[str]:
        return [info.code for info in self.locales]

    def translations(self, code: str) -> gettext.NullTranslations:
        return self._by_code.get(code, self._source)

    def match(self, tag: str) -> str | None:
        """The shipped language a tag asks for, or None.

        `de-AT` and `de_AT` find `de` when there is no catalogue of their own, so a browser
        that asks for a regional variant still gets the language.
        """
        normalised = tag.strip().replace("-", "_")
        if not normalised:
            return None
        lowered = {code.lower(): code for code in self.codes()}
        if normalised.lower() in lowered:
            return lowered[normalised.lower()]
        base = normalised.split("_", 1)[0].lower()
        return lowered.get(base)


_lock = threading.Lock()
_catalogs: Catalogs | None = None


def get_catalogs() -> Catalogs:
    """The process-wide catalogues, loaded on first use."""
    global _catalogs  # noqa: PLW0603
    if _catalogs is None:
        with _lock:
            if _catalogs is None:
                _catalogs = Catalogs()
    return _catalogs


def reload_catalogs(locale_dir: Path = LOCALE_DIR) -> Catalogs:
    """Load the catalogues again from `locale_dir`; used by tests."""
    global _catalogs  # noqa: PLW0603
    with _lock:
        _catalogs = Catalogs(locale_dir)
        return _catalogs
