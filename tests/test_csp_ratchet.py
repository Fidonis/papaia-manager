"""The ratchet towards the strict content security policy (T-UI-03).

tests/csp_baseline.json holds, per template, what still stands between it and the policy
(tests/csp_scan.py). A count above the baseline fails; a count below it fails as well until
the baseline is lowered in the same change, so the baseline only shrinks. The templates listed
as `converted` run on the CSP build of Alpine (base.html).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))

import csp_scan  # noqa: E402

BASELINE = REPO / "tests" / "csp_baseline.json"
TEMPLATES = REPO / "src" / "app" / "templates"


def _baseline() -> dict[str, object]:
    doc: dict[str, object] = json.loads(BASELINE.read_text(encoding="utf-8"))
    return doc


def _templates_of_baseline() -> dict[str, dict[str, int]]:
    rows = _baseline()["templates"]
    assert isinstance(rows, dict)
    return rows


# -- the baseline --------------------------------------------------------------------------


def test_the_baseline_is_well_formed() -> None:
    doc = _baseline()
    assert set(doc) == {"format", "alpine", "converted", "templates", "totals"}
    assert doc["format"] == "papaia-manager.csp-baseline/1"
    alpine = doc["alpine"]
    assert isinstance(alpine, dict)
    assert set(alpine) == {"package", "version"}
    assert alpine["package"] == "@alpinejs/csp"
    assert re.fullmatch(r"\d+\.\d+\.\d+", str(alpine["version"]))
    converted = doc["converted"]
    assert isinstance(converted, list)
    assert converted == sorted(set(converted))
    for name in converted:
        assert (TEMPLATES / name).is_file(), name
        assert "/" not in name, f"{name}: only full pages are converted"
    rows = _templates_of_baseline()
    for name in converted:
        # A converted page has nothing left to count.
        assert name not in rows, f"{name}: a converted template has findings"
    for name, counts in rows.items():
        assert (TEMPLATES / name).is_file(), name
        assert set(counts) == set(csp_scan.KINDS), name
        assert all(isinstance(v, int) and v >= 0 for v in counts.values()), name
        # A template without findings has no row.
        assert any(counts.values()), name
    assert doc["totals"] == csp_scan.totals(rows.values())


def test_the_alpine_version_of_the_baseline_is_the_one_the_image_ships() -> None:
    dockerfile = (REPO / "docker" / "Dockerfile").read_text(encoding="utf-8")
    version = _baseline()["alpine"]["version"]  # type: ignore[index]
    assert f"ARG ALPINE_VERSION={version}\n" in dockerfile


def test_the_measured_counts_equal_the_baseline() -> None:
    problems = csp_scan.compare(_templates_of_baseline(), csp_scan.scan())
    assert problems == [], "\n".join(problems)


# -- the scanner ---------------------------------------------------------------------------


def _counts(markup: str) -> dict[str, int]:
    return csp_scan.count_markup(markup)


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
    assert _counts(clean) == dict.fromkeys(csp_scan.KINDS, 0)


@pytest.mark.parametrize(
    ("markup", "kind"),
    [
        ("<script>var x = 1;</script>", "inline_scripts"),
        ('<script type="module">import "./a.js";</script>', "inline_scripts"),
        ('<script type="text/javascript">x()</script>', "inline_scripts"),
        ('<button onclick="go()">go</button>', "on_attributes"),
        ('<form onsubmit="return check()"></form>', "on_attributes"),
        ('<button x-on:click="() => go()">go</button>', "alpine_unsupported"),
        ('<button @click="items.forEach(i => i.done = true)">go</button>', "alpine_unsupported"),
        ("<span x-text=\"`${a} of ${b}`\"></span>", "alpine_unsupported"),
        ('<span x-text="Math.round(pct)"></span>', "alpine_unsupported"),
        ('<div x-init="window.addEventListener(\'x\', f)"></div>', "alpine_unsupported"),
        ('<div x-data="{ v: JSON.parse(raw) }"></div>', "alpine_unsupported"),
        ('<div @click="document.body.focus()"></div>', "alpine_unsupported"),
        ('<div x-html="markup"></div>', "alpine_unsupported"),
        ("<style>.a { color: red }</style>", "style_blocks"),
        ('<div style="width: 3rem"></div>', "style_attributes"),
    ],
)
def test_each_kind_is_counted(markup: str, kind: str) -> None:
    expected = dict.fromkeys(csp_scan.KINDS, 0)
    expected[kind] = 1
    assert _counts(markup) == expected


def test_jinja_values_never_count_as_markup() -> None:
    markup = '<p title="{{ \'<script>\' }}">{{ "<style>" }}</p>{# <script> #}'
    assert _counts(markup) == dict.fromkeys(csp_scan.KINDS, 0)


def _seed(tmp_path: Path, name: str, extra: str) -> Path:
    templates = tmp_path / "templates"
    for path in TEMPLATES.rglob("*.html"):
        target = templates / path.relative_to(TEMPLATES)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    seeded = templates / name
    seeded.write_text(seeded.read_text(encoding="utf-8") + extra, encoding="utf-8")
    return templates


@pytest.mark.parametrize(
    ("extra", "kind"),
    [
        ("<script>window.seeded = 1;</script>", "inline_scripts"),
        ('<button onclick="seeded()">x</button>', "on_attributes"),
        ('<button x-on:click="() => seeded()">x</button>', "alpine_unsupported"),
        ('<p style="color: red">x</p>', "style_attributes"),
    ],
)
def test_the_ratchet_fails_on_a_seeded_finding(tmp_path: Path, extra: str, kind: str) -> None:
    templates = _seed(tmp_path, "jobs.html", extra)
    problems = csp_scan.compare(_templates_of_baseline(), csp_scan.scan(templates))
    assert len(problems) == 1
    assert problems[0].startswith(f"jobs.html: {kind} rose from ")


def test_the_ratchet_fails_on_a_raised_count_in_a_new_template(tmp_path: Path) -> None:
    templates = _seed(tmp_path, "jobs.html", "")
    (templates / "partials" / "seeded.html").write_text("<style>p{}</style>", encoding="utf-8")
    problems = csp_scan.compare(_templates_of_baseline(), csp_scan.scan(templates))
    assert problems == ["partials/seeded.html: style_blocks rose from 0 to 1"]


def test_the_ratchet_fails_on_a_lowered_count_until_the_baseline_follows() -> None:
    baseline = {name: dict(counts) for name, counts in _templates_of_baseline().items()}
    name = next(iter(baseline))
    kind = next(k for k in csp_scan.KINDS if baseline[name][k] > 0)
    baseline[name][kind] += 1
    problems = csp_scan.compare(baseline, csp_scan.scan())
    assert len(problems) == 1
    assert f"{name}: {kind} fell from" in problems[0]
    assert "lower tests/csp_baseline.json" in problems[0]


def test_the_ratchet_passes_on_the_measured_baseline() -> None:
    measured = csp_scan.scan()
    assert csp_scan.compare(measured, measured) == []
