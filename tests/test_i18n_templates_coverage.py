"""No template shows English text that was not marked for translation.

This is a lint, not a proof: `i18n_scan` reads a template as text and tags and reports what a
reader would see in English outside `{% trans %}` and `_()`. It stands in for the question
"did anybody miss a text?" that the catalogue test cannot ask, because a text that is not
marked is not in the catalogue to begin with.

A word that is not English prose (a product, a unit, a protocol) goes in `ALLOWED_WORDS` in
`i18n_scan.py`; anything else that shows up here is a text to mark with `{% trans %}` or `_()`.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.templating import templates
from tests import i18n_scan

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "src" / "app" / "templates"
TEMPLATES = sorted(TEMPLATES_DIR.rglob("*.html"))


def _name(path: Path) -> str:
    return path.relative_to(TEMPLATES_DIR).as_posix()


def test_the_inventory_covers_the_templates() -> None:
    assert len(TEMPLATES) > 80


@pytest.mark.parametrize("path", TEMPLATES, ids=_name)
def test_no_text_is_left_untranslated(path: Path) -> None:
    findings = i18n_scan.scan(path.read_text(encoding="utf-8"))

    shown = [f"{_name(path)}:{f.line} [{f.kind}] {f.text[:100]}" for f in findings]
    assert not shown, "not marked for translation:\n" + "\n".join(shown)


@pytest.mark.parametrize("path", TEMPLATES, ids=_name)
def test_the_template_compiles(path: Path) -> None:
    """A malformed {% trans %} block is a syntax error, found here instead of on first use."""
    templates.env.get_template(_name(path))


# The scan itself: it has to flag the plain cases and leave the marked ones alone.


@pytest.mark.parametrize(
    ("source", "kind"),
    [
        ("<p>Save the settings</p>", "text"),
        ("<p>Save <strong>all</strong> of it</p>", "text"),
        ('<button title="Remove this job">x</button>', "attr"),
        ('<input type="text" placeholder="Search the catalogue">', "attr"),
        ('<input type="submit" value="Save changes">', "attr"),
        ('<div x-text="ok ? \'Everything works\' : \'Something broke\'"></div>', "alpine"),
        ("<script>toast('The job was saved');</script>", "script"),
        ("{{ 'Saved successfully' }}", "expr"),
        ("<p>{{ n }} {{ 'job' if n == 1 else 'jobs' }}</p>", "expr"),
        ("<p>{{ state or 'unknown' }}</p>", "expr"),
        ("<p>{{ n }} {{ 'day' if n == 1 else 'days' }}</p>", "expr"),
        ("{% trans w=('day' if n == 1 else 'days') %}{{ w }} ago{% endtrans %}", "expr"),
    ],
)
def test_the_scan_sees_english_text_that_is_not_marked(source: str, kind: str) -> None:
    assert {f.kind for f in i18n_scan.scan(source)} == {kind}


@pytest.mark.parametrize(
    "source",
    [
        "<p>{% trans %}Save the settings{% endtrans %}</p>",
        "<p>{% trans count=n %}{{ count }} job{% pluralize %}{{ count }} jobs{% endtrans %}</p>",
        "<button title=\"{{ _('Remove this job') }}\">x</button>",
        '<div x-text="ok ? ' + "{{ _('Everything works')|tojson|forceescape }}" + ' : 1"></div>',
        "<script>toast({{ _('The job was saved')|tojson }});</script>",
        "<p>{{ job.name }}</p>",
        "<p>Qdrant</p>",
        "<code>docker compose up</code>",
        '<div class="mt-2 flex gap-2" id="job-list" data-role="list"></div>',
        "{# A comment in English is not shown #}",
        "<!-- Neither is this one -->",
        "<!doctype html>",
        "<script>addEventListener('DOMContentLoaded', init);</script>",
        '<button type="submit" name="locale" value="auto">{% trans %}Automatic{% endtrans %}'
        "</button>",
        "<p>&nbsp;&middot;&nbsp;</p>",
        "<div x-data=\"{ busy: {{ 'true' if busy else 'false' }} }\"></div>",
        "{% if status == 'running' %}{% trans %}Running{% endtrans %}{% endif %}",
        "{% trans count=(rows | length) %}{{ count }} row{% pluralize %}{{ count }} rows"
        "{% endtrans %}",
        "{% trans v=(_('New job') if job is none else job.id) %}{{ v }}{% endtrans %}",
        "{% trans n=(x | round(0, 'floor')) %}about {{ n }} h{% endtrans %}",
        "{% trans total=result['items'] | length %}{{ total }} items{% endtrans %}",
    ],
)
def test_the_scan_leaves_marked_text_and_markup_alone(source: str) -> None:
    assert i18n_scan.scan(source) == []
