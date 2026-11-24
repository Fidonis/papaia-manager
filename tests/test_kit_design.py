"""Kit 0.1.0 in the manager: version stamp, specimen, brand label, kit files and design gate.

PLT-83 (A5: meta `papaia-kit` on every page; the design check of the kit), PLT-24-A2 (no own
value for a kit variable, stamp equal to the bill of materials), PLT-25 (brand, Ä-30),
oberfl 3.6 to 3.8. The browser run of `pk-conform --design` needs a running manager and the
kit's browser tool (scripts/design-check.sh); this file checks without a browser what that
run reads and how the gate decides on its report.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import sys
import tempfile
import tomllib
import zipfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_WORLD = Path(tempfile.mkdtemp(prefix="papaia-kit-design-"))

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(REPO / "tests"))

import check_design  # noqa: E402
import design_serve  # noqa: E402
import design_world  # noqa: E402

# Empty directories for the settings at import time, as the other test modules set them: the
# stand-in world (RAG profile, a distributed topology) is made per test by the fixture, so it
# never becomes the default of a module imported after this one.
_CONFIG, _WORKSPACE = _WORLD / "config", _WORLD / "workspace"
_CONFIG.mkdir()
_WORKSPACE.mkdir()
for _key, _value in design_world.environment(
    _CONFIG, _WORKSPACE, host="http://localhost:8120", secret="test-session-secret-value"
).items():
    os.environ.setdefault(_key, _value)

import papaia_kit  # noqa: E402
import yaml  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402
from papaia_kit.ui import pages as kit_pages  # noqa: E402
from papaia_kit.ui.specimen import check_specimen_markup, load_specimen  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core import module_catalog, user_prefs  # noqa: E402
from app.core.settings_store import (  # noqa: E402
    BrandingSettings,
    ManagerSettings,
    save_settings,
)
from app.main import create_app  # noqa: E402
from app.templating import templates  # noqa: E402

VERSION = "0.1.0"
STYLESHEET = f"/_kit/{VERSION}/css/papaia-kit.css"
PAGES_FILE = REPO / "ui" / "pages.yaml"
KNOWN_FILE = REPO / "ui" / "design-known.yaml"
SRC = REPO / "src"


def _pages() -> dict[str, Any]:
    doc: dict[str, Any] = yaml.safe_load(PAGES_FILE.read_text(encoding="utf-8"))
    return doc


def page_paths() -> list[str]:
    return [row["path"] for row in _pages()["pages"] if "path" in row]


def page_rows() -> list[tuple[str, str]]:
    """(path, person) of every page with a path: the person who sees it in the stand-in world."""
    return [(row["path"], row["person"]) for row in _pages()["pages"] if "path" in row]


# -- fixtures ------------------------------------------------------------------------------


@pytest.fixture
def world(tmp_path: Path) -> tuple[Path, Path]:
    return design_world.make_world(tmp_path)


@pytest.fixture
def client(world: tuple[Path, Path]) -> Iterator[TestClient]:
    config, workspace = world
    get_settings.cache_clear()
    module_catalog.reset_readers()
    settings = get_settings().model_copy(
        update={"papaia_config_dir": str(config), "papaia_workspace_dir": str(workspace)}
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    design_world.stand_ins(app)
    test_client = TestClient(app, follow_redirects=False)
    test_client.cookies.set(
        design_world.SESSION_COOKIE,
        design_world.session_cookie(get_settings().manager_session_secret),
    )
    yield test_client
    get_settings.cache_clear()
    module_catalog.reset_readers()


# -- version, folder and lock --------------------------------------------------------------


def test_the_manager_runs_kit_0_2_2() -> None:
    assert papaia_kit.__version__ == VERSION


# -- the page list -------------------------------------------------------------------------


def test_the_page_list_is_a_valid_part_of_the_manager() -> None:
    part = kit_pages.load_part(PAGES_FILE.read_bytes())
    assert part["module"] == "manager"
    paths = page_paths()
    assert len(paths) == len(set(paths))
    for required in ("/", "/help", "/_kit/specimen"):
        assert required in paths


def test_the_page_list_names_every_full_page_template() -> None:
    """Every template outside partials/ is a page of the list (error.html is the state).

    That each path of the list answers with its page is the test below.
    """
    templates_dir = SRC / "app" / "templates"
    full = {p.name for p in templates_dir.glob("*.html")} - {"base.html", "error.html"}
    # One row per full-page template: the templates the routes of the list render.
    rendered = {
        "dashboard.html", "services.html", "host.html", "models.html", "jobs.html",
        "job_log.html", "addons.html", "addon_detail.html", "addon_upgrade.html",
        "modules.html", "catalogs.html", "connections.html", "collections.html",
        "embedding.html", "ingest_jobs.html", "ingest_job_edit.html", "ingest_job.html",
        "ingest_runs.html", "ingest_run.html", "ingest_secrets.html", "ingest_orphans.html",
        "ingest_catalog.html", "knowledge_map.html", "backup.html", "upgrade.html",
        "users.html", "roles.html", "notifications.html", "mail.html", "nodes.html",
        "node_detail.html", "audit.html", "settings.html", "settings_installation.html",
        "settings_modules.html", "settings_overlay.html", "help.html", "me.html",
        "kit_specimen.html", "inventory.html", "person_requests.html",
        "access_report.html",
    }  # fmt: skip
    assert full == rendered


def test_every_placeholder_of_the_list_has_a_sample_value() -> None:
    for path in page_paths():
        assert "{" not in design_world.sample_path(path)


# -- every page carries the kit ------------------------------------------------------------


def test_every_person_of_the_list_is_a_person_of_the_stand_in_world() -> None:
    for path, person in page_rows():
        assert person in design_world.PERSONS, (path, person)


def test_the_data_subject_requests_are_a_page_of_the_confirming_person() -> None:
    """Erika holds the confirmation role and nothing of the administration."""
    assert ("/person-requests", "erika") in page_rows()
    assert "papaia-confirm" in design_world.PERSONS["erika"]
    assert not {"admin", "papaia-admin", "papaia-auditor"} & set(design_world.PERSONS["erika"])


def test_the_inventory_is_a_page_of_the_auditor_only() -> None:
    """Separation of duties: the administrator Anna does not see it, the auditor Emil does."""
    assert ("/inventory", "emil") in page_rows()
    assert ("/access-report", "emil") in page_rows()
    assert "papaia-auditor" in design_world.PERSONS["emil"]
    assert "papaia-auditor" not in design_world.PERSONS["anna"]
    assert not {"admin", "papaia-admin"} & set(design_world.PERSONS["emil"])


@pytest.mark.parametrize(("pattern", "person"), page_rows())
def test_every_page_carries_the_kit_version_and_stylesheet(
    client: TestClient, pattern: str, person: str
) -> None:
    client.cookies.set(
        design_world.SESSION_COOKIE,
        design_world.session_cookie(
            get_settings().manager_session_secret,
            roles=design_world.PERSONS[person],
            person=person,
        ),
    )
    response = client.get(design_world.sample_path(pattern))
    assert response.status_code == 200, pattern
    body = response.text
    assert f'<meta name="papaia-kit" content="{VERSION}">' in body
    links = re.findall(r'<link rel="stylesheet" href="([^"]+)"', body)
    assert links[0] == STYLESHEET


def test_the_no_access_page_carries_the_kit_version(client: TestClient) -> None:
    """The state page "Kein Zugang" of the list: an account without the admin role."""
    client.cookies.set(
        design_world.SESSION_COOKIE,
        design_world.session_cookie(get_settings().manager_session_secret, roles=("user",)),
    )
    response = client.get("/settings")
    assert response.status_code == 403
    assert f'<meta name="papaia-kit" content="{VERSION}">' in response.text
    assert re.findall(r'<link rel="stylesheet" href="([^"]+)"', response.text)[0] == STYLESHEET


def test_the_specimen_page_is_served_from_the_vendored_kit(client: TestClient) -> None:
    response = client.get("/_kit/specimen")
    assert response.status_code == 200
    body = response.text
    assert check_specimen_markup(body, load_specimen()) == []
    assert f'<meta name="papaia-kit" content="{VERSION}">' in body
    main = body[body.index('<main id="pk-main"') :]
    # Only the kit: no stylesheet or script of the manager restyles the reference content.
    assert "app.css" not in body and "htmx" not in main
    assert f"/_kit/{VERSION}/icons/sprite.svg" in main


def test_the_specimen_page_needs_a_session(world: tuple[Path, Path]) -> None:
    config, workspace = world
    settings = get_settings().model_copy(
        update={"papaia_config_dir": str(config), "papaia_workspace_dir": str(workspace)}
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    response = TestClient(app, follow_redirects=False).get("/_kit/specimen")
    assert response.status_code in (302, 303, 307, 401)


# -- brand label (contract/shell/shell.md section 4, WCAG 2.5.3) -----------------------------


def _brand(body: str) -> tuple[str, str]:
    """The accessible name and the visible text of the brand link."""
    zone = body[body.index('data-pk-zone="brand"') :]
    zone = zone[: zone.index("</a>")]
    name = html.unescape(re.search(r'aria-label="([^"]+)"', zone).group(1))  # type: ignore[union-attr]
    visible = " ".join(
        html.unescape(t).strip()
        for t in re.findall(r'<span class="pk-brand-(?:name|suffix)">([^<]*)</span>', zone)
        if t.strip()
    )
    return name, visible


@pytest.mark.parametrize(
    ("name", "tagline", "expected"),
    [
        ("Musterfirma", "Testinstallation", "Musterfirma Testinstallation, Übersicht"),
        ("Musterfirma", "", "Musterfirma, Übersicht"),
        (None, None, "papAIa manager by Fidonis, Übersicht"),
    ],
)
def test_the_brand_link_is_named_by_its_visible_text(
    client: TestClient,
    world: tuple[Path, Path],
    name: str | None,
    tagline: str | None,
    expected: str,
) -> None:
    config, _ = world
    save_settings(
        str(config), ManagerSettings(branding=BrandingSettings(name=name, tagline=tagline))
    )
    user_prefs.set_locale(str(config), "u-anna", "de")
    for path in ("/", "/help", "/_kit/specimen"):
        label, visible = _brand(client.get(path).text)
        assert label == expected, path
        assert label.startswith(visible), path


def test_the_brand_label_in_english_follows_the_same_rule(
    client: TestClient, world: tuple[Path, Path]
) -> None:
    config, _ = world
    branding = BrandingSettings(name="Musterfirma", tagline="Testinstallation")
    save_settings(str(config), ManagerSettings(branding=branding))
    user_prefs.set_locale(str(config), "u-anna", "en")
    label, visible = _brand(client.get("/").text)
    assert label == "Musterfirma Testinstallation, overview"
    assert label.startswith(visible)


# -- kit macros in the manager's environment -----------------------------------------------


def test_the_environment_offers_the_kit_macros() -> None:
    env = templates.env
    assert "pk_ui" in env.globals and "pk_text" in env.globals
    page = env.from_string(
        '{% import "pk/components.html" as pk with context %}'
        '{{ pk.button("Speichern <jetzt>", variant="primary") }}'
    ).render(pk_lang="de", pk_csrf="x" * 24)
    assert "Speichern &lt;jetzt&gt;" in page
    icon = env.from_string(
        '{% import "pk/components.html" as pk with context %}{{ pk.icon("plus") }}'
    ).render(pk_lang="de", pk_csrf="x" * 24)
    assert f"/_kit/{VERSION}/icons/sprite.svg#pk-icon-plus" in icon


def test_own_templates_still_win_over_the_kit() -> None:
    filename = templates.env.get_template("base.html").filename
    assert filename is not None
    assert Path(filename).resolve() == (SRC / "app" / "templates" / "base.html").resolve()


# -- no value of a kit variable changes (PLT-24-A2) ----------------------------------------


def _wheel() -> Path:
    lock = tomllib.loads((SRC / "kit.lock").read_text(encoding="utf-8"))
    path: str = lock["files"][0]["path"]
    return SRC / path


def test_the_served_kit_files_equal_the_wheel(client: TestClient) -> None:
    prefix = "papaia_kit/_data/dist/"
    served = 0
    with zipfile.ZipFile(_wheel()) as wheel:
        for entry in wheel.namelist():
            if not entry.startswith(prefix):
                continue
            folder, _, name = entry[len(prefix) :].partition("/")
            if folder not in ("css", "js", "icons", "fonts") or "/" in name or not name:
                continue
            if name.startswith(".") or Path(name).suffix not in (".css", ".js", ".svg", ".woff2"):
                continue
            response = client.get(f"/_kit/{VERSION}/{folder}/{name}")
            assert response.status_code == 200, entry
            expected = hashlib.sha256(wheel.read(entry)).hexdigest()
            assert hashlib.sha256(response.content).hexdigest() == expected, entry
            served += 1
    assert served >= 4


_OWN_VALUE = re.compile(r"--pk-[A-Za-z0-9-]+\s*:")


def test_no_stylesheet_of_the_manager_defines_a_kit_variable() -> None:
    """The kit's variables keep the kit's values: no own value in any style source."""
    sources = [*(REPO / "docker").glob("tailwind.*"), *(SRC / "app" / "templates").rglob("*.html")]
    sources += [*(SRC / "app" / "static").rglob("*.css")]
    assert len(sources) > 10
    for source in sources:
        text = source.read_text(encoding="utf-8")
        assert not _OWN_VALUE.search(text), source


# -- the known design findings -------------------------------------------------------------


def test_design_known_list_is_well_formed() -> None:
    entries = check_design.parse_known(KNOWN_FILE.read_text(encoding="utf-8"))
    paths = set(page_paths())
    for entry in entries:
        assert entry.page in paths, entry.page
        rule = entry.check.removeprefix(check_design.AXE_PREFIX)
        assert entry.check in check_design.CHECKS or rule != entry.check, entry.check
        # Only findings the page rebuild removes may wait, each with its ticket.
        assert re.fullmatch(r"MGR-013|Fidonis/papaia-manager#[0-9]+", entry.ticket), entry.ticket


def test_the_design_check_covers_every_page_of_the_list() -> None:
    assert design_serve.design_pages() == [design_world.sample_path(p) for p in page_paths()]
    by_person = [
        page for person in design_serve.design_persons()
        for page in design_serve.design_pages(person=person)
    ]  # fmt: skip
    assert sorted(by_person) == sorted(design_serve.design_pages())
    assert design_serve.design_persons() == ["anna", "emil", "erika"]
    assert design_serve.design_pages(person="emil") == ["/inventory", "/access-report"]
    assert design_serve.design_pages(person="erika") == ["/person-requests"]


GOOD = """\
# comment
- check: measure
  page: /
  ticket: MGR-013
  reason: the overview text runs wider than the measure until the rebuild
"""


def test_parse_known_reads_the_form() -> None:
    (entry,) = check_design.parse_known(GOOD)
    assert entry == check_design.KnownEntry(
        check="measure",
        page="/",
        ticket="MGR-013",
        reason="the overview text runs wider than the measure until the rebuild",
    )
    assert check_design.parse_known("# nothing\n") == []


@pytest.mark.parametrize(
    "text",
    [
        GOOD.replace("check: measure", "rule: measure"),  # unknown field
        GOOD.replace("  ticket: MGR-013\n", ""),  # missing field
        GOOD + "  ticket: MGR-014\n",  # field twice
        GOOD.replace("page: /", "page: overview"),  # page form
        GOOD.replace("ticket: MGR-013", "ticket: soon"),  # ticket form
        GOOD.replace("check: measure", "check: Measure!"),  # check form
        GOOD.replace("reason: the overview", "reason: short").split("text")[0] + "\n",
        GOOD + GOOD.replace("# comment\n", ""),  # duplicate check and page
        "  check: measure\n",  # no leading entry
        GOOD.replace("check: measure", "check: [measure]"),  # flow value
        GOOD.replace("page: /", 'page: "/\\x"'),  # escape in quotes
        "x" * (check_design.MAX_FILE + 1),  # too large
    ],
)
def test_parse_known_rejects_malformed_lists(text: str) -> None:
    with pytest.raises(check_design.KnownListError):
        check_design.parse_known(text)


def test_parse_known_accepts_issue_references() -> None:
    (entry,) = check_design.parse_known(GOOD.replace("MGR-013", "Fidonis/papaia-manager#31"))
    assert entry.ticket == "Fidonis/papaia-manager#31"


# -- gate over the pk-conform report -------------------------------------------------------


def report(
    findings: dict[tuple[str, str, str], list[str]] | None = None,
    *,
    a11y_ok: bool = True,
) -> dict[str, Any]:
    pages = []
    for page in ("/", "/settings"):
        for theme in ("light", "dark"):
            for viewport in ("desktop", "mobile"):
                checks = (findings or {}).get((page, theme, viewport), [])
                pages.append(
                    {
                        "page": page,
                        "theme": theme,
                        "viewport": viewport,
                        "findings": [{"check": c, "message": f"{c} off"} for c in checks],
                    }
                )
    failed = any((findings or {}).values()) or not a11y_ok
    return {
        "format": "papaia.pk-design/1",
        "status": "fail" if failed else "pass",
        "pages": pages,
        "a11y": {"ok": a11y_ok, "blocking": [], "unlisted": [], "listed": [], "stale": []},
    }


KNOWN = check_design.parse_known(GOOD)


def test_gate_passes_a_clean_report() -> None:
    result = check_design.evaluate(report(), [])
    assert result.ok
    assert result.unlisted == [] and result.stale == [] and result.listed == []


def test_gate_accepts_listed_findings_in_any_theme_and_width() -> None:
    result = check_design.evaluate(
        report({("/", "light", "desktop"): ["measure"], ("/", "dark", "mobile"): ["measure"]}),
        KNOWN,
    )
    assert result.ok
    assert len(result.listed) == 2


def test_gate_fails_unlisted_and_stale_entries() -> None:
    result = check_design.evaluate(report({("/settings", "dark", "mobile"): ["font"]}), KNOWN)
    assert not result.ok
    assert [(f.page, f.theme, f.viewport, f.check) for f in result.unlisted] == [
        ("/settings", "dark", "mobile", "font")
    ]
    assert result.stale == KNOWN


def test_gate_fails_accessibility_findings_of_the_report() -> None:
    result = check_design.evaluate(report(a11y_ok=False), [])
    assert not result.ok
    assert result.a11y_failed


def _blocking(rule: str, page: str) -> dict[str, Any]:
    return {"id": rule, "impact": "serious", "page": page, "theme": "light", "state": "rest"}


def test_gate_accepts_a_blocking_axe_finding_only_when_listed() -> None:
    doc = report(a11y_ok=False)
    doc["a11y"]["blocking"] = [_blocking("color-contrast", "/settings")]
    result = check_design.evaluate(doc, [])
    assert not result.ok and not result.a11y_failed
    assert [(f.page, f.check) for f in result.unlisted] == [("/settings", "axe-color-contrast")]
    known = check_design.parse_known(
        GOOD.replace("check: measure", "check: axe-color-contrast").replace("/", "/settings", 1)
    )
    assert check_design.evaluate(doc, known).ok
    # Listed for another rule or page, it is not covered and the entry is stale.
    doc["a11y"]["blocking"] = [_blocking("label", "/settings")]
    result = check_design.evaluate(doc, known)
    assert not result.ok and result.stale == known


def test_gate_fails_an_unlisted_finding_below_serious() -> None:
    doc = report(a11y_ok=False)
    doc["a11y"]["unlisted"] = [_blocking("region", "/")]
    assert check_design.evaluate(doc, []).a11y_failed


def test_gate_rejects_a_malformed_blocking_finding() -> None:
    doc = report(a11y_ok=False)
    doc["a11y"]["blocking"] = [_blocking("Color Contrast!", "/")]
    with pytest.raises(check_design.ReportError):
        check_design.evaluate(doc, [])


def test_gate_matches_templated_page_paths() -> None:
    known = check_design.parse_known(GOOD.replace("page: /", 'page: "/jobs/{job_id}"'))
    doc = report()
    doc["pages"].append(
        {
            "page": "/jobs/0b5c4a1e-job",
            "theme": "light",
            "viewport": "desktop",
            "findings": [{"check": "measure", "message": "wide"}],
        }
    )
    assert check_design.evaluate(doc, known).ok
    doc["pages"][-1]["page"] = "/jobs/0b5c4a1e-job/log"
    assert not check_design.evaluate(doc, known).ok


@pytest.mark.parametrize(
    "doc",
    [
        {},
        {"format": "papaia.pk-design/2", "status": "pass", "pages": [], "a11y": {"ok": True}},
        {"format": "papaia.pk-design/1", "status": "pass", "pages": "x", "a11y": {"ok": True}},
        {"format": "papaia.pk-design/1", "status": "pass", "pages": [], "a11y": {}},
        {
            "format": "papaia.pk-design/1",
            "status": "pass",
            "pages": [{"page": "/", "theme": "light", "viewport": "desktop", "findings": [1]}],
            "a11y": {"ok": True},
        },
    ],
)
def test_gate_rejects_malformed_reports(doc: dict[str, Any]) -> None:
    with pytest.raises(check_design.ReportError):
        check_design.evaluate(doc, [])


def test_gate_cli(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    known = tmp_path / "design-known.yaml"
    known.write_text(GOOD, encoding="utf-8")
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report({("/", "light", "desktop"): ["measure"]})), "utf-8")
    assert check_design.main(["--report", str(path), "--known", str(known)]) == 0
    path.write_text(json.dumps(report({("/", "light", "desktop"): ["font"]})), "utf-8")
    assert check_design.main(["--report", str(path), "--known", str(known)]) == 1
    err = capsys.readouterr().err
    assert "font" in err and "stale" in err
    path.write_text("{", encoding="utf-8")
    assert check_design.main(["--report", str(path), "--known", str(known)]) == 2
    assert check_design.main(["--report", str(tmp_path / "missing"), "--known", str(known)]) == 2


# -- the manager for the browser run -------------------------------------------------------


def test_design_serve_writes_a_signed_in_storage_state(tmp_path: Path) -> None:
    state = tmp_path / "state.json"
    design_serve.write_storage_state(state, port=57880, secret="design-secret-value-0001")
    doc = json.loads(state.read_text(encoding="utf-8"))
    [cookie] = doc["cookies"]
    assert cookie["name"] == design_world.SESSION_COOKIE
    assert cookie["domain"] == "127.0.0.1"
    assert cookie["httpOnly"] is True and cookie["secure"] is False
    assert doc["origins"] == []
    # The cookie is Anna's session, signed with the secret the served manager runs with.
    session = json.loads(
        base64.b64decode(TimestampSigner("design-secret-value-0001").unsign(cookie["value"]))
    )
    assert session["user"]["preferred_username"] == "anna"
    assert session["user"]["roles"] == list(design_world.ROLES)


def test_design_serve_writes_the_session_of_each_person(tmp_path: Path) -> None:
    state = tmp_path / "emil.json"
    design_serve.write_storage_state(
        state, port=57880, secret="design-secret-value-0001", person="emil"
    )
    [cookie] = json.loads(state.read_text(encoding="utf-8"))["cookies"]
    session = json.loads(
        base64.b64decode(TimestampSigner("design-secret-value-0001").unsign(cookie["value"]))
    )
    assert session["user"]["preferred_username"] == "emil"
    assert session["user"]["roles"] == list(design_world.PERSONS["emil"])


@pytest.mark.parametrize("value", ["zoe=s.json", "=s.json", "emil="])
def test_design_serve_refuses_an_unknown_person(value: str) -> None:
    with pytest.raises(SystemExit) as stop:
        design_serve.main(["--port", "57880", "--storage-state", value])
    assert stop.value.code == 2


@pytest.mark.parametrize("port", ["80", "1023", "65536", "abc", "-1"])
def test_design_serve_takes_unprivileged_loopback_ports_only(port: str, tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as stop:
        design_serve.main(["--port", port, "--storage-state", str(tmp_path / "s.json")])
    assert stop.value.code == 2


def test_the_ci_design_job_needs_no_secret_and_no_other_repository() -> None:
    """The job runs on the hosted runner of a public repository: vendored tools only."""
    workflow = yaml.safe_load((REPO / ".github" / "workflows" / "ci.yml").read_text("utf-8"))
    job = workflow["jobs"]["design"]
    text = yaml.safe_dump(job)
    assert "secrets." not in text and "token" not in text.lower()
    checkouts = [s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/checkout")]
    assert [c.get("with", {}).get("repository") for c in checkouts] == [None]
    runs = " ".join(str(s.get("run", "")) for s in job["steps"])
    assert "scripts/design-tools.sh" in runs and "scripts/design-check.sh" in runs
