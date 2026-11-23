"""The map of administration places in the manager (PLT-77 / PLT-63, PLT-73 / PLT-59).

The kit's `contract/admin-map.yaml` names the tasks the manager carries (install, upgrade,
backup, users and roles, name and logo, contact, audit log) and the rows a module's group
"Verwaltung" points into the manager with (audit log, assign roles). Every such place must
be a page of the manager. The navigation entry "Module" lists each module with its state,
its version, "Öffnen" and "Verwalten"; a stopped module keeps a locked entry with the kit's
fixed sentence, a module that is not set up shows the other fixed sentence.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient
from papaia_kit._resources import contract_dir
from papaia_kit.catalog import parse_services
from papaia_kit.ui.address import target_url
from papaia_kit.ui.admin_map import entry_url, load_admin_map
from starlette.routing import Match

from app import shell
from app.main import create_app
from tests.test_module_catalog import (  # noqa: F401 -- fixtures
    _as,
    client,
    dirs,
    write_bom,
    write_catalog,
)

ALL = shell.NavFlags(admin=True, rag=True, knowledge_map=True, users=True, nodes=True)


class _Pages:
    """The GET routes of the manager, matched the way a request is routed."""

    def __init__(self) -> None:
        self.routes = create_app().routes

    def __contains__(self, path: object) -> bool:
        scope = {"type": "http", "path": path, "method": "GET", "root_path": ""}
        return any(route.matches(scope)[0] == Match.FULL for route in self.routes)


def _page_paths() -> _Pages:
    return _Pages()


def _nav_paths() -> dict[str, str]:
    return {row.href: row.label for _h, rows in shell.nav_groups(ALL, lambda t: t) for row in rows}


def _reference_catalog() -> Any:
    vectors = json.loads((contract_dir() / "vectors" / "from.json").read_text(encoding="utf-8"))
    return parse_services(json.dumps(vectors["catalog"]).encode("utf-8"))


def test_every_manager_task_of_the_map_is_a_page_in_the_navigation() -> None:
    tasks = [t for t in load_admin_map().tasks if t.place == "manager"]
    assert {t.id for t in tasks} >= {"install_module", "upgrade", "backup_restore",
                                     "users_and_roles", "name_and_logo", "audit_log"}
    pages, nav = _page_paths(), _nav_paths()
    for task in tasks:
        assert task.path in pages, task
        assert task.path in nav, task


def test_the_manager_targets_of_the_catalog_are_pages_here() -> None:
    catalog = _reference_catalog()
    manager = catalog.surface("manager")
    assert manager is not None
    pages = _page_paths()
    for name in manager.data["targets"]:
        url = target_url(catalog, "manager", name)
        assert url is not None
        assert urlsplit(url).path in pages, name


@pytest.mark.parametrize("module", ["studio", "flow", "assistant", "data"])
def test_rows_of_a_module_into_the_manager_resolve_to_manager_pages(module: str) -> None:
    catalog = _reference_catalog()
    manager = catalog.surface("manager")
    assert manager is not None
    admin, pages = load_admin_map(), _page_paths()
    into_manager = [e for e in admin.entries_for(module) if e.target is not None]
    assert {e.id for e in into_manager} == {"audit_log", "roles"}
    for entry in into_manager:
        url = entry_url(catalog, module, entry)
        assert url is not None
        parts = urlsplit(url)
        assert url.startswith(manager.public_url.rstrip("/") + "/")
        assert parts.path in pages
        if entry.filter_module:
            assert parts.query == f"module={module}"


def test_the_navigation_has_the_entry_modules() -> None:
    groups = shell.nav_groups(ALL, lambda t: t)
    rows = {row.href: row for _h, items in groups for row in items}
    assert rows["/modules"].label == "Modules"
    assert shell.current_row("/modules", groups) == "/modules"
    assert shell.current_row("/addons/openwebui", groups) == "/addons"
    assert shell.nav_groups(shell.NavFlags(), lambda t: t) == []


def _rows(body: str) -> dict[str, str]:
    """Module title -> its table row."""
    out = {}
    for row in re.findall(r"<tr data-module=\"[^\"]+\">(.*?)</tr>", body, re.DOTALL):
        title = re.search(r'<div class="font-medium">([^<]+)</div>', row)
        assert title is not None
        out[title.group(1)] = row
    return out


def test_the_modules_page_offers_open_and_manage(
    client: TestClient, dirs: tuple[Path, Path]  # noqa: F811
) -> None:
    workspace, config = dirs
    write_bom(workspace)
    write_catalog(
        config,
        {"papaia-flow": "running", "papaia-studio": "running", "papaia-assist": "running",
         "papaia-data": "stopped"},
    )
    body = _as(client, "admin").get("/modules", headers={"Accept-Language": "de"}).text
    rows = _rows(body)
    assert 'href="https://flow.example.org/" rel="noopener">Öffnen</a>' in rows["Flow"]
    assert 'href="https://flow.example.org/admin" rel="noopener">Verwalten</a>' in rows["Flow"]
    assert (
        'href="https://assistant.example.org/admin/emergency-stop" rel="noopener">Verwalten</a>'
        in rows["Assistant"]
    )
    # Studio declares no administration overview: no "Verwalten".
    assert "Öffnen" in rows["Studio"]
    assert "Verwalten" not in rows["Studio"]


def test_a_stopped_module_keeps_a_locked_entry(
    client: TestClient, dirs: tuple[Path, Path]  # noqa: F811
) -> None:
    workspace, config = dirs
    write_bom(workspace)
    write_catalog(config, {"papaia-data": "stopped"})
    body = _as(client, "admin").get("/modules", headers={"Accept-Language": "de"}).text
    row = _rows(body)["Data"]
    locked = '<span class="pk-link" aria-disabled="true">Data ist gerade nicht erreichbar.</span>'
    assert locked in row
    assert "data.example.org" not in body.split("<main", 1)[1]
    body = _as(client, "admin").get("/modules", headers={"Accept-Language": "en"}).text
    assert "Data is currently not reachable." in _rows(body)["Data"]


def test_a_module_that_is_not_set_up_says_so(
    client: TestClient, dirs: tuple[Path, Path]  # noqa: F811
) -> None:
    workspace, config = dirs
    write_bom(workspace)
    body = _as(client, "admin").get("/modules", headers={"Accept-Language": "de"}).text
    row = _rows(body)["Flow"]
    assert "Flow ist in dieser Installation nicht eingerichtet." in row
    assert "Öffnen" not in row
    assert "Verwalten" not in row
    assert "papaia-ctl addon install papaia-flow" in row


def test_the_modules_page_is_titled_modules(
    client: TestClient, dirs: tuple[Path, Path]  # noqa: F811
) -> None:
    write_bom(dirs[0])
    body = _as(client, "admin").get("/modules", headers={"Accept-Language": "de"}).text
    assert "<title>Module – Manager" in body
    assert 'href="/modules" aria-current="page"' in body


def test_the_api_names_the_administration_address(
    client: TestClient, dirs: tuple[Path, Path]  # noqa: F811
) -> None:
    workspace, config = dirs
    write_bom(workspace)
    write_catalog(config, {"papaia-flow": "running", "papaia-data": "stopped"})
    modules = {m["name"]: m for m in _as(client, "admin").get("/api/v1/modules").json()["modules"]}
    assert modules["papaia-flow"]["admin_url"] == "https://flow.example.org/admin"
    assert modules["papaia-data"]["admin_url"] is None
    assert modules["papaia-studio"]["admin_url"] is None
