"""Every language that ships a catalogue translates every text of the interface, correctly.

The texts are extracted from the source in memory with the same `babel.cfg` that
`pybabel extract` uses, so what the command would find is what is demanded here. A language
is a directory under `src/app/locale/`; the tests below run once for each of them (the source
language excepted), so a language added later is checked without touching this file.

When this fails after a change to the interface: extract the texts again, update the
catalogues (`pybabel update`) and translate what is new. CONTRIBUTING.md has the commands.
"""
from __future__ import annotations

import re
from collections.abc import Iterator
from functools import cache
from pathlib import Path

import pytest
from babel.messages.catalog import Catalog, Message
from babel.messages.extract import extract_from_dir
from babel.messages.frontend import parse_mapping
from babel.messages.pofile import read_po

from app.i18n import SOURCE_LOCALE
from app.i18n.catalog import LOCALE_DIR, shipped_codes

SRC = Path(__file__).resolve().parents[1] / "src"

_PLACEHOLDER = re.compile(
    r"%\([A-Za-z_][A-Za-z0-9_]*\)[#0\- +]*\d*(?:\.\d+)?[sdfrxa]|\{[A-Za-z_][A-Za-z0-9_]*\}"
)
_TAG = re.compile(r"</?([A-Za-z][A-Za-z0-9]*)")

LANGUAGES = [code for code in shipped_codes() if code != SOURCE_LOCALE]


@cache
def _extracted() -> list[tuple[str, int, str | tuple[str, ...], str | None]]:
    """What `pybabel extract` finds: (file, line, message, context), one per use."""
    with (SRC / "babel.cfg").open(encoding="utf-8") as handle:
        method_map, options_map = parse_mapping(handle)
    return [
        (file, lineno, message, context)
        for file, lineno, message, _comments, context in extract_from_dir(
            str(SRC / "app"), method_map, options_map
        )
    ]


@cache
def _source_texts() -> dict[tuple[str | None, str | tuple[str, ...]], Message]:
    """Every text the source marks for translation, keyed the way a catalogue keys it."""
    template = Catalog()
    for _file, lineno, message, context in _extracted():
        template.add(message, context=context, lineno=lineno)
    found: dict[tuple[str | None, str | tuple[str, ...]], Message] = {}
    for entry in template:
        if entry.id:
            found[(entry.context, _key(entry.id))] = entry
    return found


def _key(message_id: str | tuple[str, ...]) -> str | tuple[str, ...]:
    return tuple(message_id) if isinstance(message_id, tuple) else message_id


@cache
def _catalogue(code: str) -> Catalog:
    path = LOCALE_DIR / code / "LC_MESSAGES" / "messages.po"
    with path.open("rb") as handle:
        return read_po(handle, locale=code, ignore_obsolete=False)


def _forms(value: str | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(value) if isinstance(value, tuple) else (value,)


def _name(message: Message) -> str:
    return repr(_forms(message.id)[0][:70])


def _translated(catalogue: Catalog) -> Iterator[tuple[Message, Message]]:
    """The texts that have a translation; those that have none are reported on their own."""
    for (context, _), source in _source_texts().items():
        found = catalogue.get(source.id, context)
        if found is not None and any(_forms(found.string)):
            yield source, found


def test_the_source_actually_has_texts_to_translate() -> None:
    # An extraction that silently found nothing would make every check below pass.
    assert len(_source_texts()) > 1500


def test_no_text_is_used_both_as_a_singular_and_as_a_plural() -> None:
    """A catalogue keys a plural by its singular, so `gettext("day")` next to
    `ngettext("day", "days", n)` would be one entry, and one of the two would be wrong in every
    language. Give one of them a context (`npgettext`)."""
    uses: dict[tuple[str | None, str], dict[object, list[str]]] = {}
    for file, lineno, message, context in _extracted():
        singular = message[0] if isinstance(message, tuple) else message
        shape = tuple(message) if isinstance(message, tuple) else "singular"
        uses.setdefault((context, singular), {}).setdefault(shape, []).append(f"{file}:{lineno}")

    clashes = {key: places for key, places in uses.items() if len(places) > 1}

    assert not clashes, f"the same text is used in different shapes: {list(clashes.items())[:5]}"


def test_there_is_a_language_besides_the_source() -> None:
    assert "de" in LANGUAGES


@pytest.mark.parametrize("code", LANGUAGES)
def test_the_header_names_the_language_and_how_its_plurals_work(code: str) -> None:
    catalogue = _catalogue(code)

    assert str(catalogue.locale).replace("_", "-").split("-")[0] == code.split("_")[0].lower()
    forms = catalogue.mime_headers and dict(catalogue.mime_headers).get("Plural-Forms")
    assert forms, f"{code}: the header has no Plural-Forms"
    assert catalogue.num_plurals >= 1


@pytest.mark.parametrize("code", LANGUAGES)
def test_every_text_is_translated(code: str) -> None:
    catalogue = _catalogue(code)

    missing = [
        _name(source)
        for (context, _), source in _source_texts().items()
        if (found := catalogue.get(source.id, context)) is None
        or not any(_forms(found.string))
    ]

    assert not missing, f"{code}: {len(missing)} texts have no translation, e.g. {missing[:8]}"


@pytest.mark.parametrize("code", LANGUAGES)
def test_a_plural_has_a_form_for_every_number_the_language_distinguishes(code: str) -> None:
    catalogue = _catalogue(code)

    wrong = []
    for source, found in _translated(catalogue):
        if isinstance(source.id, tuple):
            forms = _forms(found.string)
            if len(forms) != catalogue.num_plurals or not all(forms):
                wrong.append((_name(source), len(forms)))

    assert not wrong, f"{code}: expected {catalogue.num_plurals} forms: {wrong[:8]}"


@pytest.mark.parametrize("code", LANGUAGES)
def test_no_translation_is_left_marked_as_a_guess(code: str) -> None:
    fuzzy = [_name(found) for found in _catalogue(code) if found.id and found.fuzzy]

    assert not fuzzy, f"{code}: fuzzy entries must be reviewed or removed: {fuzzy[:8]}"


@pytest.mark.parametrize("code", LANGUAGES)
def test_the_catalogue_holds_no_text_the_source_no_longer_has(code: str) -> None:
    catalogue = _catalogue(code)
    source = _source_texts()

    stale = [
        _name(found)
        for found in catalogue
        if found.id and (found.context, _key(found.id)) not in source
    ]
    obsolete = [_name(found) for found in catalogue.obsolete.values()]

    assert not stale, f"{code}: texts no longer in the source: {stale[:8]}"
    assert not obsolete, f"{code}: obsolete entries (#~) to drop: {obsolete[:8]}"


@pytest.mark.parametrize("code", LANGUAGES)
def test_placeholders_survive_translation(code: str) -> None:
    """A dropped or invented placeholder is a KeyError or a hole in a sentence at run time."""
    problems = []
    for source, found in _translated(_catalogue(code)):
        wanted: set[str] = set()
        for form in _forms(source.id):
            wanted |= set(_PLACEHOLDER.findall(form))
        got = [set(_PLACEHOLDER.findall(form)) for form in _forms(found.string)]
        if isinstance(source.id, tuple):
            # A form for a single item may leave the number out ("one day"); another form of the
            # same text has to carry every placeholder, and none may invent one.
            ok = all(form <= wanted for form in got) and any(form == wanted for form in got)
        else:
            ok = got[0] == wanted
        if not ok:
            problems.append((_name(source), sorted(wanted), [sorted(form) for form in got]))

    assert not problems, f"{code}: placeholders differ: {problems[:6]}"


@pytest.mark.parametrize("code", LANGUAGES)
def test_markup_in_a_text_is_kept(code: str) -> None:
    problems = []
    for source, found in _translated(_catalogue(code)):
        wanted = sorted({tag.lower() for form in _forms(source.id) for tag in _TAG.findall(form)})
        for form in _forms(found.string):
            got = sorted({tag.lower() for tag in _TAG.findall(form)})
            if got != wanted:
                problems.append((_name(source), wanted, got))

    assert not problems, f"{code}: markup differs: {problems[:6]}"


@pytest.mark.parametrize("code", LANGUAGES)
def test_babel_finds_nothing_wrong_with_the_format_strings(code: str) -> None:
    problems = [
        (_name(message), [str(error) for error in errors])
        for message, errors in _catalogue(code).check()
    ]

    assert not problems, f"{code}: {problems[:6]}"


@pytest.mark.parametrize("code", LANGUAGES)
def test_the_application_loads_the_catalogue_it_ships(code: str) -> None:
    """The catalogue compiles at start-up and the picker can name the language."""
    from app.i18n import get_catalogs

    catalogs = get_catalogs()
    assert code in catalogs.codes()
    names = {info.code: info.name for info in catalogs.locales}
    assert names[code]
    assert names[code] != code
