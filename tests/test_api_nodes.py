"""The Nodes page and its API: who may see it, who may change it, and what is recorded."""
from __future__ import annotations

import base64
import json
import os
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

_SESSION_SECRET = "test-session-secret-value"
_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-nodes-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-nodes-workspace-")

os.environ.update(
    OIDC_ISSUER_KC_AUTH="https://kc.test/auth",
    OIDC_ISSUER_KC_TOKEN="https://kc.test/token",
    OIDC_ISSUER_KC_CERTS="https://kc.test/certs",
    MANAGER_ADMIN_ROLE="admin",
    MANAGER_USER_ROLE="user",
    MANAGER_HOST="http://localhost:8120",
    MANAGER_OIDC_CLIENT_SECRET="client-secret",
    MANAGER_SESSION_SECRET=_SESSION_SECRET,
)

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app import main as app_main  # noqa: E402
from app.auth import oidc  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core import nodes  # noqa: E402
from app.core.jobs import JobQueue  # noqa: E402
from app.main import create_app  # noqa: E402

_CSRF = "test-csrf-token"
_TOPOLOGY = {
    "topology": {
        "mode": "distributed",
        "controller": "internal",
        "nodes": {
            "internal": {"address": "10.0.2.10", "executor": "local"},
            "edge": {"address": "10.0.1.10", "executor": "ssh"},
        },
    }
}
_STATE = nodes.NodeState(
    name="edge",
    address="10.0.1.10",
    executor="ssh",
    controller=False,
    services=("keycloak",),
    host_key_pinned=True,
    reachable=False,
    reason="ssh: timed out",
    version="1.8.0",
)


def _deployment(body: dict[str, Any]) -> None:
    (Path(_CONFIG_DIR) / "deployment.yaml").write_text(yaml.safe_dump(body), encoding="utf-8")


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PAPAIA_CONFIG_DIR", _CONFIG_DIR)
    monkeypatch.setenv("PAPAIA_WORKSPACE_DIR", _WORKSPACE_DIR)
    get_settings.cache_clear()
    app_main._job_queue = JobQueue(config_dir=_CONFIG_DIR)  # noqa: SLF001
    _deployment(_TOPOLOGY)

    async def listed(**kwargs: Any) -> list[nodes.NodeState]:
        return [_STATE]

    monkeypatch.setattr(nodes, "overview", listed)
    yield TestClient(create_app(), follow_redirects=False)
    get_settings.cache_clear()
    app_main._job_queue = None  # noqa: SLF001


def _session(
    client: TestClient, *, roles: tuple[str, ...] = ("admin",), **extra: Any
) -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": "u-1",
            "preferred_username": "tester",
            "roles": list(roles),
            "exp": int(time.time()) + 3600,
        },
        "_csrf_token": _CSRF,
        **extra,
    }
    payload = base64.b64encode(json.dumps(session).encode())
    client.cookies.clear()
    client.cookies.set(
        "papaia_manager_session", TimestampSigner(_SESSION_SECRET).sign(payload).decode()
    )
    return client


def _stepped_up(client: TestClient) -> TestClient:
    return _session(client, step_up_at=int(time.time()))


def _audit() -> list[dict[str, Any]]:
    path = Path(_CONFIG_DIR) / "manager" / "audit.log"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


_HEADERS = {"X-CSRF-Token": _CSRF}
_BODY = {"name": "gpu", "address": "10.0.3.10"}


def test_a_single_host_has_no_page_and_no_sidebar_entry(client: TestClient) -> None:
    _deployment({"core": {}})
    admin = _session(client)
    assert admin.get("/nodes").status_code == 404
    assert admin.get("/partials/nodes").status_code == 404
    assert admin.get("/nodes/edge").status_code == 404
    assert 'href="/nodes"' not in admin.get("/audit").text


def test_a_distributed_installation_has_both(client: TestClient) -> None:
    admin = _session(client)
    page = admin.get("/nodes")
    assert page.status_code == 200 and 'href="/nodes"' in page.text


def test_the_list_shows_a_node_that_does_not_answer(client: TestClient) -> None:
    html = _session(client).get("/partials/nodes").text
    assert "edge" in html and "10.0.1.10" in html and "keycloak" in html
    assert "unreachable" in html and "1.8.0" in html


def test_a_node_to_enrol_shows_the_commands_and_its_host_keys(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    pending = nodes.NodeState(
        name="edge",
        address="10.0.1.10",
        executor="ssh",
        controller=False,
        services=(),
        host_key_pinned=False,
        reachable=False,
    )

    async def listed(**kwargs: Any) -> list[nodes.NodeState]:
        return [pending]

    async def keys(name: str, **kwargs: Any) -> list[nodes.HostKey]:
        return [nodes.HostKey("ssh-ed25519", "SHA256:" + "a" * 43)]

    monkeypatch.setattr(nodes, "overview", listed)
    monkeypatch.setattr(nodes, "host_keys", keys)
    html = _stepped_up(client).get("/nodes/edge").text
    assert "papaia-join-edge/install.sh" in html
    assert "SHA256:" + "a" * 43 in html and "pinKey" in html


def test_an_unknown_node_has_no_page(client: TestClient) -> None:
    assert _session(client).get("/nodes/elsewhere").status_code == 404


def test_an_account_without_the_role_is_refused(client: TestClient) -> None:
    user = _session(client, roles=("user",))
    assert user.get("/nodes").status_code == 403
    assert user.post("/api/v1/nodes", json=_BODY, headers=_HEADERS).status_code == 403


@pytest.mark.parametrize(
    "method, url, body",
    [
        ("post", "/api/v1/nodes", _BODY),
        ("delete", "/api/v1/nodes/edge", None),
        ("post", "/api/v1/nodes/edge/pin", {"fingerprint": "SHA256:" + "a" * 43}),
    ],
)
def test_every_change_needs_a_fresh_sign_in(
    client: TestClient, method: str, url: str, body: dict[str, Any] | None
) -> None:
    admin = _session(client)
    kwargs: dict[str, Any] = {"headers": _HEADERS}
    if body is not None:
        kwargs["json"] = body
    response = getattr(admin, method)(url, **kwargs)
    assert response.status_code == 403
    assert "fresh" in response.json()["detail"]


def test_a_sign_in_that_is_too_old_does_not_count(client: TestClient) -> None:
    stale = _session(client, step_up_at=int(time.time()) - 301)
    assert stale.post("/api/v1/nodes", json=_BODY, headers=_HEADERS).status_code == 403


def test_a_change_needs_the_csrf_token_too(client: TestClient) -> None:
    assert _stepped_up(client).post("/api/v1/nodes", json=_BODY).status_code == 403


def test_adding_a_node_is_audited(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    async def add(new: nodes.NewNode, **kwargs: Any) -> str:
        return f"/c/nodes/{new.name}/papaia-join-{new.name}.tar.gz"

    monkeypatch.setattr(nodes, "add_node", add)
    response = _stepped_up(client).post("/api/v1/nodes", json=_BODY, headers=_HEADERS)
    assert response.status_code == 201
    assert response.json()["archive"].endswith("papaia-join-gpu.tar.gz")
    last = _audit()[-1]
    assert last["action"] == "node.add" and last["target"] == "gpu" and last["user"] == "tester"


def test_a_refused_removal_is_audited_and_says_why(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def remove(name: str, **kwargs: Any) -> None:
        raise nodes.NodeError("node 'edge' still runs keycloak")

    monkeypatch.setattr(nodes, "remove_node", remove)
    response = _stepped_up(client).delete("/api/v1/nodes/edge", headers=_HEADERS)
    assert response.status_code == 409 and "still runs" in response.json()["detail"]
    last = _audit()[-1]
    assert last["action"] == "node.remove" and last["result"] == "failed"


@pytest.mark.parametrize("name", ["Bad_Name", "-x", "a b"])
def test_a_bad_name_never_reaches_the_core(client: TestClient, name: str) -> None:
    response = _stepped_up(client).post(
        "/api/v1/nodes", json={"name": name, "address": "10.0.3.10"}, headers=_HEADERS
    )
    assert response.status_code == 422


def test_a_bad_fingerprint_never_reaches_the_core(client: TestClient) -> None:
    response = _stepped_up(client).post(
        "/api/v1/nodes/edge/pin", json={"fingerprint": "MD5:aa"}, headers=_HEADERS
    )
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Step-up
# ---------------------------------------------------------------------------


class FakeOidc:
    def __init__(self, *, auth_time: int, sub: str = "u-1") -> None:
        self.auth_time = auth_time
        self.sub = sub
        self.step_up: bool | None = None

    def build_auth_url(self, state: str, challenge: str, *, step_up: bool = False) -> str:
        self.step_up = step_up
        return f"https://kc.test/auth?state={state}"

    async def exchange_code(self, *, code: str, code_verifier: str) -> oidc.TokenSet:
        claims = oidc.OIDCClaims(
            sub=self.sub,
            preferred_username="tester",
            roles=["admin"],
            exp=int(time.time()) + 3600,
            auth_time=self.auth_time,
        )
        return oidc.TokenSet(claims=claims, refresh_token=None)


def _step_up(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, fake: FakeOidc
) -> tuple[int, str]:
    from app.routers import auth

    monkeypatch.setattr(auth, "get_oidc_client", lambda: fake)
    admin = _session(client)
    sent = admin.get("/auth/step-up?next=/nodes")
    assert sent.status_code == 302 and fake.step_up is True
    state = sent.headers["location"].split("state=", 1)[1]
    back = admin.get(f"/auth/callback?code=c&state={state}")
    return back.status_code, back.headers.get("location", "")


def test_a_fresh_sign_in_opens_the_changes(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    code, location = _step_up(client, monkeypatch, FakeOidc(auth_time=int(time.time())))
    assert (code, location) == (302, "/nodes")
    async def add(new: nodes.NewNode, **kwargs: Any) -> str:
        return "/c/x.tar.gz"

    monkeypatch.setattr(nodes, "add_node", add)
    assert client.post("/api/v1/nodes", json=_BODY, headers=_HEADERS).status_code == 201


def test_a_silent_sign_in_is_not_a_step_up(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The provider ignored prompt=login and answered from its open SSO session.
    code, _ = _step_up(client, monkeypatch, FakeOidc(auth_time=int(time.time()) - 3600))
    assert code == 403
    assert client.post("/api/v1/nodes", json=_BODY, headers=_HEADERS).status_code == 403


def test_another_account_cannot_confirm_for_this_one(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeOidc(auth_time=int(time.time()), sub="someone-else")
    assert _step_up(client, monkeypatch, fake)[0] == 403


def test_step_up_returns_only_to_this_site(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.routers import auth

    fake = FakeOidc(auth_time=int(time.time()))
    monkeypatch.setattr(auth, "get_oidc_client", lambda: fake)
    admin = _session(client)
    state = admin.get("/auth/step-up?next=//evil.example").headers["location"].split("state=")[1]
    assert admin.get(f"/auth/callback?code=c&state={state}").headers["location"] == "/"


def test_an_ordinary_login_ends_a_step_up(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.routers import auth

    fake = FakeOidc(auth_time=int(time.time()))
    monkeypatch.setattr(auth, "get_oidc_client", lambda: fake)
    admin = _stepped_up(client)
    state = admin.get("/auth/login").headers["location"].split("state=", 1)[1]
    assert admin.get(f"/auth/callback?code=c&state={state}").status_code == 302
    assert fake.step_up is False
    assert admin.post("/api/v1/nodes", json=_BODY, headers=_HEADERS).status_code == 403


def test_the_id_token_carries_the_time_of_the_sign_in() -> None:
    claims = oidc._extract_claims(  # noqa: SLF001
        {"sub": "u", "exp": 2_000_000_000, "auth_time": 1_900_000_000, "roles": []}, "roles"
    )
    assert claims.auth_time == 1_900_000_000
    assert oidc._extract_claims({"sub": "u", "exp": 2_000_000_000}, "roles").auth_time == 0  # noqa: SLF001
