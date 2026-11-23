"""Nothing in any template that the enforced content security policy would refuse (T-UI-03).

The policy is enforced (app/csp.py), so a template with an executable inline script, an inline
event handler, an Alpine expression the CSP build of Alpine cannot evaluate, a `<style>` block
or a `style` attribute breaks its page. tests/csp_scan.py counts these per template; every
count of every template under src/app/templates/ must be zero. This replaces the ratchet of
the conversion (tests/csp_baseline.json), which only had to shrink.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))

import csp_scan  # noqa: E402

TEMPLATES = REPO / "src" / "app" / "templates"
ZERO = dict.fromkeys(csp_scan.KINDS, 0)


def _templates() -> list[str]:
    return sorted(p.relative_to(TEMPLATES).as_posix() for p in TEMPLATES.rglob("*.html"))


# -- every template ------------------------------------------------------------------------


def test_the_scan_covers_every_template() -> None:
    names = _templates()
    assert "base.html" in names
    assert any(name.startswith("partials/") for name in names)


@pytest.mark.parametrize("name", _templates())
def test_the_template_has_nothing_the_policy_refuses(name: str) -> None:
    counts = csp_scan.count_markup((TEMPLATES / name).read_text(encoding="utf-8"))
    assert counts == ZERO, name


def test_no_template_has_a_finding() -> None:
    assert csp_scan.findings() == []


# -- the scanner ---------------------------------------------------------------------------


def test_a_clean_template_counts_nothing() -> None:
    clean = """
    {% from "partials/_csp.html" import data_island %}
    <script src="{{ asset_url('js/pages/x.js') }}" type="module"></script>
    {{ data_island("x", value) }}
    <script type="application/json" id="y">{"a": 1}</script>
    <div x-data="{ open: false }" x-on:click="open = !open" :class="open ? 'a' : 'b'"
         :style="{ width: pct + '%' }" x-text="item.label" @keydown.escape="close()"></div>
    <p class="online">{{ "onclick" }} documented</p>
    """
    assert csp_scan.count_markup(clean) == ZERO


@pytest.mark.parametrize(
    ("markup", "kind"),
    [
        ("<script>var x = 1;</script>", "inline_scripts"),
        ('<script type="module">import "./a.js";</script>', "inline_scripts"),
        ('<script type="text/javascript">x()</script>', "inline_scripts"),
        ('<button onclick="go()">go</button>', "on_attributes"),
        ('<form onsubmit="return check()"></form>', "on_attributes"),
        ('<img src="x" ONERROR="go()">', "on_attributes"),
        ('<button x-on:click="() => go()">go</button>', "alpine_unsupported"),
        ('<button @click="items.forEach(i => i.done = true)">go</button>', "alpine_unsupported"),
        ('<span x-text="`${a} of ${b}`"></span>', "alpine_unsupported"),
        ('<span x-text="Math.round(pct)"></span>', "alpine_unsupported"),
        ("<div x-init=\"window.addEventListener('x', f)\"></div>", "alpine_unsupported"),
        ('<div x-data="{ v: JSON.parse(raw) }"></div>', "alpine_unsupported"),
        ('<div @click="document.body.focus()"></div>', "alpine_unsupported"),
        ('<div x-html="markup"></div>', "alpine_unsupported"),
        ("<style>.a { color: red }</style>", "style_blocks"),
        ('<div style="width: 3rem"></div>', "style_attributes"),
    ],
)
def test_each_kind_is_counted(markup: str, kind: str) -> None:
    expected = dict(ZERO)
    expected[kind] = 1
    assert csp_scan.count_markup(markup) == expected


def test_jinja_values_never_count_as_markup() -> None:
    markup = '<p title="{{ \'<script>\' }}">{{ "<style>" }}</p>{# <script> #}'
    assert csp_scan.count_markup(markup) == ZERO


# -- seeded findings -----------------------------------------------------------------------


def _seed(tmp_path: Path, name: str, extra: str) -> Path:
    templates = tmp_path / "templates"
    for path in TEMPLATES.rglob("*.html"):
        target = templates / path.relative_to(TEMPLATES)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    seeded = templates / name
    seeded.parent.mkdir(parents=True, exist_ok=True)
    before = seeded.read_text(encoding="utf-8") if seeded.is_file() else ""
    seeded.write_text(before + extra, encoding="utf-8")
    return templates


@pytest.mark.parametrize(
    ("extra", "kind"),
    [
        ("<script>window.seeded = 1;</script>", "inline_scripts"),
        ('<button onclick="seeded()">x</button>', "on_attributes"),
        ('<button x-on:click="() => seeded()">x</button>', "alpine_unsupported"),
        ("<style>p { color: red }</style>", "style_blocks"),
        ('<p style="color: red">x</p>', "style_attributes"),
    ],
)
def test_the_zero_rule_fails_on_a_seeded_finding(tmp_path: Path, extra: str, kind: str) -> None:
    templates = _seed(tmp_path, "jobs.html", extra)
    assert csp_scan.findings(templates) == [f"jobs.html: {kind} 1"]


def test_the_zero_rule_fails_on_a_seeded_inline_script_in_the_frame(tmp_path: Path) -> None:
    templates = _seed(tmp_path, "base.html", "<script>alert(1)</script>")
    assert csp_scan.findings(templates) == ["base.html: inline_scripts 1"]


def test_the_zero_rule_fails_on_a_seeded_onclick_in_a_new_partial(tmp_path: Path) -> None:
    templates = _seed(tmp_path, "partials/seeded.html", '<a href="#" onclick="go()">x</a>')
    assert csp_scan.findings(templates) == ["partials/seeded.html: on_attributes 1"]


def test_the_scanner_has_no_baseline_left() -> None:
    assert not hasattr(csp_scan, "compare")
    assert not hasattr(csp_scan, "BASELINE")
