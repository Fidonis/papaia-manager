"""Approving an add-on's Flow link as part of its installation from the manager.

Before an add-on with a `flow:` section is installed while Flow is installed, the install dialog
shows what the link may do and offers to approve it. With the box ticked the job passes
`--approve-flow-link` to `papaia-ctl addon install` and a Flow token of the signed-in
administrator in the environment (`PAPAIA_FLOW_TOKEN`), never as an argument. The token comes
from a token exchange for Flow's audience, made from the administrator's own session.

Stand-ins: `papaia-ctl` is a shell script in a temporary workspace that records its arguments and
the token it received and prints the lines the core prints; Keycloak's token endpoint is an
`httpx.MockTransport`, and the session's refresh is replaced like in `test_user_token.py`. No
core, no Flow and no Keycloak run here.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import tempfile
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import parse_qs

import httpx
import pytest
import yaml

_SESSION_SECRET = "test-session-secret-value"
_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-flowlink-install-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-flowlink-install-workspace-")

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
from app.auth import user_token  # noqa: E402
from app.auth.oidc import OIDCClaims, OIDCClient, TokenSet, get_oidc_client  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core.catalogs import Catalog, CatalogRegistry, save_registry  # noqa: E402
from app.core.jobs import Job, JobCallback, JobContext  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers import ui  # noqa: E402

_CSRF = "test-csrf-token"
_CONFIG = Path(_CONFIG_DIR)
_WORKSPACE = Path(_WORKSPACE_DIR)
_CATALOG_SOURCE = _WORKSPACE / "catalog-source"
_ADMIN_ACCESS = "admin-access-token"

# The stand-in of papaia-ctl. It writes what it was given next to the config directory and prints
# the lines of the core's `flow_link.link_at_install` for the answer the test chose.
_CTL = r"""#!/usr/bin/env bash
set -euo pipefail
out="$PAPAIA_CONFIG_DIR/.ctl"
mkdir -p "$out"
printf '%s\n' "$@" > "$out/argv"
printf '%s' "${PAPAIA_FLOW_TOKEN-<unset>}" > "$out/token"
verb="$2"; name="$3"
[[ "$verb" == install ]] || exit 0
answer="$(cat "$out/answer" 2>/dev/null || echo approved)"
approve=0
for arg in "$@"; do [[ "$arg" == --approve-flow-link ]] && approve=1; done
if [[ -z "${PAPAIA_FLOW_TOKEN-}" ]]; then
  echo "NOTE: $name declares a Flow link; without a token of a Flow administrator" \
    "it stays a proposal."
elif [[ "$approve" -eq 0 ]]; then
  echo "Flow link of $name not approved; it stays a proposal. Run the install again"
elif [[ "$answer" == approved ]]; then
  echo "Flow link of $name approved at install."
else
  echo "Flow link of $name stays a proposal for a second person:" \
    "egress:tag_document, data_class:find"
fi
echo "Addon installed: $name"
"""


class _FakeQueue:
    def __init__(self) -> None:
        self.enqueued: list[dict[str, Any]] = []

    async def enqueue(
        self,
        *,
        action: str,
        target: str,
        user: str,
        params: dict[str, Any] | None = None,
        callback: JobCallback,
    ) -> Job:
        self.enqueued.append({"params": params, "callback": callback})
        return Job(
            id="job-1", action=action, target=target, user=user,
            created_at=datetime.now(tz=UTC), params=params or {},
        )


class _Keycloak:
    """The token endpoint: the session's refresh, and the exchange for Flow's audience."""

    def __init__(self) -> None:
        self.exchanges: list[dict[str, list[str]]] = []
        self.status = 200
        self.aud: Any = "flow"
        self.expires_in = 300

    async def refresh(self, *, refresh_token: str) -> TokenSet:
        claims = OIDCClaims(sub="u-1", preferred_username="tester", roles=["admin"], exp=2**31)
        return TokenSet(
            claims=claims, refresh_token=refresh_token, access_token=_ADMIN_ACCESS,
            access_expires_at=time.time() + 300,
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        form = parse_qs(request.content.decode())
        self.exchanges.append(form)
        if self.status != 200:
            return httpx.Response(self.status, json={"error": "access_denied"})
        return httpx.Response(
            200, json={"access_token": self.flow_token(), "expires_in": self.expires_in}
        )

    def flow_token(self) -> str:
        def part(data: dict[str, Any]) -> str:
            return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

        return f"{part({'alg': 'RS256'})}.{part({'aud': self.aud, 'sub': 'u-1'})}.c2ln"


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> _FakeQueue:
    fake = _FakeQueue()
    monkeypatch.setattr(app_main, "_job_queue", fake)
    return fake


@pytest.fixture
def keycloak(monkeypatch: pytest.MonkeyPatch) -> Iterator[_Keycloak]:
    fake = _Keycloak()
    monkeypatch.setattr(OIDCClient, "refresh", fake.refresh)
    real = httpx.AsyncClient

    def _factory(*args: Any, **kwargs: Any) -> httpx.AsyncClient:
        return real(*args, transport=httpx.MockTransport(fake.handle), **kwargs)

    monkeypatch.setattr("app.auth.oidc.httpx.AsyncClient", _factory)
    get_oidc_client.cache_clear()
    user_token.clear()
    yield fake
    user_token.clear()
    get_oidc_client.cache_clear()


@pytest.fixture
def client(
    monkeypatch: pytest.MonkeyPatch, queue: _FakeQueue, keycloak: _Keycloak
) -> Iterator[TestClient]:
    async def no_docker(_settings: Any) -> Any:
        return SimpleNamespace(running_projects=set())

    monkeypatch.setattr(ui, "_load_stack_snapshot", no_docker)
    monkeypatch.setenv("PAPAIA_CONFIG_DIR", _CONFIG_DIR)
    monkeypatch.setenv("PAPAIA_WORKSPACE_DIR", _WORKSPACE_DIR)
    monkeypatch.delenv("PAPAIA_FLOW_TOKEN", raising=False)
    get_settings.cache_clear()
    yield TestClient(create_app(), follow_redirects=False)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_state() -> Iterator[None]:
    for root in (_CONFIG, _WORKSPACE):
        for child in root.iterdir():
            shutil.rmtree(child) if child.is_dir() else child.unlink()
    yield


def _as(client: TestClient, role: str = "admin", *, refresh: bool = True) -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": "u-1", "preferred_username": "tester", "roles": [role],
            "exp": int(time.time()) + 3600,
        },
        "_csrf_token": _CSRF,
    }
    if refresh:
        session["oidc"] = {"refresh_token": "rt-1"}
    payload = base64.b64encode(json.dumps(session).encode())
    client.cookies.clear()
    client.cookies.set(
        "papaia_manager_session", TimestampSigner(_SESSION_SECRET).sign(payload).decode()
    )
    return client


_FLOW_SECTION: dict[str, Any] = {
    "api": 1,
    "source": "urn:papaia:addon:archive",
    "events": [{"name": "document.added", "format": "plain", "data_class_hint": "internal"}],
    "actions": [
        {
            "name": "find_documents", "via": "tools", "server": "archive-mcp", "tool": "search",
            "identity": "person", "effect": "read", "data_class_max": "internal",
        },
        {
            "name": "tag_document", "via": "rest", "base_url": "http://archive-api:8000",
            "method": "POST", "path": "/api/tags", "identity": "service", "effect": "write",
        },
    ],
}


def _world(
    *, flow: bool = True, section: bool = True, core_approves: bool = True,
    answer: str = "approved",
) -> None:
    """`archive` in a local catalog, Flow registered or not, and the papaia-ctl stand-in."""
    ctl = _WORKSPACE / "papaia" / "tools" / "papaia-ctl"
    ctl.parent.mkdir(parents=True, exist_ok=True)
    ctl.write_text(_CTL, encoding="utf-8")
    addon_sh = ctl.parent / "lib" / "sh" / "addon.sh"
    addon_sh.parent.mkdir(parents=True, exist_ok=True)
    addon_sh.write_text(
        "cmd_addon_install() {\n"
        + ("  --approve-flow-link) approve_flow_link=1 ;;\n" if core_approves else "")
        + "}\n",
        encoding="utf-8",
    )
    os.utime(addon_sh, ns=(time.time_ns(), time.time_ns()))

    manifest: dict[str, Any] = {"name": "archive", "version": "1.4.0"}
    if section:
        manifest["flow"] = _FLOW_SECTION
    addon = _CATALOG_SOURCE / "archive"
    addon.mkdir(parents=True)
    (addon / "papaia-app.yaml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    (addon / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    save_registry(
        _CONFIG_DIR,
        CatalogRegistry(catalogs=[Catalog(name="cat", type="local", path=str(_CATALOG_SOURCE))]),
    )
    addons = [{"name": "papaia-flow", "path": "/flow", "active": True}] if flow else []
    (_CONFIG / "deployment.yaml").write_text(yaml.safe_dump({"addons": addons}), encoding="utf-8")
    (_CONFIG / ".ctl").mkdir()
    (_CONFIG / ".ctl" / "answer").write_text(answer, encoding="utf-8")


def _install(client: TestClient, *, approve: bool | None, refresh: bool = True) -> Any:
    body: dict[str, Any] = {"catalog": "cat", "start": False}
    if approve is not None:
        body["approve_flow_link"] = approve
    return _as(client, refresh=refresh).post(
        "/api/v1/addons/archive/install", json=body, headers={"X-CSRF-Token": _CSRF}
    )


def _run(queue: _FakeQueue, tmp_path: Path) -> str:
    (entry,) = queue.enqueued
    log_path = tmp_path / "job.log"
    ctx = JobContext(
        job=Job(
            id="job-1", action="install", target="archive", user="tester",
            created_at=datetime.now(tz=UTC),
        ),
        log_path=log_path,
    )
    asyncio.run(entry["callback"](ctx))
    return log_path.read_text(encoding="utf-8")


def _argv() -> list[str]:
    return (_CONFIG / ".ctl" / "argv").read_text(encoding="utf-8").splitlines()


def _token_seen() -> str:
    return (_CONFIG / ".ctl" / "token").read_text(encoding="utf-8")


def _audit() -> list[dict[str, Any]]:
    path = _CONFIG / "manager" / "audit.log"
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


# ── the dialog ───────────────────────────────────────────────────────────────


def test_the_dialog_shows_what_the_link_may_do_and_offers_to_approve_it(
    client: TestClient,
) -> None:
    _world()

    html = _as(client).get("/partials/addons/archive").text

    for text in (
        "document.added", "plain", "find_documents", "person", "read", "internal",
        "archive-mcp/search", "tag_document", "service", "write",
        "POST http://archive-api:8000/api/tags",
        'name="approve_flow_link"', "Approve the link with Flow",
        "flow link approve archive --at-install",
    ):
        assert text in html, text


def test_the_gallery_dialog_shows_the_same_link(client: TestClient) -> None:
    _world()

    html = _as(client).get("/partials/addons").text

    assert 'name="approve_flow_link"' in html
    assert "tag_document" in html


def test_the_box_is_labelled_in_german(client: TestClient) -> None:
    _world()

    html = _as(client).get("/partials/addons/archive", headers={"Accept-Language": "de"}).text

    assert "Verknüpfung mit Flow freigeben" in html


def test_without_flow_the_dialog_says_nothing_of_a_link(client: TestClient) -> None:
    _world(flow=False)

    html = _as(client).get("/partials/addons/archive").text

    assert 'name="approve_flow_link"' not in html
    assert "find_documents" not in html


def test_an_add_on_without_a_flow_section_gets_no_box(client: TestClient) -> None:
    _world(section=False)

    assert 'name="approve_flow_link"' not in _as(client).get("/partials/addons/archive").text


def test_a_core_that_cannot_approve_shows_the_link_without_the_box(client: TestClient) -> None:
    _world(core_approves=False)

    html = _as(client).get("/partials/addons/archive").text

    assert "find_documents" in html
    assert 'name="approve_flow_link"' not in html
    assert "flow link approve archive --at-install" in html


# ── the installation ─────────────────────────────────────────────────────────


def test_a_confirmation_passes_the_flag_and_the_flow_token_in_the_environment(
    client: TestClient, queue: _FakeQueue, keycloak: _Keycloak, tmp_path: Path
) -> None:
    _world()

    response = _install(client, approve=True)

    assert response.status_code == 202, response.text
    (exchange,) = keycloak.exchanges
    assert exchange["subject_token"] == [_ADMIN_ACCESS]
    assert exchange["audience"] == ["flow"]
    log = _run(queue, tmp_path)
    argv = _argv()
    assert argv[:3] == ["addon", "install", "archive"]
    assert "--approve-flow-link" in argv
    flow_token = keycloak.flow_token()
    assert _token_seen() == flow_token
    assert not any(flow_token in arg for arg in argv), "the token is never an argument"
    assert not any(arg.startswith("--flow-token-file") for arg in argv)
    assert "Flow link of archive approved at install." in log
    assert flow_token not in log and _ADMIN_ACCESS not in log
    (entry,) = _audit()
    assert entry["action"] == "install"
    assert entry["params"]["flow_link"] == "approved"
    assert entry["params"]["flow_link_confirmed"] is True
    assert flow_token not in json.dumps(entry)


def test_a_link_flow_keeps_for_a_second_person_is_recorded_with_its_reasons(
    client: TestClient, queue: _FakeQueue, tmp_path: Path
) -> None:
    _world(answer="proposal")

    assert _install(client, approve=True).status_code == 202
    _run(queue, tmp_path)

    (entry,) = _audit()
    assert entry["params"]["flow_link"] == "proposal"
    assert entry["params"]["flow_link_reasons"] == ["egress:tag_document", "data_class:find"]


def test_without_a_confirmation_there_is_no_flag_and_no_token(
    client: TestClient, queue: _FakeQueue, keycloak: _Keycloak, tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _world()
    # A token the manager's own environment happens to carry never reaches papaia-ctl.
    monkeypatch.setenv("PAPAIA_FLOW_TOKEN", "inherited")

    assert _install(client, approve=None).status_code == 202
    _run(queue, tmp_path)

    assert keycloak.exchanges == []
    assert "--approve-flow-link" not in _argv()
    assert _token_seen() == "<unset>"
    (entry,) = _audit()
    assert entry["params"]["flow_link_confirmed"] is False
    assert entry["params"]["flow_link"] == "no_token"


def test_an_add_on_without_a_section_records_no_link(
    client: TestClient, queue: _FakeQueue, tmp_path: Path
) -> None:
    _world(section=False)
    (_WORKSPACE / "papaia" / "tools" / "papaia-ctl").write_text(
        "#!/usr/bin/env bash\necho \"Addon installed: $3\"\n", encoding="utf-8"
    )

    assert _install(client, approve=False).status_code == 202
    _run(queue, tmp_path)

    assert _audit()[0]["params"]["flow_link"] == "none"


@pytest.mark.parametrize(
    "world",
    [{"flow": False}, {"section": False}, {"core_approves": False}],
    ids=["flow-not-installed", "no-flow-section", "core-without-the-flag"],
)
def test_a_confirmation_with_nothing_to_approve_is_refused(
    client: TestClient, queue: _FakeQueue, keycloak: _Keycloak, world: dict[str, bool]
) -> None:
    _world(**world)

    response = _install(client, approve=True)

    assert response.status_code == 409, response.text
    assert queue.enqueued == []
    assert keycloak.exchanges == []


def test_a_refused_exchange_queues_nothing(
    client: TestClient, queue: _FakeQueue, keycloak: _Keycloak
) -> None:
    _world()
    keycloak.status = 403

    response = _install(client, approve=True)

    assert response.status_code == 502, response.text
    assert queue.enqueued == []


def test_a_token_for_more_than_flow_is_not_used(
    client: TestClient, queue: _FakeQueue, keycloak: _Keycloak
) -> None:
    _world()
    keycloak.aud = ["flow", "account"]

    assert _install(client, approve=True).status_code == 502
    assert queue.enqueued == []


def test_a_session_without_a_refresh_token_signs_in_again(
    client: TestClient, queue: _FakeQueue
) -> None:
    _world()

    assert _install(client, approve=True, refresh=False).status_code == 401
    assert queue.enqueued == []


def test_a_token_that_expired_while_the_job_waited_is_not_passed(
    client: TestClient, queue: _FakeQueue, keycloak: _Keycloak, tmp_path: Path
) -> None:
    _world()
    keycloak.expires_in = 0

    assert _install(client, approve=True).status_code == 202
    log = _run(queue, tmp_path)

    assert _token_seen() == "<unset>"
    assert "expired" in log
    assert _audit()[0]["params"]["flow_link"] == "no_token"
