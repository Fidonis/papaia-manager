"""The inventory of work objects ("Bestand", governance GV-16, GV-17, GV-21; OPS-24).

The manager asks every module for its part at `GET /internal/inventory` on each request and
keeps no copy. A module that does not answer, or answers with something that is not its part
of `papaia.inventory/1`, is listed as `nicht erreichbar` instead of silently missing. Page,
list and export need the realm role `papaia-auditor`; every export, a refused one too, is one
`inventory_exported` row of `papaia.audit/1`.
"""
from __future__ import annotations

import base64
import copy
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

for _key, _value in {
    "OIDC_ISSUER_KC_AUTH": "https://kc.test/auth",
    "OIDC_ISSUER_KC_TOKEN": "https://kc.test/token",
    "OIDC_ISSUER_KC_CERTS": "https://kc.test/certs",
    "MANAGER_ADMIN_ROLE": "admin",
    "MANAGER_USER_ROLE": "user",
    "MANAGER_HOST": "http://localhost:8120",
    "MANAGER_OIDC_CLIENT_SECRET": "client-secret",
    "MANAGER_SESSION_SECRET": "test-session-secret-value",
    "PAPAIA_CONFIG_DIR": tempfile.mkdtemp(prefix="papaia-inventory-config-"),
    "PAPAIA_WORKSPACE_DIR": tempfile.mkdtemp(prefix="papaia-inventory-workspace-"),
}.items():
    os.environ.setdefault(_key, _value)

import jsonschema  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402
from papaia_kit.audit import validate_event  # noqa: E402
from pydantic import ValidationError  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402
from app.core import kit_audit, work_inventory  # noqa: E402
from app.core.audit import audit_path  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers.api_inventory import get_inventory_fetch  # noqa: E402

_CSRF = "test-csrf-token-value"
_ANNA = "5b0e8f0a-2c51-4d7e-9a43-1f6c2d8e9b10"
_BEN = "c2d4f6a8-0b1c-4e3d-8f7a-6b5c4d3e2f10"
_DORA = "e1f2a3b4-c5d6-4e7f-8a9b-0c1d2e3f4a5b"
_SOURCES = (
    "studio=https://studio.test:8443,flow=https://flow.test:8443,"
    "data=https://data.test:8443,assist=https://assist.test:8443"
)
_API = "/api/v1/inventory"
_EXPORT = "/api/v1/inventory/export"


def _part(module: str, items: list[dict[str, Any]]) -> dict[str, Any]:
    """One module's answer: its part of `papaia.inventory/1`, checksum as the contract says."""
    body = {
        "format": "papaia.inventory/1",
        "generated_on": "2026-11-20",
        "sources": [{"module": module, "status": "erreichbar"}],
        "items": sorted(items, key=lambda item: item["id"].encode()),
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return {**body, "checksum": "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()}


def _app_item(object_id: str, title: str, **extra: Any) -> dict[str, Any]:
    item: dict[str, Any] = {
        "id": f"studio:app:{object_id}",
        "kind": "app",
        "module": "studio",
        "title": title,
        "version": "2",
        "state": "privat",
        "owner_sub": _BEN,
        "shared_with": {"groups": [], "persons": 0},
        "catalog_entries": [],
        "max_data_class": "employee",
        "service_accounts": [],
        "created_at": "2026-11-01",
        "flags": ["deputy_missing"],
    }
    item.update(extra)
    return item


_WORKFLOW = {
    "id": "flow:workflow:invoice-intake",
    "kind": "workflow",
    "module": "flow",
    "title": "Eingangsrechnungen ablegen",
    "version": "4",
    "state": "aktiv",
    "owner_sub": _ANNA,
    "deputy_sub": _BEN,
    "approver_sub": _DORA,
    "approved_at": "2026-09-30",
    "shared_with": {"groups": ["buchhaltung"], "persons": 0},
    "catalog_entries": [
        {"catalog_id": "paperless", "operations": ["get_document", "update_document"],
         "effect": "write"}
    ],
    "max_data_class": "confidential",
    "service_accounts": [{"name": "svc-flow-paperless", "audience": "mcp-paperless"}],
    "created_at": "2026-03-14",
    "last_used_at": "2026-11-20",
    "confirmed_at": "2026-03-20",
    "flags": [],
}

_METRIC = {
    "id": "data:metric:open-invoices",
    "kind": "metric",
    "module": "data",
    "title": "Offene Rechnungen",
    "version": "1",
    "state": "geprüft",
    "owner_sub": _DORA,
    "deputy_sub": _ANNA,
    "shared_with": {"groups": ["leitung"], "persons": 2},
    "catalog_entries": [
        {"catalog_id": "mssql", "operations": ["query"], "effect": "read"}
    ],
    "max_data_class": "internal",
    "service_accounts": [],
    "created_at": "2025-01-10",
    "last_used_at": "2025-03-01",
    "confirmed_at": "2025-01-12",
    "flags": ["unused_180d", "confirmation_due"],
}

# The text of a reminder is personal content: it must never reach the inventory.
_REMINDER_TEXT = "Erinnere mich an den Arzttermin"
_REMINDER = {
    "id": "assist:event_reminder:7f3c2a",
    "kind": "event_reminder",
    "module": "assist",
    "purpose": "assist.event",
    "state": "aktiv",
    "owner_sub": _ANNA,
    "catalog_entries": [
        {"catalog_id": "mail", "operations": ["send_message"], "effect": "send"}
    ],
    "max_data_class": "personal",
    "service_accounts": [],
    "created_at": "2026-11-02",
    "last_used_at": "2026-11-19",
    "flags": [],
}


def _answers() -> dict[str, bytes | Exception]:
    return {
        "studio": json.dumps(_part("studio", [
            _app_item("holiday-planner", "Urlaubsplanung"),
            _app_item("formula", "=HYPERLINK(\"https://evil.test\")"),
        ])).encode(),
        "flow": json.dumps(_part("flow", [_WORKFLOW])).encode(),
        "data": json.dumps(_part("data", [_METRIC])).encode(),
        "assist": json.dumps(_part("assist", [_REMINDER])).encode(),
    }


class FakeModules:
    """The four modules' `/internal/inventory`, answering from a table; records each call."""

    def __init__(self) -> None:
        self.answers: dict[str, bytes | Exception] = _answers()
        self.calls: list[tuple[str, str]] = []

    def fetch(self, module: str, url: str) -> bytes:
        self.calls.append((module, url))
        answer = self.answers.get(module)
        if answer is None or isinstance(answer, Exception):
            raise work_inventory.SourceUnavailable(module)
        return answer


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    (directory / "manager").mkdir(parents=True)
    return directory


@pytest.fixture
def modules() -> FakeModules:
    return FakeModules()


@pytest.fixture
def client(config_dir: Path, modules: FakeModules) -> Iterator[TestClient]:
    get_settings.cache_clear()
    settings = get_settings().model_copy(
        update={
            "papaia_config_dir": str(config_dir),
            "inventory_sources": _SOURCES,
            "manager_auditor_role": "papaia-auditor",
        }
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_inventory_fetch] = lambda: modules.fetch
    yield TestClient(app, follow_redirects=False)
    get_settings.cache_clear()


def _as(client: TestClient, *roles: str, language: str = "de") -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": _DORA,
            "preferred_username": "dora",
            "roles": list(roles),
            "exp": int(time.time()) + 3600,
        },
        "_csrf_token": _CSRF,
    }
    payload = base64.b64encode(json.dumps(session).encode())
    signed = TimestampSigner(get_settings().manager_session_secret).sign(payload).decode()
    client.cookies.clear()
    client.cookies.set("papaia_manager_session", signed)
    client.headers["Accept-Language"] = language
    return client


def _auditor(client: TestClient, language: str = "de") -> TestClient:
    return _as(client, "papaia-auditor", language=language)


def _outbox(config_dir: Path) -> list[dict[str, Any]]:
    path = kit_audit.outbox_path(str(config_dir))
    if not path.exists():
        return []
    with sqlite3.connect(path) as conn:
        found = conn.execute("SELECT body FROM audit_outbox ORDER BY source_seq").fetchall()
    return [json.loads(body) for (body,) in found]


def _log(config_dir: Path) -> list[dict[str, Any]]:
    path = audit_path(str(config_dir))
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _schema() -> dict[str, Any]:
    return work_inventory.schema()


# -- rights --------------------------------------------------------------------------------


@pytest.mark.parametrize("roles", [("admin",), ("user",), ("papaia-admin", "admin"), ()])
@pytest.mark.parametrize("path", ["/inventory", _API, f"{_EXPORT}?format=json"])
def test_every_surface_needs_the_auditor_role(
    client: TestClient, modules: FakeModules, roles: tuple[str, ...], path: str
) -> None:
    response = _as(client, *roles).get(path)
    assert response.status_code == 403
    assert modules.calls == []


def test_a_refused_export_is_an_audit_event(client: TestClient, config_dir: Path) -> None:
    assert _as(client, "papaia-admin", "admin").get(f"{_EXPORT}?format=csv").status_code == 403
    rows = _outbox(config_dir)
    assert len(rows) == 1
    row = rows[0]
    validate_event(row)
    assert row["event"] == "inventory_exported"
    assert row["result"] == "denied"
    assert row["error_code"] == "denied"
    assert row["sub"] == _DORA
    assert _log(config_dir)[-1]["result"] == "denied"


def test_without_a_session_the_page_asks_for_sign_in(client: TestClient) -> None:
    client.cookies.clear()
    response = client.get("/inventory")
    assert response.status_code == 307
    assert response.headers["location"].startswith("/auth/login")


def test_the_auditor_may_sign_in_and_sees_the_inventory_entry(client: TestClient) -> None:
    page = _auditor(client).get("/inventory")
    assert page.status_code == 200
    assert 'href="/inventory"' in page.text
    other = _as(client, "admin").get("/")
    assert 'href="/inventory"' not in other.text


@pytest.mark.parametrize("path", ["/", "/me"])
def test_the_auditor_reaches_the_surfaces_of_a_user(client: TestClient, path: str) -> None:
    assert _auditor(client).get(path).status_code == 200


@pytest.mark.parametrize(
    "path", ["/addons", "/backup", "/catalogs", "/jobs", "/services", "/upgrade", "/users"]
)
def test_the_auditor_reaches_no_administration_page(client: TestClient, path: str) -> None:
    assert _auditor(client).get(path).status_code == 403


# -- collecting ----------------------------------------------------------------------------


def test_the_list_merges_every_module_into_one_valid_document(
    client: TestClient, modules: FakeModules
) -> None:
    doc = _auditor(client).get(_API).json()
    jsonschema.validate(doc, _schema())
    assert doc["format"] == "papaia.inventory/1"
    assert doc["sources"] == [
        {"module": "assist", "status": "erreichbar"},
        {"module": "data", "status": "erreichbar"},
        {"module": "flow", "status": "erreichbar"},
        {"module": "studio", "status": "erreichbar"},
    ]
    ids = [item["id"] for item in doc["items"]]
    assert ids == sorted(ids, key=str.encode)
    assert len(ids) == 5
    body = {k: v for k, v in doc.items() if k != "checksum"}
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    assert doc["checksum"] == "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()
    assert sorted(module for module, _url in modules.calls) == ["assist", "data", "flow", "studio"]
    assert ("flow", "https://flow.test:8443/internal/inventory") in modules.calls


def test_the_list_is_the_same_on_every_call(client: TestClient) -> None:
    first = _auditor(client).get(_API).json()
    second = _auditor(client).get(_API).json()
    assert first["checksum"] == second["checksum"]


def test_reading_the_list_writes_nothing(client: TestClient, config_dir: Path) -> None:
    assert _auditor(client).get(_API).status_code == 200
    assert _auditor(client).get("/inventory").status_code == 200
    assert _outbox(config_dir) == []


def test_an_unreachable_module_is_listed_not_left_out(
    client: TestClient, modules: FakeModules
) -> None:
    modules.answers["flow"] = work_inventory.SourceUnavailable("flow")
    doc = _auditor(client).get(_API).json()
    jsonschema.validate(doc, _schema())
    assert {"module": "flow", "status": "nicht erreichbar"} in doc["sources"]
    assert not [item for item in doc["items"] if item["module"] == "flow"]


@pytest.mark.parametrize(
    "broken",
    [
        b"not json",
        b"[]",
        json.dumps({**_part("flow", [_WORKFLOW]), "extra": 1}).encode(),
        json.dumps(_part("flow", [{**_WORKFLOW, "content": "rows"}])).encode(),
        # another module's object in flow's answer
        json.dumps(_part("flow", [_WORKFLOW, _METRIC])).encode(),
        json.dumps(_part("flow", [_WORKFLOW, _WORKFLOW])).encode(),
        json.dumps({**_part("flow", [_WORKFLOW]), "checksum": "sha256:" + "0" * 64}).encode(),
    ],
)
def test_an_invalid_answer_counts_as_unreachable(
    client: TestClient, modules: FakeModules, broken: bytes
) -> None:
    modules.answers["flow"] = broken
    doc = _auditor(client).get(_API).json()
    assert {"module": "flow", "status": "nicht erreichbar"} in doc["sources"]
    assert [item["module"] for item in doc["items"]].count("flow") == 0
    assert "rows" not in json.dumps(doc)


def test_a_module_installed_but_without_an_address_is_unreachable(
    config_dir: Path, modules: FakeModules
) -> None:
    settings = get_settings().model_copy(
        update={"papaia_config_dir": str(config_dir), "inventory_sources": "flow=https://f.test"}
    )
    doc = work_inventory.collect(
        settings.inventory_source_map, modules.fetch, also_ask=("studio",)
    )
    assert doc["sources"] == [
        {"module": "flow", "status": "erreichbar"},
        {"module": "studio", "status": "nicht erreichbar"},
    ]


def test_no_reminder_text_reaches_the_inventory(client: TestClient, modules: FakeModules) -> None:
    leaked = copy.deepcopy(_REMINDER)
    leaked["title"] = _REMINDER_TEXT
    modules.answers["assist"] = json.dumps(_part("assist", [leaked])).encode()
    for path in (_API, "/inventory", f"{_EXPORT}?format=json", f"{_EXPORT}?format=csv"):
        assert _REMINDER_TEXT not in _auditor(client).get(path).text


# -- the page ------------------------------------------------------------------------------


def test_the_page_shows_the_columns_and_objects(client: TestClient) -> None:
    text = _auditor(client).get("/inventory").text
    assert "<h1" in text and "Bestand" in text
    for column in ("Titel", "Art", "Modul", "Eigentümerin", "Vertretung", "Zustand", "Klasse",
                   "Wirkung", "zuletzt genutzt", "Kennzeichen"):
        assert f">{column}<" in text
    assert "Eingangsrechnungen ablegen" in text
    assert "Urlaubsplanung" in text
    assert 'href="/api/v1/inventory/export?format=csv' in text
    assert 'href="/api/v1/inventory/export?format=json' in text


def test_the_page_names_a_module_that_does_not_answer(
    client: TestClient, modules: FakeModules
) -> None:
    modules.answers["flow"] = work_inventory.SourceUnavailable("flow")
    text = _auditor(client).get("/inventory").text
    assert (
        "Der Bestand ist unvollständig: Flow antwortet nicht. Objekte aus Flow fehlen in dieser "
        "Liste und im Export. Prüfen Sie den Zustand unter „Betrieb“ und laden Sie die Seite "
        "danach neu."
    ) in text


def test_the_unreachable_message_in_english(client: TestClient, modules: FakeModules) -> None:
    modules.answers["data"] = work_inventory.SourceUnavailable("data")
    text = _auditor(client, language="en").get("/inventory").text
    assert "The inventory is incomplete: Data does not answer." in text


def test_the_page_says_when_there_is_nothing(client: TestClient, modules: FakeModules) -> None:
    for module in ("studio", "flow", "data", "assist"):
        modules.answers[module] = json.dumps(_part(module, [])).encode()
    text = _auditor(client).get("/inventory").text
    assert "Noch keine Apps, Abläufe, Kennzahlen oder Anlass-Erinnerungen vorhanden." in text


def test_a_filter_without_a_match_is_not_the_empty_state(client: TestClient) -> None:
    text = _auditor(client).get("/inventory?module=flow&flag=unused_180d").text
    assert "Noch keine Apps" not in text
    assert "Eingangsrechnungen ablegen" not in text


def _main(text: str) -> str:
    """The page's own part: from its subtitle to the end of the content (the frame excluded)."""
    return text[text.index('<p class="pk-lead">') : text.index("</main>")]


def test_the_page_is_built_from_the_kit_components(
    client: TestClient, modules: FakeModules
) -> None:
    """Kit 0.1.0 patterns: table, badge, alert, form fields and buttons; no own markup rules."""
    modules.answers["flow"] = work_inventory.SourceUnavailable("flow")
    main = _main(_auditor(client).get("/inventory").text)
    assert '<table class="pk-table">' in main
    assert 'class="pk-table-scroll"' in main
    assert 'class="pk-alert" role="status" data-pk-tone="warning"' in main
    assert 'class="pk-badge" data-pk-tone="warning"' in main
    assert 'class="pk-field"' in main and '<label for="inventory-filter-flag">' in main
    assert 'class="pk-btn" data-pk-variant="secondary"' in main
    # Content security policy: nothing inline (MGR-003 to MGR-028).
    assert "<script" not in main
    assert " style=" not in main
    assert not re.search(r"\son[a-z]+=", main)
    # The own utility classes of the pages before the kit are gone from this page.
    for daisy in ("btn-outline", "select-bordered", "input-bordered", "badge-warning",
                  "alert-warning", "table-sm"):
        assert daisy not in main


def test_the_empty_state_is_the_kit_empty_pattern(
    client: TestClient, modules: FakeModules
) -> None:
    for module in ("studio", "flow", "data", "assist"):
        modules.answers[module] = json.dumps(_part(module, [])).encode()
    main = _main(_auditor(client).get("/inventory").text)
    assert 'class="pk-empty" data-pk-state="empty"' in main
    assert "<table" not in main


# -- filters -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ("module=flow", ["flow:workflow:invoice-intake"]),
        ("flag=confirmation_due", ["data:metric:open-invoices"]),
        ("data_class=personal", ["assist:event_reminder:7f3c2a"]),
        ("effect=write", ["flow:workflow:invoice-intake"]),
        ("effect=send", ["assist:event_reminder:7f3c2a"]),
        (f"person={_ANNA}", [
            "assist:event_reminder:7f3c2a",
            "data:metric:open-invoices",
            "flow:workflow:invoice-intake",
        ]),
        (f"person={_DORA}&module=data", ["data:metric:open-invoices"]),
    ],
)
def test_filters(client: TestClient, query: str, expected: list[str]) -> None:
    doc = _auditor(client).get(f"{_API}?{query}").json()
    assert [item["id"] for item in doc["items"]] == expected
    jsonschema.validate(doc, _schema())


@pytest.mark.parametrize(
    "query",
    [
        "module=crm",
        "flag=secret",
        "data_class=top",
        "effect=execute",
        "person=" + "x" * 300,
        "person=a%20b",
        "module=flow&module=data",
        "flag=&flag=owner_inactive",
        "flag=owner_inactive&flag=",
        "person=&person=" + "u" * 8,
        "unknown=1",
    ],
)
def test_an_invalid_filter_is_refused(
    client: TestClient, modules: FakeModules, query: str
) -> None:
    assert _auditor(client).get(f"{_API}?{query}").status_code == 400
    assert _auditor(client).get(f"/inventory?{query}").status_code == 400
    assert modules.calls == []


# -- export --------------------------------------------------------------------------------


def test_the_json_export_is_the_document_and_an_audit_event(
    client: TestClient, config_dir: Path
) -> None:
    response = _auditor(client).get(f"{_EXPORT}?format=json&module=flow")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["content-disposition"].startswith('attachment; filename="inventory-')
    doc = response.json()
    jsonschema.validate(doc, _schema())
    assert [item["id"] for item in doc["items"]] == ["flow:workflow:invoice-intake"]
    rows = _outbox(config_dir)
    assert len(rows) == 1
    row = rows[0]
    validate_event(row)
    assert row["event"] == "inventory_exported"
    assert row["result"] == "allowed"
    assert row["operation"] == "inventory.export"
    assert row["object_ref"] == {"type": "inventory", "id": doc["checksum"]}
    assert row["sub"] == _DORA
    assert response.headers["x-papaia-correlation-id"] == row["correlation_id"]
    entry = _log(config_dir)[-1]
    assert entry["action"] == "inventory.export"
    assert entry["params"] == {
        "format": "json",
        "items": 1,
        "filters": {"module": "flow"},
        "unreachable": [],
    }


def test_the_csv_export(client: TestClient, modules: FakeModules, config_dir: Path) -> None:
    modules.answers["data"] = work_inventory.SourceUnavailable("data")
    response = _auditor(client).get(f"{_EXPORT}?format=csv")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(response.text)))
    assert list(rows[0]) == list(work_inventory.CSV_COLUMNS)
    by_id = {row["id"]: row for row in rows}
    flow = by_id["flow:workflow:invoice-intake"]
    assert flow["catalog_entries"] == "paperless:get_document+update_document:write"
    assert flow["service_accounts"] == "svc-flow-paperless@mcp-paperless"
    assert flow["shared_groups"] == "buchhaltung"
    # A title a spreadsheet would run as a formula is quoted.
    assert by_id["studio:app:formula"]["title"].startswith("'=")
    # The module that did not answer is a row of its own, never silently missing.
    unreachable = [row for row in rows if row["state"] == "nicht erreichbar"]
    assert [(row["module"], row["id"]) for row in unreachable] == [("data", "")]
    row = _outbox(config_dir)[-1]
    validate_event(row)
    assert row["event"] == "inventory_exported"
    assert _log(config_dir)[-1]["params"]["unreachable"] == ["data"]


def test_the_export_format_is_validated(client: TestClient, config_dir: Path) -> None:
    assert _auditor(client).get(f"{_EXPORT}?format=xlsx").status_code == 400
    assert _auditor(client).get(_EXPORT).status_code == 400
    assert _outbox(config_dir) == []


# -- settings ------------------------------------------------------------------------------


def _settings(**update: Any) -> Settings:
    base = get_settings().model_dump()
    base.update(update)
    return Settings(**base)


def test_the_sources_setting_is_parsed() -> None:
    settings = _settings(inventory_sources=" flow=https://f.test:8443 , data=https://d.test ")
    assert settings.inventory_source_map == {
        "data": "https://d.test",
        "flow": "https://f.test:8443",
    }
    assert _settings(inventory_sources="").inventory_source_map == {}


@pytest.mark.parametrize(
    "value",
    [
        "flow=http://f.test",
        "crm=https://c.test",
        "flow=https://f.test,flow=https://g.test",
        "flow",
        "flow=https://user:pw@f.test",
        "flow=https://f.test/path?x=1",
        "flow=https://",
    ],
)
def test_a_wrong_sources_setting_is_refused(value: str) -> None:
    with pytest.raises(ValidationError):
        _settings(inventory_sources=value)


def test_the_fetch_refuses_an_oversized_answer() -> None:
    big = b"x" * (work_inventory.MAX_ANSWER_BYTES + 1)

    def fetch(_module: str, _url: str) -> bytes:
        return big

    doc = work_inventory.collect({"flow": "https://f.test"}, fetch)
    assert doc["sources"] == [{"module": "flow", "status": "nicht erreichbar"}]



# -- the fetch over the wire ---------------------------------------------------------------


def _wire(
    handler: Any, **kwargs: Any
) -> tuple[work_inventory.Fetch, list[httpx.Request]]:
    """`fetch_with` on a stub transport; the list collects every request it sends."""
    seen: list[httpx.Request] = []

    def record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        response: httpx.Response = handler(request)
        return response

    def client() -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(record))

    return work_inventory.fetch_with(client, **kwargs), seen


def test_the_fetch_returns_the_body_and_forwards_the_correlation_id() -> None:
    fetch, seen = _wire(lambda _r: httpx.Response(200, content=b'{"ok": true}'))
    with kit_audit.bound(None, "corr-inventory-0001"):
        assert fetch("flow", "https://f.test/internal/inventory") == b'{"ok": true}'
    assert [str(r.url) for r in seen] == ["https://f.test/internal/inventory"]
    assert seen[0].method == "GET"
    assert seen[0].headers["accept"] == "application/json"
    assert seen[0].headers["x-papaia-correlation-id"] == "corr-inventory-0001"


def test_the_fetch_sends_no_correlation_header_outside_a_request() -> None:
    fetch, seen = _wire(lambda _r: httpx.Response(200, content=b"{}"))
    fetch("flow", "https://f.test/internal/inventory")
    assert "x-papaia-correlation-id" not in seen[0].headers


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_the_fetch_follows_no_redirect(status: int) -> None:
    fetch, seen = _wire(
        lambda _r: httpx.Response(status, headers={"location": "https://evil.test/x"})
    )
    with pytest.raises(work_inventory.SourceUnavailable):
        fetch("flow", "https://f.test/internal/inventory")
    assert [r.url.host for r in seen] == ["f.test"]


@pytest.mark.parametrize("status", [204, 401, 403, 404, 500])
def test_the_fetch_takes_only_a_200(status: int) -> None:
    fetch, _ = _wire(lambda _r: httpx.Response(status, content=b"{}"))
    with pytest.raises(work_inventory.SourceUnavailable):
        fetch("flow", "https://f.test/internal/inventory")


def test_the_fetch_stops_reading_at_the_size_cap() -> None:
    chunk = b"x" * (1024 * 1024)
    pulled: list[int] = []

    def endless() -> Iterator[bytes]:
        while True:
            pulled.append(1)
            yield chunk

    fetch, _ = _wire(lambda _r: httpx.Response(200, content=endless()))
    with pytest.raises(work_inventory.SourceUnavailable):
        fetch("flow", "https://f.test/internal/inventory")
    assert len(pulled) == work_inventory.MAX_ANSWER_BYTES // len(chunk) + 1


def test_the_fetch_gives_up_at_the_total_deadline() -> None:
    """A module that keeps sending slowly is cut off by the deadline, not by the cap."""
    now = [0.0]
    pulled: list[int] = []

    def trickle() -> Iterator[bytes]:
        while True:
            pulled.append(1)
            now[0] += 1.0
            yield b"x"

    fetch, _ = _wire(
        lambda _r: httpx.Response(200, content=trickle()), clock=lambda: now[0]
    )
    with pytest.raises(work_inventory.SourceUnavailable):
        fetch("flow", "https://f.test/internal/inventory")
    assert len(pulled) == int(work_inventory.DEADLINE_SECONDS) + 1


def test_a_transport_error_is_an_unreachable_source() -> None:
    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    fetch, _ = _wire(broken)
    with pytest.raises(work_inventory.SourceUnavailable):
        fetch("flow", "https://f.test/internal/inventory")


def test_the_mtls_fetch_refuses_a_missing_certificate(tmp_path: Path) -> None:
    fetch = work_inventory.mtls_fetch(
        ca_file=str(tmp_path / "ca.pem"),
        cert_file=str(tmp_path / "cert.pem"),
        key_file=str(tmp_path / "key.pem"),
    )
    with pytest.raises(work_inventory.SourceUnavailable):
        fetch("flow", "https://f.test/internal/inventory")
