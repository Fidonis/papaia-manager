"""The governance view of the knowledge-map page: the source matrix with add-on proposals, the
model inventory against LiteLLM, agents and their model tier, findings with fixes and
exceptions, the edits that write governance.yaml through the operator-file job, and the fact
export.
"""
from __future__ import annotations

import csv
import io
import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from fastapi.testclient import TestClient

from app.config import get_settings
from app.core import knowledge_map as km
from app.core import knowledge_map_governance as gov
from app.core.audit import audit_path
from app.core.jobs import JobQueue
from app.core.model_usage import ModelEntry
from app.main import create_app
from app.routers import api_knowledge_map_governance as api
from app.routers.knowledge_map_deps import get_knowledge_map_transport
from app.routers.models_deps import get_litellm_transport
from tests.fake_litellm import MASTER_KEY, FakeLitellm
from tests.test_knowledge_map import (
    _BASE,
    _HEADERS,
    FakeMapServer,
    _as,
    config_dir,  # noqa: F401 - fixture
    queue,  # noqa: F401 - fixture
    server,  # noqa: F401 - fixture
)

_GOV = f"{_BASE}/governance"

AGENT = {"id": "agent.service", "kind": "agent", "system": "Service assistant",
         "uses": ["MediaWiki"]}
GOVERNANCE = {
    "sources": {"wiki.main": {"owner": "Teamleitung Service", "classification": "confidential",
                              "personal_data": "ordinary", "data_subjects": ["employees"]}},
    "models": {"qwen-local": {"tier": "local"}, "gpt-4o-mini": {"tier": "local"}},
    "policy": {"embedding_model": "bge-m3"},
}
RENDER_FINDINGS = [
    {"severity": "high", "code": "confidential_in_free_chat", "target": "mcp.calendar",
     "message": "Calendar is selectable in free chat", "origin": "render",
     "category": "governance", "fix": "set chatMenu: false for Calendar in librechat.yaml"},
    {"severity": "high", "code": "open_confidential", "target": "wiki.main",
     "message": "Wiki is open to everyone", "origin": "render", "category": "governance",
     "fix": "add required_roles", "exception": {"expires": "2099-01-01"}},
]


def write_lints(config: Path, findings: list[dict[str, Any]], **extra: Any) -> None:
    lints = yaml.safe_load((config / "knowledge-map" / "lints.yaml").read_text())
    lints["findings"] = [{**f, "category": "quality", "origin": "render"}
                         for f in lints["findings"][:1]] + findings
    lints.update(extra)
    (config / "knowledge-map" / "lints.yaml").write_text(yaml.safe_dump(lints))


@pytest.fixture
def gconfig(config_dir: Path) -> Path:  # noqa: F811 - the imported fixture, extended
    manifests = config_dir / "knowledge-map" / "manifests"
    (manifests / "agent.service.yaml").write_text(yaml.safe_dump(AGENT), encoding="utf-8")
    write_lints(
        config_dir, RENDER_FINDINGS,
        proposals={"wiki.main": {"classification": "internal", "personal_data": "ordinary"},
                   "mcp.calendar": {"classification": "public", "personal_data": "ordinary",
                                    "data_subjects": ["employees"]}},
        agent_models={"agent.service": {"provider": "LiteLLM", "name": "gpt-4o-mini"}},
    )
    (config_dir / "ai" / "litellm").mkdir(parents=True)
    (config_dir / "ai" / "litellm" / ".env").write_text(f"LITELLM_MASTER_KEY={MASTER_KEY}\n")
    km.write_file(str(config_dir), "governance", yaml.safe_dump(GOVERNANCE).encode(),
                  expected_revision="")
    return config_dir


@pytest.fixture
def litellm() -> FakeLitellm:
    return FakeLitellm()


@pytest.fixture
def client(
    gconfig: Path,
    server: FakeMapServer,  # noqa: F811
    litellm: FakeLitellm,
    queue: JobQueue,  # noqa: F811
) -> Iterator[TestClient]:
    get_settings.cache_clear()
    values = get_settings().model_copy(update={"papaia_config_dir": str(gconfig)})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: values
    app.dependency_overrides[get_knowledge_map_transport] = lambda: httpx.MockTransport(
        server.handler
    )
    app.dependency_overrides[get_litellm_transport] = lambda: litellm.transport
    yield TestClient(app, follow_redirects=False)
    get_settings.cache_clear()


class Written:
    """What the write job was asked to put into governance.yaml."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    @property
    def document(self) -> dict[str, Any]:
        loaded = yaml.safe_load(self.calls[-1]["content"])
        assert isinstance(loaded, dict)
        return loaded


@pytest.fixture
def written(monkeypatch: pytest.MonkeyPatch) -> Written:
    record = Written()

    def capture(**kwargs: Any) -> Any:
        record.calls.append(kwargs)

        async def _noop(ctx: Any) -> None:
            return None

        return _noop

    monkeypatch.setattr(api, "write_callback", capture)
    return record


def _revision(config: Path) -> str:
    return km.read_file(str(config), "governance").revision


# ── reading ─────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", [_GOV, f"{_GOV}/export/facts.json", f"{_GOV}/export/facts.csv"])
def test_only_administrators_read_the_governance_view(client: TestClient, path: str) -> None:
    assert _as(client, "user").get(path).status_code == 403


def test_the_matrix_shows_what_is_documented_the_gaps_and_open_proposals(
    client: TestClient,
) -> None:
    data = _as(client, "admin").get(_GOV).json()
    assert data["litellm"]["available"] is True and data["parse_error"] == ""
    rows = {r["id"]: r for r in data["matrix"]}
    assert set(rows) == {"wiki.main", "mcp.calendar"}  # the agent has its own table
    wiki = rows["wiki.main"]
    assert wiki["classification"] == "confidential" and wiki["classification_confirmed"]
    assert wiki["gaps"] == [] and wiki["open_proposal"] == {}
    calendar = rows["mcp.calendar"]
    # an add-on's "public" cannot lower an unconfirmed entry below internal
    assert calendar["classification"] == "internal"
    assert not calendar["classification_confirmed"]
    assert calendar["open_proposal"]["classification"] == "public"
    assert set(calendar["gaps"]) == {"owner", "classification", "personal_data"}


def test_an_entry_without_proposal_counts_as_confidential() -> None:
    assert gov.effective_class({}, {}) == "confidential"
    assert gov.effective_class({}, {"classification": "strictly_confidential"}) == (
        "strictly_confidential")
    assert gov.effective_class({"classification": "public"}, {}) == "public"


def test_the_ropa_reference_is_only_asked_for_once_it_is_used() -> None:
    entries = [{"id": "a.b", "kind": "source"}, {"id": "c.d", "kind": "source"}]
    without = gov.matrix(entries, {"sources": {"a.b": {"personal_data": "ordinary"}}})
    assert "ropa_ref" not in without[0]["gaps"]
    used = gov.matrix(entries, {"sources": {"a.b": {"personal_data": "ordinary"},
                                            "c.d": {"ropa_ref": "VVT-1"}}})
    assert "ropa_ref" in used[0]["gaps"]


def test_a_server_that_is_only_an_access_path_has_no_row_of_its_own() -> None:
    entries = [
        {"id": "crm.main", "kind": "source"},
        {"id": "mcp.crm", "kind": "mcp", "undeclared": False},
        {"id": "mcp.calendar", "kind": "mcp", "undeclared": True},
        {"id": "mcp.erp", "kind": "mcp", "undeclared": False},
    ]
    rows = gov.matrix(entries, {"sources": {"mcp.erp": {"owner": "IT"}}})
    assert [r["id"] for r in rows] == ["crm.main", "mcp.calendar", "mcp.erp"]


def test_findings_carry_their_fix_and_origin(client: TestClient) -> None:
    data = _as(client, "admin").get(_GOV).json()
    found = {(f["code"], f["target"]): f for f in data["findings"]}
    free_chat = found[("confidential_in_free_chat", "mcp.calendar")]
    assert free_chat["origin"] == "render" and "chatMenu" in free_chat["fix"]
    assert found[("open_confidential", "wiki.main")]["exception"]
    # quality findings stay on their own tab
    assert ("undeclared_server", "mcp.calendar") not in found
    contradiction = found[("model_tier_contradiction", "gpt-4o-mini")]
    assert contradiction["severity"] == "high" and contradiction["origin"] == "manager"
    assert contradiction["fix"]
    embedding = found[("unclassified_model", "bge-m3")]
    assert embedding["severity"] == "high"  # the embedding model is in use
    assert "Confirm the suggested tier local" in embedding["fix"]
    assert found[("unclassified_model", "chat-large")]["severity"] == "medium"


def test_models_are_checked_against_litellm(client: TestClient) -> None:
    models = {m["name"]: m for m in _as(client, "admin").get(_GOV).json()["models"]}
    assert models["qwen-local"]["suggested_tier"] == "local"
    assert models["qwen-local"]["effective_tier"] == "local"
    # listed as local, but LiteLLM sends it to the provider's public endpoint
    assert models["gpt-4o-mini"]["effective_tier"] == "third_country"
    assert models["gpt-4o-mini"]["suggested_tier"] is None
    assert models["gpt-4o-mini"]["used_by"] == ["agent.service"]


def test_mixed_deployments_count_as_the_worst_tier() -> None:
    document = {"models": {"mix": {"tier": "local"}}}
    live = gov.LiveModels((ModelEntry("mix", "openai", "10.0.0.5"),
                           ModelEntry("mix", "openai", "api.example.com")))
    models, findings, tiers = gov.model_view(document, live, [])
    assert models[0]["mixed"] is True and tiers["mix"] == "third_country"
    assert models[0]["suggested_tier"] is None
    assert [f["code"] for f in findings] == ["model_tier_contradiction"]


def test_agents_show_their_model_tier_and_what_they_reach(client: TestClient) -> None:
    (agent,) = _as(client, "admin").get(_GOV).json()["agents"]
    assert agent["model"] == "gpt-4o-mini" and agent["tier"] == "third_country"
    assert agent["reached"] == [{"id": "wiki.main", "system": "Company wiki", "via": "mcp",
                                 "classification": "confidential", "personal_data": "ordinary",
                                 "allowed": False}]
    assert agent["result"] == "action_needed"


def test_without_litellm_the_view_is_evaluated_from_the_files(
    client: TestClient, litellm: FakeLitellm
) -> None:
    litellm.down = True
    data = _as(client, "admin").get(_GOV).json()
    assert data["litellm"]["available"] is False and "not reachable" in data["litellm"]["problem"]
    names = {m["name"] for m in data["models"]}
    assert names == {"qwen-local", "gpt-4o-mini", "bge-m3"}  # file, agent, embedding model
    codes = {f["code"] for f in data["findings"] if f["origin"] == "manager"}
    assert codes == {"unclassified_model"}
    assert data["agents"][0]["tier"] == "local"  # as declared; no host to contradict it


def test_a_governance_file_that_cannot_be_parsed_is_reported(
    client: TestClient, gconfig: Path, written: Written
) -> None:
    km.write_file(str(gconfig), "governance", b"sources: [unclosed\n",
                  expected_revision=_revision(gconfig))
    admin = _as(client, "admin")
    assert admin.get(_GOV).json()["parse_error"]
    res = admin.put(f"{_GOV}/entries/wiki.main", json={"revision": _revision(gconfig)},
                    headers=_HEADERS)
    assert res.status_code == 409 and written.calls == []


# ── writing ─────────────────────────────────────────────────────────────────────────────────


def test_an_entry_edit_goes_through_the_write_job(
    client: TestClient, gconfig: Path, written: Written
) -> None:
    res = _as(client, "admin").put(
        f"{_GOV}/entries/wiki.main",
        json={"revision": _revision(gconfig), "ropa_ref": "VVT-012", "owner": "",
              "classification": "internal"},
        headers=_HEADERS,
    )
    assert res.status_code == 202, res.text
    call = written.calls[-1]
    assert call["key"] == "governance" and call["expected_revision"] == _revision(gconfig)
    wiki = written.document["sources"]["wiki.main"]
    assert wiki["ropa_ref"] == "VVT-012" and wiki["classification"] == "internal"
    assert "owner" not in wiki  # an empty value removes the field
    assert wiki["data_subjects"] == ["employees"]  # untouched


def test_an_add_on_proposal_is_confirmed_with_one_call(
    client: TestClient, gconfig: Path, written: Written
) -> None:
    admin = _as(client, "admin")
    res = admin.post(f"{_GOV}/entries/mcp.calendar/confirm-proposal",
                     json={"revision": _revision(gconfig)}, headers=_HEADERS)
    assert res.status_code == 202
    assert written.document["sources"]["mcp.calendar"] == {
        "classification": "public", "personal_data": "ordinary", "data_subjects": ["employees"]}
    # wiki.main has every proposed field set already
    res = admin.post(f"{_GOV}/entries/wiki.main/confirm-proposal",
                     json={"revision": _revision(gconfig)}, headers=_HEADERS)
    assert res.status_code == 409


def test_edits_are_refused_for_unknown_entries_stale_revisions_bad_values_and_no_csrf(
    client: TestClient, gconfig: Path, written: Written
) -> None:
    admin = _as(client, "admin")
    revision = _revision(gconfig)
    assert admin.put(f"{_GOV}/entries/nope.x", json={"revision": revision},
                     headers=_HEADERS).status_code == 404
    assert admin.put(f"{_GOV}/entries/wiki.main", json={"revision": "stale"},
                     headers=_HEADERS).status_code == 409
    assert admin.put(f"{_GOV}/entries/wiki.main",
                     json={"revision": revision, "classification": "secret"},
                     headers=_HEADERS).status_code == 422
    # a field outside the lean set is not written
    assert admin.put(f"{_GOV}/entries/wiki.main",
                     json={"revision": revision, "owner": "x", "purpose": "y"},
                     headers=_HEADERS).status_code == 202
    assert "purpose" not in written.document["sources"]["wiki.main"]
    written.calls.clear()
    assert admin.put(f"{_GOV}/entries/wiki.main", json={"revision": revision}).status_code == 403
    assert admin.post(f"{_GOV}/entries/mcp.calendar/confirm-proposal",
                      json={"revision": revision}).status_code == 403
    assert written.calls == []


def test_a_model_tier_is_set_and_removed(
    client: TestClient, gconfig: Path, written: Written
) -> None:
    admin = _as(client, "admin")
    res = admin.put(f"{_GOV}/models/vendor/chat-large",
                    json={"revision": _revision(gconfig), "tier": "third_country_contracted"},
                    headers=_HEADERS)
    assert res.status_code == 202
    assert written.document["models"]["vendor/chat-large"] == {
        "tier": "third_country_contracted"}
    res = admin.put(f"{_GOV}/models/qwen-local", json={"revision": _revision(gconfig),
                                                       "tier": None}, headers=_HEADERS)
    assert res.status_code == 202 and "qwen-local" not in written.document["models"]
    assert admin.put(f"{_GOV}/models/x", json={"revision": _revision(gconfig), "tier": "moon"},
                     headers=_HEADERS).status_code == 422


def test_exceptions_are_written_with_an_expiry(
    client: TestClient, gconfig: Path, written: Written
) -> None:
    admin = _as(client, "admin")
    res = admin.put(f"{_GOV}/exceptions", json={"revision": _revision(gconfig), "exceptions": [
        {"finding": "unclassified_model", "subject": "bge-m3", "expires": "2027-02-01",
         "risk_ref": "RISK-7"}]}, headers=_HEADERS)
    assert res.status_code == 202
    assert written.document["exceptions"] == [
        {"finding": "unclassified_model", "subject": "bge-m3", "expires": date(2027, 2, 1),
         "risk_ref": "RISK-7"}]
    res = admin.put(f"{_GOV}/exceptions", json={"revision": _revision(gconfig), "exceptions": [
        {"finding": "unclassified_model", "subject": "bge-m3"}]}, headers=_HEADERS)
    assert res.status_code == 422  # no expiry


def test_exceptions_mark_findings_until_they_expire(client: TestClient, gconfig: Path) -> None:
    document = {**GOVERNANCE, "exceptions": [
        {"finding": "unclassified_model", "subject": "bge-m3", "expires": "2099-01-01"},
        {"finding": "unclassified_model", "subject": "chat-large", "expires": "2020-01-01"}]}
    km.write_file(str(gconfig), "governance", yaml.safe_dump(document).encode(),
                  expected_revision=_revision(gconfig))
    data = _as(client, "admin").get(_GOV).json()
    found = {f["target"]: f for f in data["findings"] if f["code"] == "unclassified_model"}
    assert found["bge-m3"]["exception"]["expires"] == "2099-01-01"
    assert "exception" not in found["chat-large"]
    assert [x["expired"] for x in data["exceptions"]] == [False, True]


def test_every_administrator_may_write_governance_yaml(
    client: TestClient, gconfig: Path
) -> None:
    res = _as(client, "admin").put(
        f"{_BASE}/files/governance",
        json={"revision": _revision(gconfig), "content": "models:\n  a: {tier: local}\n"},
        headers=_HEADERS,
    )
    assert res.status_code == 202


def test_the_file_editor_checks_governance_yaml(client: TestClient, gconfig: Path) -> None:
    res = _as(client, "admin").put(
        f"{_BASE}/files/governance",
        json={"revision": _revision(gconfig), "content": "models:\n  a: {tier: cloud}\n"},
        headers=_HEADERS,
    )
    assert res.status_code == 422 and "tier" in res.json()["detail"]["message"]


@pytest.mark.parametrize(
    ("content", "fragment"),
    [
        ("models:\n  a: {tier: cloud}\n", "tier"),
        ("models:\n  a: {role: chat}\n", "needs a tier"),
        ("sources:\n  wiki.main: {classification: secret}\n", "classification"),
        ("sources:\n  wiki.main: {personal_data: lots}\n", "personal_data"),
        ("sources:\n  wiki.main: {data_subjects: [aliens]}\n", "data_subjects"),
        ("sources:\n  wiki.main: {data_subjects: employees}\n", "list"),
        ("sources:\n  Bad Id: {}\n", "valid entry id"),
        ("policy:\n  allowed_tiers: {internal: [moon]}\n", "tier"),
        ("policy:\n  personal_data_tiers: {secret: [local]}\n", "key"),
        ("policy:\n  broad_roles: user\n", "list"),
        ("exceptions:\n  - {finding: x, subject: y}\n", "expires"),
        ("exceptions:\n  - {finding: x, subject: y, expires: soon}\n", "date"),
        ("exceptions: {}\n", "list"),
        ("sources: []\n", "mapping"),
    ],
)
def test_governance_drafts_are_checked(content: str, fragment: str) -> None:
    problems = km.check("governance", content)
    assert problems and any(fragment in p for p in problems), problems


def test_a_valid_governance_file_and_the_overrides_boundary() -> None:
    valid = yaml.safe_dump({**GOVERNANCE, "exceptions": [
        {"finding": "a", "subject": "b", "expires": "2027-01-01"}]})
    assert km.check("governance", valid) == []
    # unknown keys are the render's to report (low), not a shape error
    assert km.check("governance", "system: {intended_purpose: x}\n") == []
    assert "governance.yaml" in km.check("overrides", "governance: {}\n")[0]
    assert "governance.yaml" in km.check(
        "overrides", "sources:\n  wiki.main: {governance: {owner: x}}\n")[0]


@pytest.mark.parametrize(
    ("host", "provider", "internal"),
    [("localai", "openai", True), ("10.1.2.3", "openai", True), ("127.0.0.1", "", True),
     ("localhost", "", True), ("gpu.internal", "", True), ("", "ollama", True),
     ("", "openai", False), ("api.mammouth.ai", "openai", False), ("8.8.8.8", "", False)],
)
def test_internal_hosts(host: str, provider: str, internal: bool) -> None:
    assert gov.host_is_internal(host, provider) is internal


# ── the fact export ─────────────────────────────────────────────────────────────────────────


def test_the_fact_export_as_json(client: TestClient, gconfig: Path) -> None:
    res = _as(client, "admin").get(f"{_GOV}/export/facts.json")
    assert res.status_code == 200 and res.headers["content-type"].startswith("application/json")
    assert "attachment" in res.headers["content-disposition"]
    data = json.loads(res.text)
    assert data["kind"] == "papaia-knowledge-map-facts" and data["litellm_available"] is True
    wiki = next(e for e in data["entries"] if e["id"] == "wiki.main")
    assert wiki["owner"] == "Teamleitung Service" and wiki["classification"] == "confidential"
    assert wiki["paths"] == ["mcp:MediaWiki", "rag:Qdrant"] and wiki["roles"] == ["staff"]
    assert {m["name"]: m["effective_tier"] for m in data["models"]}["qwen-local"] == "local"
    assert data["agents"][0] == {"id": "agent.service", "name": "Service assistant",
                                 "model": "gpt-4o-mini", "tier": "third_country",
                                 "reaches": ["wiki.main"], "result": "action_needed"}
    codes = {f["code"] for f in data["findings"]}
    assert {"confidential_in_free_chat", "model_tier_contradiction"} <= codes
    assert "knowledge-map.export" in audit_path(str(gconfig)).read_text()


def test_the_fact_export_as_csv(client: TestClient) -> None:
    res = _as(client, "admin").get(f"{_GOV}/export/facts.csv")
    assert res.status_code == 200 and res.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(res.text)))
    wiki = next(r for r in rows if r["id"] == "wiki.main")
    assert wiki["classification"] == "confidential" and wiki["classification_confirmed"] == "yes"
    assert wiki["paths"] == "mcp:MediaWiki; rag:Qdrant"


def test_an_unknown_export_is_a_404(client: TestClient) -> None:
    assert _as(client, "admin").get(f"{_GOV}/export/passwords.txt").status_code == 404


def test_csv_cells_are_not_formulas() -> None:
    assert gov._cell("=HYPERLINK(1)") == "'=HYPERLINK(1)"  # noqa: SLF001
    assert gov._cell(["a", "b"]) == "a; b"  # noqa: SLF001


# ── accepting suggestions that carry text ───────────────────────────────────────────────────


@pytest.mark.parametrize("suggestion", ["aaaaaaaaaaaa", "bbbbbbbbbbbb"])
def test_text_suggestions_need_the_no_personal_data_confirmation(
    client: TestClient, suggestion: str
) -> None:
    admin = _as(client, "admin")
    res = admin.post(f"{_BASE}/suggestions/{suggestion}", json={"decision": "accepted"},
                     headers=_HEADERS)
    assert res.status_code == 400 and "names of persons" in res.json()["detail"]
    res = admin.post(f"{_BASE}/suggestions/{suggestion}",
                     json={"decision": "accepted", "confirm_no_personal_data": True},
                     headers=_HEADERS)
    assert res.status_code == 202


def test_a_declaration_needs_no_confirmation(client: TestClient) -> None:
    res = _as(client, "admin").post(f"{_BASE}/suggestions/cccccccccccc",
                                    json={"decision": "accepted"}, headers=_HEADERS)
    assert res.status_code == 202


# ── the page ────────────────────────────────────────────────────────────────────────────────


def test_the_page_has_the_governance_tab(client: TestClient) -> None:
    page = _as(client, "admin").get("/knowledge-map")
    assert page.status_code == 200
    assert "loadGovernance()" in page.text and "confirm_no_personal_data" in page.text
    assert "export/facts.json" in page.text and "confirm-proposal" in page.text


def test_proposals_and_agent_models_come_from_lints(client: TestClient) -> None:
    data = _as(client, "admin").get(_BASE).json()
    assert {f["category"] for f in data["findings"]} == {"quality", "governance"}
    entries = {e["id"]: e for e in data["entries"]}
    assert entries["agent.service"]["model"]["name"] == "gpt-4o-mini"
    assert entries["mcp.calendar"]["governance_proposal"]["classification"] == "public"
