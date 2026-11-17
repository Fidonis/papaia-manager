"""The entry editor: one entry of the AI map as a form, written into sources.yaml (own
entries) or overrides.yaml (declared entries), and the probe of an unsaved entry."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.core import knowledge_map as km
from app.core import knowledge_map_entries as entries
from tests.test_knowledge_map import (
    _BASE,
    _HEADERS,
    WIKI,
    FakeMapServer,
    _as,
    client,  # noqa: F401 - fixture
    config_dir,  # noqa: F401 - fixture
    queue,  # noqa: F401 - fixture
    server,  # noqa: F401 - fixture
)


@pytest.fixture
def cfg(config_dir: Path) -> Path:  # noqa: F811 - the imported fixture
    return config_dir


@pytest.fixture
def api(client: TestClient) -> TestClient:  # noqa: F811 - the imported fixture
    return client


@pytest.fixture
def fake(server: FakeMapServer) -> FakeMapServer:  # noqa: F811 - the imported fixture
    return server


OWN = {
    "id": "files.protocols",
    "kind": "reference",
    "system": "File share",
    "coverage": "Signed maintenance protocols as PDF",
    "location": "Nextcloud, folder 04_Anlagen",
    "owner": "head of service",
}


def _own(config: Path) -> None:
    km.write_file(str(config), "sources", km._dump({"sources": [OWN]}), expected_revision="")
    manifest = {**OWN, "provenance": {"declared_by": "operator"}}
    (config / "knowledge-map" / "manifests" / "files.protocols.yaml").write_text(
        yaml.safe_dump(manifest), encoding="utf-8"
    )


def _doc(config: Path, key: str) -> dict:
    return yaml.safe_load(km.read_file(str(config), key).text) or {}


# ── normalising the form ────────────────────────────────────────────────────────────────────


def test_clean_keeps_the_editable_fields_and_drops_empty_ones() -> None:
    cleaned = entries.clean(
        {
            "system": "  Wiki ",
            "coverage": "",
            "provenance": {"x": 1},
            "owner": "someone",
            "topics": [{"label": "Prozesse & Abläufe"}, {"label": ""}, {"id": "a", "label": "A"}],
            "example_queries": ["q1", " q1 ", "", "q2"],
            "not_covered": [{"what": "invoices", "instead": "erp.main"}, {"what": ""}],
            "access": [{"via": "rag", "target": "Qdrant", "collection": "wiki", "x": "y"}, {}],
        }
    )
    assert cleaned == {
        "system": "Wiki",
        "topics": [
            {"id": "prozesse-ablaeufe", "label": "Prozesse & Abläufe"},
            {"id": "a", "label": "A"},
        ],
        "example_queries": ["q1", "q2"],
        "not_covered": [{"what": "invoices", "instead": "erp.main"}],
        "access": [{"via": "rag", "target": "Qdrant", "collection": "wiki"}],
    }


# ── reading ─────────────────────────────────────────────────────────────────────────────────


def test_the_form_of_an_own_entry_comes_from_sources_yaml(cfg: Path) -> None:
    _own(cfg)
    view = entries.form(str(cfg), "files.protocols")
    assert view["origin"] == "own" and view["file"] == "sources"
    assert view["fields"]["location"] == OWN["location"]
    assert "owner" not in view["fields"]
    assert view["revision"] == km.read_file(str(cfg), "sources").revision


def test_the_form_of_a_declared_entry_names_the_changed_fields(cfg: Path) -> None:
    km.write_file(
        str(cfg),
        "overrides",
        km._dump({"sources": {"wiki.main": {"coverage": "x", "provenance": 1}}}),
        expected_revision="",
    )
    view = entries.form(str(cfg), "wiki.main")
    assert view["origin"] == "declared" and view["declared_by"] == "addon:mediawiki"
    assert view["overridden"] == ["coverage"]
    assert view["file"] == "overrides"
    with pytest.raises(km.KnowledgeMapError):
        entries.form(str(cfg), "nope")


# ── changing ────────────────────────────────────────────────────────────────────────────────


def test_saving_a_declared_entry_overrides_only_what_differs(cfg: Path) -> None:
    current = entries.form(str(cfg), "wiki.main")["fields"]
    fields = {**current, "coverage": "Processes, checklists and error codes"}
    key, content, _rev, problems = entries.update(str(cfg), "wiki.main", fields, [])
    assert key == "overrides" and not problems
    assert yaml.safe_load(content) == {
        "sources": {"wiki.main": {"coverage": "Processes, checklists and error codes"}}
    }


def test_resetting_a_field_gives_it_back_to_the_declaration(cfg: Path) -> None:
    km.write_file(
        str(cfg),
        "overrides",
        km._dump({"sources": {"wiki.main": {"coverage": "x"}}}),
        expected_revision="",
    )
    _k, content, _r, _p = entries.update(str(cfg), "wiki.main", {}, ["coverage"])
    assert yaml.safe_load(content) == {}


def test_emptying_a_required_field_of_a_declared_entry_is_a_problem(cfg: Path) -> None:
    _k, _c, _r, problems = entries.update(str(cfg), "wiki.main", {"system": ""}, [])
    assert problems


def test_saving_an_own_entry_keeps_its_other_keys(cfg: Path) -> None:
    _own(cfg)
    key, content, _rev, problems = entries.update(
        str(cfg), "files.protocols", {**OWN, "coverage": "Protocols and logbooks"}, []
    )
    assert key == "sources" and not problems
    (saved,) = yaml.safe_load(content)["sources"]
    assert saved["coverage"] == "Protocols and logbooks" and saved["owner"] == OWN["owner"]


# Access path keys the map server reads but the form shows no field for.
_A2A_PATH = {
    "via": "a2a",
    "target": "https://helpdesk.example/agent-card.json",
    "skills": ["ticket-lookup", "password-reset"],
    "note": "only during office hours",
}


def test_saving_an_own_entry_keeps_skills_and_note_of_its_access_paths(cfg: Path) -> None:
    agent = {
        "id": "agent.helpdesk",
        "kind": "agent",
        "system": "Helpdesk agent",
        "coverage": "Tickets and accounts",
        "access": [_A2A_PATH],
    }
    km.write_file(str(cfg), "sources", km._dump({"sources": [agent]}), expected_revision="")
    current = entries.form(str(cfg), "agent.helpdesk")["fields"]
    _k, content, _r, problems = entries.update(
        str(cfg), "agent.helpdesk", {**current, "coverage": "Tickets, accounts and printers"}, []
    )
    assert not problems
    (saved,) = yaml.safe_load(content)["sources"]
    assert saved["access"] == [_A2A_PATH]


def test_an_override_of_the_access_paths_keeps_skills_and_note(cfg: Path) -> None:
    snapshot = {**WIKI["access"][1], "note": "nightly snapshot", "skills": ["search"]}
    declared = {**WIKI, "access": [WIKI["access"][0], snapshot]}
    (cfg / "knowledge-map" / "manifests" / "wiki.main.yaml").write_text(
        yaml.safe_dump(declared), encoding="utf-8"
    )
    current = entries.form(str(cfg), "wiki.main")["fields"]
    access = [{**current["access"][0], "best_for": "live pages"}, current["access"][1]]
    _k, content, _r, problems = entries.update(
        str(cfg), "wiki.main", {**current, "access": access}, []
    )
    assert not problems
    override = yaml.safe_load(content)["sources"]["wiki.main"]
    assert override == {"access": [{**WIKI["access"][0], "best_for": "live pages"}, snapshot]}


def test_a_new_entry_needs_a_free_id_and_its_required_fields(cfg: Path) -> None:
    taken = entries.create(str(cfg), {"id": "wiki.main", "system": "x", "coverage": "y"})
    assert taken[3] and not taken[1]
    _k, content, _r, problems = entries.create(
        str(cfg), {"id": "calendar.team", "system": "Team calendar"}
    )
    assert problems  # no coverage
    key, content, _r, problems = entries.create(
        str(cfg),
        {
            "id": "calendar.team",
            "system": "Team calendar",
            "coverage": "Appointments",
            "access": [{"via": "mcp", "target": "Calendar"}],
        },
    )
    assert key == "sources" and not problems
    assert yaml.safe_load(content)["sources"][0]["kind"] == "source"


def test_only_own_entries_can_be_removed(cfg: Path) -> None:
    _own(cfg)
    _k, content, _r = entries.remove(str(cfg), "files.protocols")
    assert yaml.safe_load(content) == {"sources": []}
    with pytest.raises(km.KnowledgeMapError):
        entries.remove(str(cfg), "wiki.main")


# ── the API ─────────────────────────────────────────────────────────────────────────────────


def test_the_api_queues_entry_changes_and_refuses_stale_or_invalid_ones(
    api: TestClient, cfg: Path
) -> None:
    admin = _as(api, "admin")
    form = admin.get(f"{_BASE}/entries/wiki.main").json()
    body = {"fields": {**form["fields"], "coverage": "More"}, "revision": form["revision"]}
    assert admin.put(f"{_BASE}/entries/wiki.main", json=body, headers=_HEADERS).status_code == 202
    assert admin.put(f"{_BASE}/entries/wiki.main", json=body).status_code == 403
    stale = {**body, "revision": "0" * 64}
    assert admin.put(f"{_BASE}/entries/wiki.main", json=stale, headers=_HEADERS).status_code == 409
    bad = {**body, "fields": {**body["fields"], "system": ""}}
    res = admin.put(f"{_BASE}/entries/wiki.main", json=bad, headers=_HEADERS)
    assert res.status_code == 422 and res.json()["detail"]["problems"]
    new = {
        "fields": {"id": "calendar.team", "system": "Team calendar", "coverage": "Dates"},
        "revision": "",
    }
    assert admin.post(f"{_BASE}/entries", json=new, headers=_HEADERS).status_code == 202
    taken = {**new, "fields": {**new["fields"], "id": "wiki.main"}}
    assert admin.post(f"{_BASE}/entries", json=taken, headers=_HEADERS).status_code == 422
    res = admin.request(
        "DELETE", f"{_BASE}/entries/wiki.main", json={"revision": ""}, headers=_HEADERS
    )
    assert res.status_code == 409
    assert admin.get(f"{_BASE}/entries/nope").status_code == 404
    assert _as(api, "user").get(f"{_BASE}/entries/wiki.main").status_code == 403


def test_the_probe_sends_the_cleaned_draft_to_the_server(
    api: TestClient, fake: FakeMapServer
) -> None:
    admin = _as(api, "admin")
    res = admin.post(
        f"{_BASE}/entries/probe",
        headers=_HEADERS,
        json={
            "fields": {"id": "calendar.team", "coverage": "Dates", "owner": "x"},
            "questions": ["who is on call"],
            "replaces": ["mcp.calendar"],
        },
    )
    assert res.status_code == 200
    method, path, body = fake.calls[-1]
    assert (method, path) == ("POST", "/admin/api/probe")
    assert body == {
        "entry": {"id": "calendar.team", "coverage": "Dates", "system": "calendar.team"},
        "questions": ["who is on call"],
        "replaces": ["mcp.calendar"],
    }
    too_many = {"fields": {}, "questions": ["q"] * 11}
    assert admin.post(f"{_BASE}/entries/probe", headers=_HEADERS, json=too_many).status_code == 422


def test_a_default_value_in_the_form_is_no_change(cfg: Path) -> None:
    current = entries.form(str(cfg), "wiki.main")["fields"]
    _k, content, _r, _p = entries.update(
        str(cfg), "wiki.main", {**current, "on_denied": "hidden", "kind": "source"}, []
    )
    assert yaml.safe_load(content) == {}
    _k, content, _r, _p = entries.update(
        str(cfg), "wiki.main", {**current, "on_denied": "acknowledge"}, []
    )
    assert yaml.safe_load(content) == {"sources": {"wiki.main": {"on_denied": "acknowledge"}}}
