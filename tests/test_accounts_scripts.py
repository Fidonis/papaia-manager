"""Users, Roles and Audit log without inline script (MGR-021, T-UI-03).

The three pages run on the CSP build of Alpine. Their script is an ES module under
static/js/pages/ that loads after the shared main.js and before Alpine, so its components
exist when Alpine starts. Server values reach a module only through data islands; a sentence
that carries markup and a value is rendered by the server with the value as a text node
(`bound`), never through `x-html`. The audit list hands its filter choices to the page
through an island instead of an inline script.
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))

import design_world  # noqa: E402

_WORLD = Path(tempfile.mkdtemp(prefix="papaia-accounts-scripts-"))
_CONFIG, _WORKSPACE = _WORLD / "config", _WORLD / "workspace"
_CONFIG.mkdir()
_WORKSPACE.mkdir()
for _key, _value in design_world.environment(
    _CONFIG, _WORKSPACE, host="http://localhost:8120", secret="test-session-secret-value"
).items():
    os.environ.setdefault(_key, _value)

from fastapi.testclient import TestClient  # noqa: E402
from markupsafe import Markup  # noqa: E402

from app import csp  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core import module_catalog  # noqa: E402
from app.core.audit import write_audit_entry  # noqa: E402
from app.main import create_app  # noqa: E402

# Page, its module under static/js/pages/ and its data island.
PAGES = {
    "/users": ("users.js", "users-page"),
    "/users/roles": ("roles.js", "roles-page"),
    "/audit": ("audit.js", "audit-page"),
}
_SCRIPT = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.DOTALL | re.IGNORECASE)


@pytest.fixture
def world(tmp_path: Path) -> Iterator[tuple[TestClient, Path]]:
    config, workspace = design_world.make_world(tmp_path)
    (config / "manager").mkdir(parents=True, exist_ok=True)
    get_settings.cache_clear()
    module_catalog.reset_readers()
    settings = get_settings().model_copy(
        update={"papaia_config_dir": str(config), "papaia_workspace_dir": str(workspace)}
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    design_world.stand_ins(app)
    client = TestClient(app, follow_redirects=False)
    client.cookies.set(
        design_world.SESSION_COOKIE,
        design_world.session_cookie(get_settings().manager_session_secret),
    )
    yield client, config
    get_settings.cache_clear()
    module_catalog.reset_readers()


@pytest.fixture
def client(world: tuple[TestClient, Path]) -> TestClient:
    return world[0]


def _get(client: TestClient, path: str) -> str:
    response = client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response.text


def _island(html: str, island_id: str) -> Any:
    found = [
        body
        for attrs, body in _SCRIPT.findall(html)
        if re.search(rf'\bid="{re.escape(island_id)}"', attrs)
        and 'type="application/json"' in attrs
    ]
    # The island of a partial htmx swaps in is a template (partials/_csp.html, swapped_island).
    found += re.findall(
        rf'<template id="{re.escape(island_id)}" data-island="application/json">(.*?)</template>',
        html,
        re.S,
    )
    assert len(found) == 1, f"{island_id}: {len(found)} islands"
    return json.loads(found[0])


def _sources(html: str) -> list[str]:
    found = [re.search(r'\bsrc="([^"]+)"', attrs) for attrs, _ in _SCRIPT.findall(html)]
    return [m.group(1).split("?", 1)[0] for m in found if m]


# -- the pages -----------------------------------------------------------------------------


@pytest.mark.parametrize("path", sorted(PAGES))
def test_the_page_runs_on_the_csp_build_and_loads_its_module(client: TestClient, path: str) -> None:
    html = _get(client, path)
    module, _ = PAGES[path]
    assert html.count('<meta name="papaia-csp" content="strict">') == 1
    order = _sources(html)
    page = f"/static/js/pages/{module}"
    assert "/static/alpine-csp.min.js" in order
    assert "/static/alpine.min.js" not in order
    assert order.count(page) == 1, order
    assert order.index("/static/js/core/main.js") < order.index(page)
    assert order.index(page) < order.index("/static/alpine-csp.min.js")
    tag = next(attrs for attrs, _ in _SCRIPT.findall(html) if f'src="{page}' in attrs)
    assert 'type="module"' in tag


@pytest.mark.parametrize("path", sorted(PAGES))
def test_the_page_has_no_inline_script_handler_style_or_x_html(
    client: TestClient, path: str
) -> None:
    html = _get(client, path)
    inline = [
        body
        for attrs, body in _SCRIPT.findall(html)
        if "src=" not in attrs and 'type="application/json"' not in attrs
    ]
    assert inline == []
    assert re.search(r"<[a-zA-Z][^<>]*\son[a-z]+\s*=", html) is None
    assert re.search(r"<[a-zA-Z][^<>]*\sstyle\s*=", html) is None
    assert "x-html" not in html


@pytest.mark.parametrize("path", sorted(PAGES))
def test_the_texts_reach_the_module_through_an_island(client: TestClient, path: str) -> None:
    html = _get(client, path)
    _, island_id = PAGES[path]
    value = _island(html, island_id)
    texts = value["texts"]
    assert texts
    assert all(isinstance(k, str) and isinstance(v, str) and v for k, v in texts.items())


def test_the_users_page_names_its_sentences(client: TestClient) -> None:
    texts = _island(_get(client, "/users"), "users-page")["texts"]
    assert set(texts) == {
        "listFailed", "listUnavailable", "reloadPage", "justNow", "mailNoServer",
        "mailNoAddress", "userCreatedLinkSent", "userCreatedMailFailed", "userCreated",
        "rolesSaved", "sessionEnded", "sessionsEnded", "linkSent", "accountEnabled",
        "accountDisabled", "chatsCleared", "chatsClearFailed", "subThreadsOne",
        "subThreadsOther", "filesFailedOne", "filesFailedOther",
    }  # fmt: skip
    assert texts["userCreated"] == "User <b>{username}</b> created."
    assert texts["chatsCleared"] == "The chats of <b>{username}</b> were cleared."


def test_the_roles_page_names_its_sentences(client: TestClient) -> None:
    texts = _island(_get(client, "/users/roles"), "roles-page")["texts"]
    assert set(texts) == {
        "listFailed", "listUnavailable", "reloadPage", "roleGone", "roleCreated",
        "roleSaved", "roleDeleted",
    }  # fmt: skip


def test_the_audit_page_hands_its_filters_and_sentences_to_the_module(client: TestClient) -> None:
    html = _get(client, "/audit?user=bob&action=install")
    value = _island(html, "audit-page")
    assert value["filters"] == {"user": "bob", "action": "install", "result": ""}
    assert set(value["texts"]) == {
        "previewFailed", "noneOlder", "willDelete", "exportFirst", "deleteFailed",
        "deletedOne", "deletedOther",
    }  # fmt: skip


def test_the_audit_page_keeps_its_hooks(client: TestClient) -> None:
    html = _get(client, "/audit")
    for hook in (
        'id="audit-filters"', 'id="audit-prune-modal"', 'id="audit-prune-open"',
        'id="audit-prune-date"', 'id="audit-prune-go"', 'id="audit-export-csv"',
        'id="audit-export-jsonl"', 'data-audit-reset', 'data-audit-prune-cancel',
    ):  # fmt: skip
        assert hook in html, hook
    for days in (30, 90, 180, 365):
        assert f'data-audit-prune-days="{days}"' in html


def test_a_sentence_with_a_value_is_rendered_by_the_server(client: TestClient) -> None:
    html = _get(client, "/users")
    dialog = html[html.index('id="user-roles-modal"') :]
    dialog = dialog[: dialog.index("</dialog>")]
    assert 'Roles of <span class="font-mono"><span x-text="username"></span></span>' in dialog
    html = _get(client, "/users/roles")
    assert 'Delete <span class="font-mono"><span x-text="name"></span></span>?' in html


def test_the_audit_list_hands_its_facets_to_the_page(world: tuple[TestClient, Path]) -> None:
    client, config = world
    write_audit_entry(str(config), user="alice", action="install", target="n8n")
    write_audit_entry(str(config), user="bob", action="stop", target="paperless")
    html = _get(client, "/partials/audit")
    facets = _island(html, "audit-facets")
    assert sorted(facets["user"]) == ["alice", "bob"]
    assert sorted(facets["action"]) == ["install", "stop"]
    inline = [b for a, b in _SCRIPT.findall(html) if 'type="application/json"' not in a]
    assert inline == []


def test_the_empty_audit_list_hands_its_facets_too(client: TestClient) -> None:
    facets = _island(_get(client, "/partials/audit"), "audit-facets")
    assert set(facets) >= {"user", "action", "result"}


# -- bound sentences -----------------------------------------------------------------------


def test_a_bound_sentence_keeps_its_markup_and_binds_each_value() -> None:
    sentence = Markup('Roles of <span class="font-mono">{username}</span> ({count})')
    out = csp.bound_sentence(sentence, username="username", count="roles.length")
    assert isinstance(out, Markup)
    assert out == (
        'Roles of <span class="font-mono"><span x-text="username"></span></span>'
        ' (<span x-text="roles.length"></span>)'
    )


def test_a_bound_sentence_takes_a_method_call_without_arguments() -> None:
    out = csp.bound_sentence(Markup("<b>{number}</b>"), number="heldNumber()")
    assert out == '<b><span x-text="heldNumber()"></span></b>'


def test_a_bound_sentence_leaves_a_placeholder_without_expression() -> None:
    # A translation that names a value the template does not bind shows the mark, it does not
    # break the page.
    assert csp.bound_sentence(Markup("{other} and {name}"), name="name") == (
        '{other} and <span x-text="name"></span>'
    )


def test_a_bound_sentence_needs_a_sentence_of_the_catalogue() -> None:
    with pytest.raises(TypeError):
        csp.bound_sentence("<b>{name}</b>", name="name")


@pytest.mark.parametrize(
    "expression",
    ["", "a b", 'x" onclick="y', "f(1)", "a[0]", "() => x", "a;b", "a.b(c)", "1a"],
)
def test_a_bound_sentence_refuses_an_expression_that_is_not_a_plain_path(expression: str) -> None:
    with pytest.raises(ValueError):
        csp.bound_sentence(Markup("{name}"), name=expression)
