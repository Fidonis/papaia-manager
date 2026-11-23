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


# -- the shared page script (T-UI-04) --------------------------------------------------------

# Pages without a finding of their own that still render partials with findings through htmx
# (static includes are followed by the test below). They are converted with those partials.
_LOADS_PARTIALS_WITH_FINDINGS: dict[str, str] = {}
_STATIC_USE = re.compile(r'\{%-?\s*(?:include|import|from|extends)\s+"([^"]+)"')


def _closure(name: str) -> set[str]:
    seen: set[str] = set()
    todo = [name]
    while todo:
        current = todo.pop()
        if current in seen or current.startswith("pk/"):
            continue
        seen.add(current)
        todo.extend(_STATIC_USE.findall((TEMPLATES / current).read_text(encoding="utf-8")))
    return seen


def test_the_shared_templates_carry_no_findings() -> None:
    rows = _templates_of_baseline()
    for shared in ("base.html", "partials/service_status_chip.html"):
        assert shared not in rows, shared
        counts = csp_scan.count_markup((TEMPLATES / shared).read_text(encoding="utf-8"))
        assert counts == dict.fromkeys(csp_scan.KINDS, 0), shared
    # The helpers of the knowledge pages moved into static/js/core/actions.js.
    assert not (TEMPLATES / "partials" / "_rag_js.html").exists()
    assert "partials/_rag_js.html" not in rows


def test_every_page_without_findings_runs_on_the_csp_build() -> None:
    rows = _templates_of_baseline()
    converted = set(_baseline()["converted"])  # type: ignore[arg-type]
    pages = sorted(
        path.name
        for path in TEMPLATES.glob("*.html")
        if "base.html" in _closure(path.name) and path.name != "base.html"
    )
    clean = {name for name in pages if not (_closure(name) - {"base.html"}) & set(rows)}
    # A page of its own frame (kit_specimen.html) is listed as well; it carries no finding.
    assert clean - set(_LOADS_PARTIALS_WITH_FINDINGS) == converted & set(pages)
    for name in converted - set(pages):
        assert (TEMPLATES / name).is_file(), name
        assert name not in rows, name
    for name in _LOADS_PARTIALS_WITH_FINDINGS:
        assert name in clean, f"{name} has findings again; drop it from the exception list"
    # A converted page never has a finding, through an include either.
    for name in converted:
        assert not _closure(name) & set(rows), name


# -- page groups converted to the CSP build (MGR-017 onwards) ----------------------------------

# Backup and restore: the page and every partial htmx swaps into it.
_BACKUP_GROUP = (
    "backup.html",
    "partials/backup_job_status.html",
    "partials/backup_schedule.html",
    "partials/backup_schedule_preview.html",
    "partials/restore_point_list.html",
    "partials/restore_status.html",
)


@pytest.mark.parametrize("name", _BACKUP_GROUP)
def test_the_backup_group_has_no_inline_script_handler_or_rejected_expression(name: str) -> None:
    counts = csp_scan.count_markup((TEMPLATES / name).read_text(encoding="utf-8"))
    assert counts == dict.fromkeys(csp_scan.KINDS, 0), name
    assert name not in _templates_of_baseline(), name


def test_the_backup_page_runs_on_the_csp_build() -> None:
    assert "backup.html" in _baseline()["converted"]  # type: ignore[operator]


# -- users, roles and audit log on the CSP build (MGR-021) --------------------------------------

# The pages and the partial htmx swaps into the audit log; the lists of users and roles
# (partials/user_list.html, partials/role_list.html) carry no finding of their own.
_ACCOUNTS_GROUP = ("audit.html", "partials/audit_list.html", "roles.html", "users.html")


@pytest.mark.parametrize("name", _ACCOUNTS_GROUP)
def test_the_accounts_group_has_no_inline_script_handler_or_rejected_expression(
    name: str,
) -> None:
    counts = csp_scan.count_markup((TEMPLATES / name).read_text(encoding="utf-8"))
    assert counts == dict.fromkeys(csp_scan.KINDS, 0), name
    assert name not in _templates_of_baseline(), name


def test_the_accounts_pages_run_on_the_csp_build() -> None:
    converted = set(_baseline()["converted"])  # type: ignore[arg-type]
    assert {"audit.html", "roles.html", "users.html"} <= converted
    for name in ("partials/user_list.html", "partials/role_list.html"):
        assert name not in _templates_of_baseline(), name


# -- the ingest helpers and lists --------------------------------------------------------------

# The pages of the ingest lists and every partial they include or htmx swaps into them. The
# job editor and the credentials page are a group of their own.
_INGEST_PAGES = (
    "ingest_catalog.html",
    "ingest_job.html",
    "ingest_jobs.html",
    "ingest_orphans.html",
    "ingest_run.html",
    "ingest_runs.html",
)
_INGEST_GROUP = (
    *_INGEST_PAGES,
    "partials/_ingest_dialogs.html",
    "partials/_ingest_macros.html",
    "partials/_ingest_texts.html",
    "partials/ingest_job_config.html",
    "partials/ingest_job_files.html",
    "partials/ingest_job_files_table.html",
    "partials/ingest_job_gone.html",
    "partials/ingest_job_list.html",
    "partials/ingest_job_overview.html",
    "partials/ingest_orphan_list.html",
    "partials/ingest_preview.html",
    "partials/ingest_run_detail.html",
    "partials/ingest_run_rows.html",
)


@pytest.mark.parametrize("name", _INGEST_GROUP)
def test_the_ingest_group_has_no_inline_script_handler_or_rejected_expression(name: str) -> None:
    counts = csp_scan.count_markup((TEMPLATES / name).read_text(encoding="utf-8"))
    assert counts == dict.fromkeys(csp_scan.KINDS, 0), name
    assert name not in _templates_of_baseline(), name


def test_the_ingest_pages_run_on_the_csp_build() -> None:
    converted = set(_baseline()["converted"])  # type: ignore[arg-type]
    assert set(_INGEST_PAGES) <= converted
    # The helpers of the ingest pages moved into static/js/pages/ingest*.js.
    assert not (TEMPLATES / "partials" / "_ingest_js.html").exists()
    assert "partials/_ingest_js.html" not in _templates_of_baseline()
