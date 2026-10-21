"""Choosing a language: the stored choice, the browser, the installation's default."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.i18n import SOURCE_LOCALE, get_catalogs, gettext, ngettext, use_locale
from app.i18n.catalog import Catalogs
from app.i18n.negotiate import negotiate, parse_accept_language, resolve


@pytest.mark.parametrize(
    ("header", "expected"),
    [
        (None, []),
        ("", []),
        ("de", ["de"]),
        ("de-DE,de;q=0.9,en;q=0.8", ["de-DE", "de", "en"]),
        ("en;q=0.5, de;q=0.9", ["de", "en"]),
        ("fr;q=0, de", ["de"]),
        ("*", []),
        ("de;q=oops, en", ["en"]),
        (",,;q=1,", []),
    ],
)
def test_accept_language_is_read_best_first(header: str | None, expected: list[str]) -> None:
    assert parse_accept_language(header) == expected


def test_an_absurdly_long_header_is_cut_off_and_never_raises() -> None:
    header = "x-" + "a" * 5000
    assert parse_accept_language(header) == [header[:1024]]


def test_the_shipped_languages_are_discovered() -> None:
    codes = get_catalogs().codes()

    assert codes[0] == SOURCE_LOCALE
    assert "de" in codes


@pytest.mark.parametrize(
    ("tag", "expected"),
    [("de", "de"), ("DE", "de"), ("de-AT", "de"), ("de_CH", "de"), ("en-GB", "en"), ("tlh", None)],
)
def test_a_regional_tag_finds_its_language(tag: str, expected: str | None) -> None:
    assert get_catalogs().match(tag) == expected


def test_the_browser_decides_when_nothing_is_stored() -> None:
    state = resolve(
        stored=None, accept_language="de-DE,en;q=0.5", default="en", catalogs=get_catalogs()
    )

    assert state.locale == "de"
    assert state.stored is None
    assert state.auto == "de"


def test_a_stored_choice_beats_the_browser() -> None:
    state = resolve(
        stored="en", accept_language="de", default="de", catalogs=get_catalogs()
    )

    assert state.locale == "en"
    assert state.stored == "en"
    assert state.auto == "de"


def test_the_installation_default_applies_when_the_browser_asks_for_nothing_shipped() -> None:
    state = resolve(stored=None, accept_language="tlh,tok", default="de", catalogs=get_catalogs())

    assert state.locale == "de"


def test_an_unknown_default_is_english() -> None:
    state = resolve(stored=None, accept_language=None, default="klingon", catalogs=get_catalogs())

    assert state.locale == SOURCE_LOCALE


def test_a_stored_language_that_is_no_longer_shipped_is_ignored() -> None:
    state = resolve(stored="xx", accept_language="de", default="en", catalogs=get_catalogs())

    assert state.locale == "de"
    assert state.stored is None


def test_negotiation_returns_none_without_a_match() -> None:
    assert negotiate("tlh,tok;q=0.8", get_catalogs()) is None


def test_texts_follow_the_language_of_the_block_and_fall_back_to_the_source() -> None:
    assert gettext("Sign out") == "Sign out"
    with use_locale("de"):
        assert gettext("Sign out") == "Abmelden"
        assert gettext("A text that has no translation anywhere") == (
            "A text that has no translation anywhere"
        )
        assert ngettext("%(count)d day", "%(count)d days", 1) % {"count": 1} == "1 Tag"
        assert ngettext("%(count)d day", "%(count)d days", 5) % {"count": 5} == "5 Tage"
    assert gettext("Sign out") == "Sign out"


def test_a_language_is_a_directory_and_nothing_else(tmp_path: Path) -> None:
    folder = tmp_path / "fr" / "LC_MESSAGES"
    folder.mkdir(parents=True)
    (folder / "messages.po").write_text(
        'msgid ""\nmsgstr ""\n"Content-Type: text/plain; charset=utf-8\\n"\n'
        '"Plural-Forms: nplurals=2; plural=(n > 1);\\n"\n\n'
        'msgid "Sign out"\nmsgstr "Se déconnecter"\n',
        encoding="utf-8",
    )
    (tmp_path / "not-a-language" / "LC_MESSAGES").mkdir(parents=True)
    (tmp_path / "not-a-language" / "LC_MESSAGES" / "messages.po").write_text("", encoding="utf-8")
    (tmp_path / "pt_BR").mkdir()

    catalogs = Catalogs(tmp_path)

    assert catalogs.codes() == [SOURCE_LOCALE, "fr"]
    assert {info.code: info.name for info in catalogs.locales}["fr"] == "Français"
    assert catalogs.translations("fr").gettext("Sign out") == "Se déconnecter"


def test_a_catalogue_that_cannot_be_read_leaves_its_language_out(tmp_path: Path) -> None:
    folder = tmp_path / "fr" / "LC_MESSAGES"
    folder.mkdir(parents=True)
    (folder / "messages.po").write_bytes(b"\xff\xfe not a catalogue \x00")

    assert Catalogs(tmp_path).codes() == [SOURCE_LOCALE]
