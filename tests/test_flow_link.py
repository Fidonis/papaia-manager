"""The Flow link of an add-on at its installation: what the dialog shows, and the Flow token.

`app.core.flow_link` reads the `flow:` section of a catalog manifest into the summary the install
dialog shows, tells whether Flow is installed and whether the core can approve a link at install,
and reads the outcome back from the lines `papaia-ctl addon install` prints. The token exchange is
checked against a stand-in of Keycloak's token endpoint (`httpx.MockTransport`).
"""
from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest

for _key, _value in {
    "OIDC_ISSUER_KC_AUTH": "https://kc.test/auth",
    "OIDC_ISSUER_KC_TOKEN": "https://kc.test/token",
    "OIDC_ISSUER_KC_CERTS": "https://kc.test/certs",
    "MANAGER_HOST": "http://localhost:8120",
    "MANAGER_OIDC_CLIENT_SECRET": "client-secret",
    "MANAGER_SESSION_SECRET": "test-session-secret-value",
    "PAPAIA_CONFIG_DIR": tempfile.mkdtemp(prefix="papaia-flowlink-config-"),
    "PAPAIA_WORKSPACE_DIR": tempfile.mkdtemp(prefix="papaia-flowlink-workspace-"),
}.items():
    os.environ.setdefault(_key, _value)

from app.auth.oidc import OIDCClient, OIDCError  # noqa: E402
from app.core import flow_link  # noqa: E402

_TOKEN_URL = "https://kc.test/token"


def _manifest() -> dict[str, Any]:
    return {
        "name": "archive",
        "version": "1.4.0",
        "flow": {
            "api": 1,
            "source": "urn:papaia:addon:archive",
            "events": [
                {"name": "document.added", "format": "plain", "data_class_hint": "internal"},
                {"name": "document.removed", "format": "cloudevents"},
            ],
            "actions": [
                {
                    "name": "find_documents",
                    "via": "tools",
                    "server": "archive-mcp",
                    "tool": "search",
                    "identity": "person",
                    "effect": "read",
                    "data_class_max": "internal",
                },
                {
                    "name": "tag_document",
                    "via": "rest",
                    "base_url": "http://archive-api:8000",
                    "method": "POST",
                    "path": "/api/tags",
                    "identity": "service",
                    "effect": "write",
                },
            ],
        },
    }


# ── what the dialog shows ────────────────────────────────────────────────────


def test_the_summary_names_events_and_actions_with_identity_effect_class_and_address() -> None:
    shown = flow_link.preview(_manifest())

    assert shown is not None
    assert [(e.name, e.format, e.data_class) for e in shown.events] == [
        ("document.added", "plain", "internal"),
        ("document.removed", "cloudevents", ""),
    ]
    assert [(a.name, a.identity, a.effect, a.data_class, a.address) for a in shown.actions] == [
        ("find_documents", "person", "read", "internal", "archive-mcp/search"),
        ("tag_document", "service", "write", "", "POST http://archive-api:8000/api/tags"),
    ]


def test_an_add_on_without_a_flow_section_has_no_summary() -> None:
    assert flow_link.preview({"name": "plain", "version": "1.0.0"}) is None
    assert flow_link.preview({"name": "plain", "flow": None}) is None
    assert flow_link.preview({"name": "plain", "flow": "not a mapping"}) is None


def test_the_summary_keeps_only_bounded_plain_text() -> None:
    manifest = _manifest()
    manifest["flow"]["events"] = [
        {"name": "x" * 500, "format": ["list"], "data_class_hint": {"no": "mapping"}},
        "not a mapping",
    ]
    manifest["flow"]["actions"] = [{"name": "a\nb", "via": "tools", "server": 7, "tool": None}]

    shown = flow_link.preview(manifest)

    assert shown is not None
    (event,) = shown.events
    assert len(event.name) == flow_link.MAX_TEXT
    assert event.format == ""
    assert event.data_class == ""
    (action,) = shown.actions
    assert action.name == "a b"
    assert action.address == ""


def test_a_long_declaration_is_cut_and_says_so() -> None:
    manifest = _manifest()
    manifest["flow"]["actions"] = [
        {"name": f"act_{n}", "via": "tools", "server": "s", "tool": "t"}
        for n in range(flow_link.MAX_ITEMS + 5)
    ]

    shown = flow_link.preview(manifest)

    assert shown is not None
    assert len(shown.actions) == flow_link.MAX_ITEMS
    assert shown.more_actions == 5
    assert shown.more_events == 0


def test_flow_counts_as_installed_only_while_its_add_on_is_active() -> None:
    assert flow_link.flow_installed({"addons": [{"name": "papaia-flow", "active": True}]})
    assert not flow_link.flow_installed({"addons": [{"name": "papaia-flow", "active": False}]})
    assert not flow_link.flow_installed({"addons": [{"name": "paperless", "active": True}]})
    assert not flow_link.flow_installed({})


def test_only_a_core_that_knows_the_flag_is_asked_to_approve(tmp_path: Path) -> None:
    script = tmp_path / "papaia" / "tools" / "lib" / "sh" / "addon.sh"
    assert not flow_link.core_approves_at_install(str(tmp_path))

    script.parent.mkdir(parents=True)
    script.write_text("cmd_addon_install() {\n  --force) force=1 ;;\n}\n", encoding="utf-8")
    assert not flow_link.core_approves_at_install(str(tmp_path))

    script.write_text(
        "cmd_addon_install() {\n  --approve-flow-link) approve_flow_link=1 ;;\n}\n",
        encoding="utf-8",
    )
    os.utime(script, ns=(time.time_ns(), time.time_ns() + 1_000_000))
    assert flow_link.core_approves_at_install(str(tmp_path))


# ── what an approval is bound to ─────────────────────────────────────────────


def test_the_digest_covers_the_section_and_not_its_spelling() -> None:
    first = flow_link.section_digest(_manifest())
    reordered = _manifest()
    reordered["flow"] = dict(reversed(list(reordered["flow"].items())))
    assert first is not None and first.startswith("sha256:") and len(first) == 7 + 64
    assert flow_link.section_digest(reordered) == first
    changed = _manifest()
    changed["flow"]["actions"][1]["effect"] = "delete"
    assert flow_link.section_digest(changed) != first
    assert flow_link.section_digest({"name": "x"}) is None


def test_the_preview_carries_the_digest_and_the_version_it_shows() -> None:
    shown = flow_link.preview(_manifest())
    assert shown is not None
    assert shown.digest == flow_link.section_digest(_manifest())
    assert shown.version == "1.4.0"
    assert not shown.truncated


def test_a_cut_preview_counts_as_truncated() -> None:
    manifest = _manifest()
    manifest["flow"]["events"] = [
        {"name": f"e{n}", "format": "plain"} for n in range(flow_link.MAX_ITEMS + 1)
    ]
    shown = flow_link.preview(manifest)
    assert shown is not None and shown.truncated


def test_only_a_core_that_checks_the_digest_binds_an_approval(tmp_path: Path) -> None:
    script = tmp_path / "papaia" / "tools" / "lib" / "sh" / "addon.sh"
    script.parent.mkdir(parents=True)
    script.write_text(
        "cmd_addon_install() {\n  --approve-flow-link) approve_flow_link=1 ;;\n}\n",
        encoding="utf-8",
    )
    assert not flow_link.core_binds_approval(str(tmp_path))
    script.write_text(
        "cmd_addon_install() {\n  --approve-flow-link) approve_flow_link=1 ;;\n"
        "  --expect-flow-digest=*) expect_flow_digest=\"${1#*=}\" ;;\n"
        "  --expect-addon-version=*) expect_addon_version=\"${1#*=}\" ;;\n}\n",
        encoding="utf-8",
    )
    os.utime(script, ns=(time.time_ns(), time.time_ns() + 2_000_000))
    assert flow_link.core_binds_approval(str(tmp_path))


# ── what the installation reported ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("lines", "expected"),
    [
        (["Flow link of archive approved at install.", "Addon installed: archive"],
         ("approved", [])),
        (["Flow link of archive stays a proposal for a second person: data_class:x, egress:y"],
         ("proposal", ["data_class:x", "egress:y"])),
        (["Flow link of archive not approved; it stays a proposal. Run the install again"],
         ("not_approved", [])),
        (["WARNING: Flow link of archive not approved: the API container is not running"],
         ("error", [])),
        (["NOTE: archive declares a Flow link; without a token of a Flow administrator"],
         ("no_token", [])),
        (["WARNING: Flow did not show the link of archive 1.4.0; is PAPAIA_FLOW_ADDON_DIR"],
         ("not_read", [])),
        (["Flow link of archive: nothing to approve (state active); the declaration stays."],
         ("unchanged", [])),
        (["NOTE: archive declares a Flow link; Flow is not installed. The declaration is kept"],
         ("skipped", [])),
        (["Addon installed: archive"], ("none", [])),
        # Another add-on's line is not this one's outcome.
        (["Flow link of other approved at install."], ("none", [])),
    ],
)
def test_the_outcome_is_read_from_what_the_core_printed(
    lines: list[str], expected: tuple[str, list[str]]
) -> None:
    assert flow_link.outcome("archive", lines) == expected


# ── the Flow token: a token exchange for Flow's audience ─────────────────────


def _jwt(claims: dict[str, Any]) -> str:
    def part(data: dict[str, Any]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'RS256', 'kid': 'k'})}.{part(claims)}.c2ln"


def _serve(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def _record(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return handler(request)

    real = httpx.AsyncClient

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return real(*args, transport=httpx.MockTransport(_record), **kwargs)

    monkeypatch.setattr("app.auth.oidc.httpx.AsyncClient", _factory)
    return seen


def _client() -> OIDCClient:
    return OIDCClient(
        auth_endpoint="https://kc.test/auth",
        token_endpoint=_TOKEN_URL,
        jwks_endpoint="https://kc.test/certs",
        client_id="papaia-manager",
        client_secret="client-secret",
        redirect_uri="http://localhost:8120/auth/callback",
    )


async def test_the_exchange_asks_for_exactly_flows_audience_with_the_admins_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    issued = _jwt({"aud": "flow", "sub": "u-1"})
    seen = _serve(
        monkeypatch,
        lambda _r: httpx.Response(200, json={"access_token": issued, "expires_in": 300}),
    )

    token = await _client().exchange_token(subject_token="admin-access", audience="flow")

    assert token.access_token == issued
    assert token.expires_at == pytest.approx(time.time() + 300, abs=5)
    assert "admin-access" not in repr(token) and issued not in repr(token)
    (request,) = seen
    assert str(request.url) == _TOKEN_URL
    assert parse_qs(request.content.decode()) == {
        "grant_type": ["urn:ietf:params:oauth:grant-type:token-exchange"],
        "client_id": ["papaia-manager"],
        "client_secret": ["client-secret"],
        "subject_token": ["admin-access"],
        "subject_token_type": ["urn:ietf:params:oauth:token-type:access_token"],
        "requested_token_type": ["urn:ietf:params:oauth:token-type:access_token"],
        # The scope is the only way the realm adds Flow's audience, and its one-audience
        # policy refuses any other `aud-*` scope next to it; the audience keeps the realm's
        # default audiences (`account`) out.
        "scope": ["aud-flow"],
        "audience": ["flow"],
    }


@pytest.mark.parametrize("aud", [["flow"], "flow"])
async def test_a_token_for_exactly_flow_is_accepted(
    monkeypatch: pytest.MonkeyPatch, aud: Any
) -> None:
    issued = _jwt({"aud": aud})
    _serve(monkeypatch, lambda _r: httpx.Response(200, json={"access_token": issued}))

    token = await _client().exchange_token(subject_token="s", audience="flow")

    # Without `expires_in` the token is trusted for a minute, not longer.
    assert token.expires_at == pytest.approx(time.time() + 60, abs=5)


@pytest.mark.parametrize(
    "answer",
    [
        {"access_token": _jwt({"aud": ["flow", "account"]})},
        {"access_token": _jwt({"aud": "papaia-manager"})},
        {"access_token": _jwt({"sub": "u-1"})},
        {"access_token": "not-a-jwt"},
        {"token_type": "Bearer"},
    ],
)
async def test_a_token_for_any_other_audience_is_refused(
    monkeypatch: pytest.MonkeyPatch, answer: dict[str, Any]
) -> None:
    _serve(monkeypatch, lambda _r: httpx.Response(200, json=answer))

    with pytest.raises(OIDCError):
        await _client().exchange_token(subject_token="s", audience="flow")


async def test_a_refused_exchange_is_an_oidc_error_without_the_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _serve(monkeypatch, lambda _r: httpx.Response(403, json={"error": "access_denied"}))

    with pytest.raises(OIDCError) as raised:
        await _client().exchange_token(subject_token="admin-access", audience="flow")

    assert "admin-access" not in str(raised.value)
