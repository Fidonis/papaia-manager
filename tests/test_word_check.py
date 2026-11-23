"""The platform's word check over the manager's texts (PLT-27-A2…A5, interface spec 4.3).

`python -m app.i18n.export` writes every interface text as `i18n/export/{de,en}.json` with its
kind (navigation, title, tab, button, text); `python -m app.i18n.wordcheck` runs the kit's
word check (`papaia_kit.ui.words.check_module`) over that export and the templates. The
mandatory places of the glossary are mapped in `i18n/glossary-map.yaml`; findings that stay
for a reason are listed in `i18n/words-known.yaml`, which may only shrink.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from papaia_kit.ui.words import check_module

from app.i18n import export, wordcheck

ROOT = Path(__file__).resolve().parent.parent


def _export(lang: str) -> dict[str, dict[str, str]]:
    entries = json.loads((ROOT / "i18n" / "export" / f"{lang}.json").read_text(encoding="utf-8"))
    return {e["key"]: e for e in entries}


def test_the_export_is_current() -> None:
    assert export.stale(export.build()) == []


def test_template_kinds_follow_the_element_around_the_text() -> None:
    source = (
        '{% block page_title %}{% trans %}Backup / Restore{% endtrans %}{% endblock %}'
        '<h2 class="x">{% trans %}History{% endtrans %}</h2>'
        '<a href="/a" class="tab whitespace-nowrap">{% trans %}Core features{% endtrans %}</a>'
        '<button type="button" role="tab">{{ _("Files") }}</button>'
        '<a class="btn btn-sm" href="/">{% trans %}Back to overview{% endtrans %}</a>'
        '<button>{% trans name=n %}Remove {{ name }}{% endtrans %}</button>'
        '<p title="{{ _(\'A hint\') }}">{% trans %}A sentence.{% endtrans %}</p>'
    )
    assert export.template_kinds(source) == {
        "Backup / Restore": "title",
        "History": "title",
        "Core features": "tab",
        "Files": "tab",
        "Back to overview": "button",
        "Remove %(name)s": "button",
        "A hint": "text",
        "A sentence.": "text",
    }


def test_labels_of_the_kit_button_macro_are_buttons() -> None:
    """A page built from the kit's macros (pk/components.html) names its buttons in calls."""
    source = (
        '{% import "pk/components.html" as kit with context %}'
        '{{ kit.button(_("Export (CSV)"), href="/x", symbol="download") }}'
        '{{ kit.field("select", "flag", _("Flag"), options=o) }}'
        '<p>{{ kit.lead(_("A lead.")) }}</p>'
    )
    assert export.template_kinds(source) == {
        "Export (CSV)": "button",
        "Flag": "text",
        "A lead.": "text",
    }


def test_the_review_group_of_auditors_has_named_navigation_keys() -> None:
    en, de = _export("en"), _export("de")
    assert en["nav.group.review"] == {"key": "nav.group.review", "text": "Review", "kind": "nav"}
    assert de["nav.group.review"]["text"] == "Prüfung"
    assert en["nav.inventory"] == {"key": "nav.inventory", "text": "Inventory", "kind": "nav"}
    assert de["nav.inventory"]["text"] == "Bestand"


def test_navigation_rows_and_page_titles_have_named_keys() -> None:
    en, de = _export("en"), _export("de")
    assert en["nav.modules"] == {"key": "nav.modules", "text": "Modules", "kind": "nav"}
    assert de["nav.modules"]["text"] == "Module"
    assert en["nav.settings"]["kind"] == "nav"
    assert en["title.my-area"] == {"key": "title.my-area", "text": "My area", "kind": "title"}
    assert set(en) == set(de)


def test_the_known_hits_of_the_specification_are_gone() -> None:
    """Interface spec 4.3: Dashboard, the tab Module, Verbindungen, Modulkatalog."""
    de, en = _export("de"), _export("en")
    labels = [e for e in de.values() if e["kind"] in ("nav", "title", "tab", "button")]
    for entry in labels:
        assert "Dashboard" not in entry["text"], entry
        assert "Modulkatalog" not in entry["text"], entry
        assert "Verbindungen" not in entry["text"], entry
    assert de["nav.connections"]["text"] == "Vektordatenbanken"
    assert en["nav.connections"]["text"] == "Vector databases"
    assert de["title.overview"]["text"] == "Übersicht"
    assert de["tab.core-features"]["text"] == "Kernfunktionen"
    assert en["tab.core-features"]["text"] == "Core features"
    assert "title.module-catalog" not in de
    assert "title.dashboard" not in de


def test_mandatory_places_and_restricted_words_have_no_finding() -> None:
    """W2 (mandatory places), W3 (restricted words in labels), W4 (completeness): zero."""
    findings = check_module(ROOT, sources=("ui",))
    strict = [f for f in findings if f.check in ("W2", "W3", "W4") and f.severity == "error"]
    assert strict == []


def test_the_glossary_map_names_every_mandatory_place() -> None:
    data = yaml.safe_load((ROOT / "i18n" / "glossary-map.yaml").read_text(encoding="utf-8"))
    assert data["module"] == "manager"
    assert data["map"]["uebersicht"] == "title.overview"
    assert data["map"]["einstellungen"] == "nav.settings"
    assert data["map"]["audit_log"] == "nav.audit"
    assert data["map"]["mein_bereich"] == "title.my-area"


def test_the_word_check_has_no_new_and_no_stale_finding(capsys: pytest.CaptureFixture[str]) -> None:
    assert wordcheck.main() == 0, capsys.readouterr().out


def test_the_known_list_never_holds_a_strict_finding() -> None:
    for item in wordcheck.known():
        assert item.split(" ", 1)[0] in ("W1", "W5"), item


def test_the_known_list_refuses_a_group_without_ticket(tmp_path: Path) -> None:
    path = tmp_path / "known.yaml"
    path.write_text(
        "version: 1\ngroups:\n- reason: r\n  findings: [W1 a b]\n", encoding="utf-8"
    )
    with pytest.raises(SystemExit):
        wordcheck.known(path)
    path.write_text(
        "version: 1\ngroups:\n- ticket: MGR-004\n  reason: r\n  findings: [W1 a b, W1 a b]\n",
        encoding="utf-8",
    )
    with pytest.raises(SystemExit):
        wordcheck.known(path)


def test_a_new_finding_fails_the_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = tmp_path / "known.yaml"
    empty.write_text("version: 1\ngroups: []\n", encoding="utf-8")
    monkeypatch.setattr(wordcheck, "KNOWN", empty)
    assert wordcheck.main() == 1
