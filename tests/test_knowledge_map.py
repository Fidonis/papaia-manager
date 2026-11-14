"""The knowledge-map page: reading the rendered map, the operator files, the job that writes
them, and the pass-through to the map server's admin API (a fake behind an httpx transport).
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-km-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-km-workspace-")

for _key, _value in {
    "OIDC_ISSUER_KC_AUTH": "https://kc.test/auth",
    "OIDC_ISSUER_KC_TOKEN": "https://kc.test/token",
    "OIDC_ISSUER_KC_CERTS": "https://kc.test/certs",
    "MANAGER_ADMIN_ROLE": "admin",
    "MANAGER_USER_ROLE": "user",
    "MANAGER_HOST": "http://localhost:8120",
    "MANAGER_OIDC_CLIENT_SECRET": "client-secret",
    "MANAGER_SESSION_SECRET": "test-session-secret-value",
    "PAPAIA_CONFIG_DIR": _CONFIG_DIR,
    "PAPAIA_WORKSPACE_DIR": _WORKSPACE_DIR,
}.items():
    os.environ.setdefault(_key, _value)

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app import main as app_main  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core import knowledge_map as km  # noqa: E402
from app.core.audit import audit_path  # noqa: E402
from app.core.config import service as svc  # noqa: E402
from app.core.ctl import CtlError  # noqa: E402
from app.core.jobs import Job, JobContext, JobQueue  # noqa: E402
from app.core.knowledge_map_jobs import RenderRefusedError, write_callback  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers.knowledge_map_deps import get_knowledge_map_transport  # noqa: E402

_CSRF = "test-csrf-token-value"
_HEADERS = {"X-CSRF-Token": _CSRF}
_BASE = "/api/v1/knowledge-map"
_TOKEN = "admin-token-for-tests"

WIKI = {
    "id": "wiki.main",
    "kind": "source",
    "system": "Company wiki",
    "coverage": "Processes and checklists",
    "topics": [{"id": "org.processes", "label": "Processes"}],
    "example_queries": ["escalation procedure"],
    "access": [
        {"via": "mcp", "target": "MediaWiki", "best_for": "current pages"},
        {"via": "rag", "target": "Qdrant", "collection": "wiki", "required_roles": ["staff"]},
    ],
    "provenance": {"declared_by": "addon:mediawiki"},
}
# What the map server records, as its admin stats and suggestions report it.
TELEMETRY = {"enabled": True, "learning_mode": "aggregate", "retention_days": 30,
             "min_profiles": 3, "min_occurrences": 3}
CALENDAR = {
    "id": "mcp.calendar",
    "kind": "mcp",
    "system": "Calendar",
    "undeclared": True,
    "access": [{"via": "mcp", "target": "Calendar"}],
}


# ── fixtures ────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    (directory / "knowledge-map" / "manifests").mkdir(parents=True)
    (directory / "ai" / "knowledge-map").mkdir(parents=True)
    (directory / ".env").write_text("COMPOSE_PROFILES=librechat,knowledge-map\n", encoding="utf-8")
    (directory / km.MODULE_ENV).write_text(f"KM_ADMIN_TOKEN={_TOKEN}\n", encoding="utf-8")
    for entry in (WIKI, CALENDAR):
        (directory / "knowledge-map" / "manifests" / f"{entry['id']}.yaml").write_text(
            yaml.safe_dump(entry), encoding="utf-8"
        )
    (directory / "knowledge-map" / "lints.yaml").write_text(
        yaml.safe_dump({
            "generated_at": "2026-11-14T10:00:00+00:00",
            "findings": [{"severity": "high", "code": "undeclared_server",
                          "target": "mcp.calendar", "message": "Calendar describes nothing"}],
            "warnings": [],
        }),
        encoding="utf-8",
    )
    return directory


class FakeMapServer:
    """The admin API of the map server, as far as the page uses it."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, Any]] = []
        self.decisions: dict[str, str] = {}
        self.suggestions = [
            {"id": "aaaaaaaaaaaa", "kind": "example_query", "target": "wiki.main",
             "title": "Add an example", "evidence": {"occurrences": 6},
             "patch": {"example_queries": ["escalation procedure", "who is on call"]}},
            {"id": "bbbbbbbbbbbb", "kind": "gold_question", "target": None,
             "title": "Keep as gold", "gold": {"q": "weather in Lisbon", "kind": "abstention"}},
            {"id": "cccccccccccc", "kind": "declare", "target": "mcp.calendar",
             "title": "Describe the calendar",
             "declaration": {"sources": [{"id": "calendar.team", "system": "Calendar",
                                          "coverage": "x",
                                          "access": [{"via": "mcp", "target": "Calendar"}]}]}},
        ]

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.headers.get("Authorization") != f"Bearer {_TOKEN}":
            return httpx.Response(401, json={"error": "unauthorized"})
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.method, request.url.path, body))
        path = request.url.path
        if path == "/admin/api/map":
            return httpx.Response(
                200, json={"entries": [WIKI], "settings": {"embedder": "lexical"}}
            )
        if path == "/admin/api/simulate":
            return httpx.Response(200, json={"abstention": False, "results": [
                {"source_id": "wiki.main", "system": "Company wiki", "confidence": 0.8}]})
        if path == "/admin/api/suggestions":
            open_ = [s for s in self.suggestions if s["id"] not in self.decisions]
            return httpx.Response(200, json={"suggestions": open_, "telemetry": TELEMETRY})
        if path.startswith("/admin/api/suggestions/"):
            self.decisions[path.split("/")[4]] = body["decision"]
            return httpx.Response(200, json={"recorded": True})
        if path == "/admin/api/stats":
            return httpx.Response(200, json={
                "totals": {"requests": 12, "abstention_rate": 0.25},
                "top_sources": {"wiki.main": 9}, "unmatched": [],
                "telemetry": TELEMETRY})
        if path == "/admin/api/eval":
            return httpx.Response(200, json={"all": {"hit_at_1": 1.0}})
        return httpx.Response(404, json={"error": "not_found"})


@pytest.fixture
def server() -> FakeMapServer:
    return FakeMapServer()


@pytest.fixture
def queue(config_dir: Path) -> Iterator[JobQueue]:
    app_main._job_queue = JobQueue(config_dir=str(config_dir))  # noqa: SLF001
    yield app_main._job_queue  # noqa: SLF001
    app_main._job_queue = None  # noqa: SLF001


@pytest.fixture
def client(config_dir: Path, server: FakeMapServer, queue: JobQueue) -> Iterator[TestClient]:
    get_settings.cache_clear()
    settings = get_settings().model_copy(update={"papaia_config_dir": str(config_dir)})
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_knowledge_map_transport] = lambda: httpx.MockTransport(
        server.handler
    )
    yield TestClient(app, follow_redirects=False)
    get_settings.cache_clear()


def _as(client: TestClient, *roles: str) -> TestClient:
    session = {
        "user": {"sub": "u-1", "preferred_username": "admin", "roles": list(roles),
                 "exp": int(time.time()) + 3600},
        "_csrf_token": _CSRF,
    }
    payload = base64.b64encode(json.dumps(session).encode())
    signed = TimestampSigner(get_settings().manager_session_secret).sign(payload).decode()
    client.cookies.clear()
    client.cookies.set("papaia_manager_session", signed)
    return client


# ── who may call it ─────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "path", [_BASE, f"{_BASE}/server", f"{_BASE}/suggestions", "/knowledge-map"]
)
def test_only_administrators_may_look(client: TestClient, path: str) -> None:
    assert _as(client, "user").get(path).status_code == 403


def test_nothing_changes_without_the_csrf_token(client: TestClient) -> None:
    admin = _as(client, "admin")
    assert admin.put(f"{_BASE}/files/overrides", json={"content": ""}).status_code == 403
    assert admin.post(f"{_BASE}/simulate", json={"need": "x"}).status_code == 403


# ── reading ─────────────────────────────────────────────────────────────────────────────────


def test_the_overview_reads_the_rendered_map_from_disk(client: TestClient) -> None:
    data = _as(client, "admin").get(_BASE).json()
    assert data["active"] is True and data["admin_api"] is True
    entries = {e["id"]: e for e in data["entries"]}
    assert [p["via"] for p in entries["wiki.main"]["access"]] == ["mcp", "rag"]
    assert entries["wiki.main"]["declared_by"] == "addon:mediawiki"
    assert entries["mcp.calendar"]["undeclared"] is True
    assert data["findings"][0]["code"] == "undeclared_server"
    assert [f["key"] for f in data["files"]] == ["sources", "overrides", "goldset", "governance"]


def test_legacy_single_path_entries_read_as_one_path(tmp_path: Path) -> None:
    (tmp_path / "knowledge-map" / "manifests").mkdir(parents=True)
    (tmp_path / "knowledge-map" / "manifests" / "crm.main.yaml").write_text(
        yaml.safe_dump({"id": "crm.main", "access": {"mcp_server": "crm", "key_tools": ["find"]}}),
        encoding="utf-8",
    )
    (entry,) = km.rendered(str(tmp_path))["entries"]
    assert entry["access"] == [{"via": "mcp", "target": "crm", "key_tools": ["find"]}]


def test_the_page_and_sidebar_follow_the_profile(client: TestClient, config_dir: Path) -> None:
    page = _as(client, "admin").get("/knowledge-map")
    assert page.status_code == 200 and 'href="/knowledge-map"' in page.text
    (config_dir / ".env").write_text("COMPOSE_PROFILES=librechat\n", encoding="utf-8")
    page = _as(client, "admin").get("/knowledge-map")
    assert 'href="/knowledge-map"' not in page.text
    assert "papaia-ctl setup" in page.text


def test_a_generated_placeholder_is_no_token(config_dir: Path) -> None:
    (config_dir / km.MODULE_ENV).write_text("KM_ADMIN_TOKEN=GENERATE_KM_ADMIN_TOKEN\n")
    assert km.admin_token(str(config_dir)) == ""


# ── the map server ──────────────────────────────────────────────────────────────────────────


def test_simulate_passes_the_role_set_through(client: TestClient, server: FakeMapServer) -> None:
    res = _as(client, "admin").post(
        f"{_BASE}/simulate", json={"need": "escalation", "roles": ["staff"]}, headers=_HEADERS
    )
    assert res.json()["results"][0]["source_id"] == "wiki.main"
    assert server.calls[-1] == ("POST", "/admin/api/simulate",
                                {"need": "escalation", "roles": ["staff"]})


def test_an_unreachable_or_refusing_server_is_a_503(client: TestClient, config_dir: Path) -> None:
    (config_dir / km.MODULE_ENV).write_text("KM_ADMIN_TOKEN=wrong\n")
    res = _as(client, "admin").get(f"{_BASE}/server")
    assert res.status_code == 503 and "refused" in res.json()["detail"]
    (config_dir / km.MODULE_ENV).write_text("")
    res = _as(client, "admin").get(f"{_BASE}/server")
    assert res.status_code == 503 and "KM_ADMIN_TOKEN" in res.json()["detail"]


def test_rejecting_tells_the_server_and_audits(
    client: TestClient, server: FakeMapServer, config_dir: Path
) -> None:
    res = _as(client, "admin").post(
        f"{_BASE}/suggestions/aaaaaaaaaaaa", json={"decision": "rejected"}, headers=_HEADERS
    )
    assert res.json() == {"status": "rejected"}
    assert server.decisions == {"aaaaaaaaaaaa": "rejected"}
    assert "knowledge-map.suggestion.reject" in audit_path(str(config_dir)).read_text()


def test_accepting_queues_a_write_of_the_servers_version(
    client: TestClient, queue: JobQueue
) -> None:
    res = _as(client, "admin").post(
        f"{_BASE}/suggestions/aaaaaaaaaaaa",
        json={"decision": "accepted", "confirm_no_personal_data": True},
        headers=_HEADERS,
    )
    assert res.status_code == 202 and res.json()["file"] == "overrides"
    job = queue.get_job(res.json()["job_id"])
    assert job is not None and job.params["suggestion"] == "aaaaaaaaaaaa"


def test_an_unknown_suggestion_is_a_404(client: TestClient) -> None:
    res = _as(client, "admin").post(
        f"{_BASE}/suggestions/dddddddddddd", json={"decision": "accepted"}, headers=_HEADERS
    )
    assert res.status_code == 404


# ── the operator files ──────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("key", "content", "fragment"),
    [
        ("sources", "sources:\n  - {id: Bad Id, system: x, coverage: y}\n", "valid id"),
        ("sources", "sources:\n  - {id: a.b, kind: oracle, system: x, coverage: y}\n", "kind"),
        ("sources", "sources:\n  - {id: a.b, kind: reference, system: x, coverage: y}\n",
         "location"),
        ("sources", "sources:\n  - {id: a.b, system: x, coverage: y, access: [{via: rag}]}\n",
         "target"),
        ("overrides", "sources:\n  wiki.main: {id: other}\n", "id"),
        ("goldset", "questions:\n  - {q: hello, kind: single}\n", "expected"),
        ("goldset", "- just a list\n", "mapping"),
    ],
)
def test_drafts_are_checked_before_they_are_written(key: str, content: str, fragment: str) -> None:
    problems = km.check(key, content)
    assert problems and fragment in problems[0]


def test_a_valid_draft_has_no_problems() -> None:
    assert km.check("sources", yaml.safe_dump({"sources": [{
        "id": "files.protocols", "kind": "reference", "system": "Share",
        "coverage": "PDF protocols", "location": "Nextcloud/04"}]})) == []
    assert km.check("goldset", "questions:\n  - {q: weather, kind: abstention}\n") == []


def test_writing_a_file_queues_a_job_and_refuses_a_stale_revision(
    client: TestClient, config_dir: Path
) -> None:
    admin = _as(client, "admin")
    res = admin.put(f"{_BASE}/files/goldset", json={"content": "questions: []\n", "revision": ""},
                    headers=_HEADERS)
    assert res.status_code == 202
    km.write_file(str(config_dir), "goldset", b"questions: []\n", expected_revision="")
    res = admin.put(f"{_BASE}/files/goldset", json={"content": "questions: []\n", "revision": ""},
                    headers=_HEADERS)
    assert res.status_code == 409
    res = admin.put(f"{_BASE}/files/goldset", json={"content": "- x\n", "revision": ""},
                    headers=_HEADERS)
    assert res.status_code == 422


def test_writes_keep_the_previous_version(config_dir: Path) -> None:
    config = str(config_dir)
    first = km.write_file(config, "overrides", b"sources: {}\n", expected_revision="")
    km.write_file(config, "overrides", b"sources: {a.b: {}}\n", expected_revision=first.revision)
    (backup,) = (config_dir / km.HISTORY_DIR / "overrides").glob("*.bak")
    assert backup.read_bytes() == b"sources: {}\n"
    with pytest.raises(km.KnowledgeMapError):
        km.write_file(config, "overrides", b"x: 1\n", expected_revision=first.revision)


# ── what accepting writes ───────────────────────────────────────────────────────────────────


def test_a_patch_merges_into_existing_overrides(config_dir: Path, server: FakeMapServer) -> None:
    config = str(config_dir)
    km.write_file(config, "overrides",
                  b"sources:\n  wiki.main:\n    owner: ops\n", expected_revision="")
    key, content, revision = km.suggestion_change(config, server.suggestions[0])
    assert key == "overrides" and revision == km.read_file(config, "overrides").revision
    assert yaml.safe_load(content)["sources"]["wiki.main"] == {
        "owner": "ops", "example_queries": ["escalation procedure", "who is on call"]}


def test_a_gold_question_is_appended_once(config_dir: Path, server: FakeMapServer) -> None:
    config = str(config_dir)
    key, content, _ = km.suggestion_change(config, server.suggestions[1])
    km.write_file(config, key, content, expected_revision="")
    _, again, _ = km.suggestion_change(config, server.suggestions[1])
    assert yaml.safe_load(again)["questions"] == [{"q": "weather in Lisbon", "kind": "abstention"}]


def test_a_declaration_goes_into_the_own_entries(config_dir: Path, server: FakeMapServer) -> None:
    key, content, _ = km.suggestion_change(str(config_dir), server.suggestions[2])
    assert key == "sources"
    assert yaml.safe_load(content)["sources"][0]["id"] == "calendar.team"


# ── the job ─────────────────────────────────────────────────────────────────────────────────


class _Verbs:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.fail = 0

    async def verb(self, **kwargs: Any) -> AsyncGenerator[str, None]:
        self.calls.append([kwargs["verb"], *kwargs["extra_flags"]])
        failing = self.fail > 0
        if failing:
            self.fail -= 1

        async def lines() -> AsyncGenerator[str, None]:
            yield "rendering"
            if failing:
                yield "knowledge map: duplicate id 'wiki.main'"
                raise CtlError("start failed", exit_code=3)

        return lines()


@pytest.fixture
def verbs(monkeypatch: pytest.MonkeyPatch) -> _Verbs:
    recorder = _Verbs()
    monkeypatch.setattr(svc, "run_core_verb", recorder.verb)
    return recorder


def _ctx(tmp_path: Path) -> JobContext:
    from datetime import UTC, datetime  # noqa: PLC0415

    job = Job(id="job-1", action="x", target="y", user="admin", created_at=datetime.now(UTC))
    return JobContext(job=job, log_path=tmp_path / "job.log")


async def test_the_job_writes_renders_and_tells_the_server(
    config_dir: Path, verbs: _Verbs, tmp_path: Path
) -> None:
    told: list[bool] = []

    async def after() -> None:
        told.append(True)

    callback = write_callback(
        key="overrides", content=b"sources: {}\n", expected_revision="", user="admin",
        workspace_dir=str(tmp_path), config_dir=str(config_dir), reason="edited", after=after,
    )
    await callback(_ctx(tmp_path))
    assert verbs.calls == [["start", "--profiles=knowledge-map"]]
    assert told == [True]
    assert km.read_file(str(config_dir), "overrides").raw == b"sources: {}\n"
    assert '"rendered": true' in audit_path(str(config_dir)).read_text()


async def test_a_refused_render_puts_the_previous_file_back(
    config_dir: Path, verbs: _Verbs, tmp_path: Path
) -> None:
    config = str(config_dir)
    before = km.write_file(config, "sources", b"sources: []\n", expected_revision="")
    verbs.fail = 1
    callback = write_callback(
        key="sources", content=b"sources: [{id: wiki.main}]\n", expected_revision=before.revision,
        user="admin", workspace_dir=str(tmp_path), config_dir=config, reason="edited",
    )
    with pytest.raises(RenderRefusedError):
        await callback(_ctx(tmp_path))
    assert km.read_file(config, "sources").raw == b"sources: []\n"
    assert len(verbs.calls) == 2  # the failed render, then the one with the old file


async def test_the_gold_set_is_written_without_a_render(
    config_dir: Path, verbs: _Verbs, tmp_path: Path
) -> None:
    callback = write_callback(
        key="goldset", content=b"questions: []\n", expected_revision="", user="admin",
        workspace_dir=str(tmp_path), config_dir=str(config_dir), reason="edited",
    )
    await callback(_ctx(tmp_path))
    assert verbs.calls == []
