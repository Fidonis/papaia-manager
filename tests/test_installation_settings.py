"""Settings -> Installation: name, suffix, logo and contact point, the four-eyes rule and the
delegation lifetime, kept by the core and published in the platform catalog (PLT-28, PLT-82,
E-18, sicherheit 1.3 and 1.10).

The manager stores none of them: each change is handed to the core's command (`settings-set`,
`settings-unset`, `delegation-lifetime`, then `catalog-render`), which these tests replace by
`FakeCore`. It keeps the values, applies the security profile as the core does (`kritis`: four
eyes always on, lifetime at most 7 / 30 days, refusals with the core's exit codes) and renders a
catalog every module reads. `test_installation_contract.py` runs the same routes against the
core's real code.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import tempfile
import time
from collections.abc import AsyncGenerator, Iterator
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from typing import Any

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
    "PAPAIA_CONFIG_DIR": tempfile.mkdtemp(prefix="papaia-installation-config-"),
    "PAPAIA_WORKSPACE_DIR": tempfile.mkdtemp(prefix="papaia-installation-workspace-"),
}.items():
    os.environ.setdefault(_key, _value)

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402
from papaia_kit.audit import validate_event  # noqa: E402
from papaia_kit.catalog import MAX_AGE_SECONDS, ServicesReader  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core import ctl, kit_audit, module_catalog  # noqa: E402
from app.core.audit import audit_path  # noqa: E402
from app.main import create_app  # noqa: E402

_CSRF = "test-csrf-token-value"
_BASE = "/api/v1/settings/installation"
_PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'
_EXAMPLE = "_data/contract/examples/platform-services.example.json"


class FakeCore:
    """The core's settings commands as the manager sees them: exit code, stdout, stderr."""

    def __init__(self, config_dir: Path, profile: str = "standard") -> None:
        self.config_dir = config_dir
        self.profile = profile
        self.overlay: dict[str, Any] = {}
        self.logo: bytes | None = None
        self.four_eyes = "off"
        self.lifetime: tuple[int, int] | None = None
        self.calls: list[list[str]] = []
        self.logo_seen: bytes | None = None
        self.rows: list[dict[str, Any]] = []
        self.synced = 0

    # -- what the core keeps ----------------------------------------------------------------

    def _in_effect(self) -> str:
        return "on" if self.profile == "kritis" else self.four_eyes

    def _bounds(self) -> tuple[int, int]:
        return (7, 30) if self.profile == "kritis" else (365, 365)

    def _lifetime(self) -> tuple[int, int]:
        default = (7, 30) if self.profile == "kritis" else (90, 180)
        return self.lifetime or default

    def render(self) -> None:
        doc = json.loads((resources.files("papaia_kit") / _EXAMPLE).read_text(encoding="utf-8"))
        doc["generated_at"] = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        doc["security_profile"] = self.profile
        branding: dict[str, Any] = {"name": self.overlay.get("name", "papAIa")}
        for key in ("suffix", "contact"):
            if key in self.overlay:
                branding[key] = self.overlay[key]
        platform = self.config_dir / "platform"
        platform.mkdir(exist_ok=True)
        if self.logo is not None:
            branding["logo_sha256"] = hashlib.sha256(self.logo).hexdigest()
            (platform / "brand-logo").write_bytes(self.logo)
        doc["branding"] = branding
        doc["approvals"] = {"four_eyes": self._in_effect()}
        idle, age = self._lifetime()
        doc["delegations"] = {"idle_days": idle, "max_days": age}
        (platform / "services.json").write_text(json.dumps(doc), encoding="utf-8")

    def _row(self, event: str, key: str, result: str) -> None:
        self.rows.append({"event": event, "object": key, "result": result})

    # -- the commands -----------------------------------------------------------------------

    def _flags(self, argv: list[str]) -> dict[str, str]:
        return dict(a[2:].split("=", 1) for a in argv if a.startswith("--") and "=" in a)

    def run(self, argv: list[str]) -> tuple[int, str, str]:
        command = argv[argv.index("--config-dir") + 2]
        self.calls.append(argv[argv.index(command):])
        flags = self._flags(argv[argv.index(command) + 1:])
        if command == "settings-show":
            branding = {k: v for k, v in self.overlay.items() if k != "contact"}
            branding.setdefault("name", "papAIa")
            if self.logo is not None:
                branding["logo_sha256"] = hashlib.sha256(self.logo).hexdigest()
            return 0, json.dumps({
                "security_profile": self.profile,
                "branding": branding,
                "contact": self.overlay.get("contact"),
                "approvals": {"four_eyes": self._in_effect(),
                              "fixed": self.profile == "kritis"},
            }), ""
        if command == "settings-set":
            return self._set(flags["key"], flags["value"])
        if command == "settings-unset":
            key = flags["key"]
            if key == "branding.logo":
                self.logo = None
            elif key == "contact":
                self.overlay.pop("contact", None)
            elif key.startswith("contact."):
                self.overlay.get("contact", {}).pop(key.split(".", 1)[1], None)
            else:
                self.overlay.pop(key.split(".", 1)[1], None)
            self._row("config_changed", key, "allowed")
            self.render()
            return 0, "", ""
        if command == "delegation-lifetime":
            if "--show" in argv:
                idle, age = self._lifetime()
                limit = self._bounds()
                return 0, json.dumps({
                    "security_profile": self.profile, "idle_days": idle, "max_days": age,
                    "source": "setting" if self.lifetime else "default", "within_bound": True,
                    "upper_bound": {"idle_days": limit[0], "max_days": limit[1]},
                }), ""
            idle, age = int(flags["idle-days"]), int(flags["max-days"])
            limit = self._bounds()
            if idle > limit[0] or age > limit[1]:
                self._row("delegation_lifetime_changed", "delegations", "denied")
                return 2, "", (
                    "Delegation lifetime refused: delegations.idle_days: must be 1 to "
                    f"{limit[0]} days in security profile {self.profile}. Nothing was changed."
                )
            self.lifetime = (idle, age)
            self._row("delegation_lifetime_changed", "delegations", "allowed")
            return 0, "Delegation lifetime set.", ""
        if command == "catalog-render":
            self.render()
            return 0, "Platform catalog written.", ""
        raise AssertionError(f"unexpected core command {command}")

    def _set(self, key: str, value: str) -> tuple[int, str, str]:
        if key == "branding.logo":
            self.logo = self.logo_seen = Path(value).read_bytes()
            self._row("config_changed", key, "allowed")
        elif key == "approvals.four_eyes":
            if self.profile == "kritis" and value == "off":
                self._row("four_eyes_changed", key, "denied")
                return 1, "", (
                    "ERROR: approvals.four_eyes stays on: security profile kritis does not "
                    "allow switching the four-eyes rule off"
                )
            self.four_eyes = value
            self._row("four_eyes_changed", key, "allowed")
        elif key.startswith("contact."):
            field = key.split(".", 1)[1]
            if field != "name" and "contact" not in self.overlay:
                return 2, "", "ERROR: set contact.name first"
            self.overlay.setdefault("contact", {})[field] = value
            self._row("config_changed", key, "allowed")
        else:
            self.overlay[key.split(".", 1)[1]] = value
            self._row("config_changed", key, "allowed")
        self.render()
        return 0, "", ""


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    (directory / "manager").mkdir(parents=True)
    return directory


@pytest.fixture
def core(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCore:
    fake = FakeCore(config_dir)

    async def run_py(*, command: str, workspace_dir: str, config_dir: str,
                     extra_flags: list[str] | None = None, **_: Any) -> tuple[int, str, str]:
        return fake.run(["--config-dir", config_dir, command, *(extra_flags or [])])

    async def role_sync(*, verb: str, **_: Any) -> AsyncGenerator[str, None]:
        assert verb == "keycloak-role-sync"
        fake.synced += 1

        async def lines() -> AsyncGenerator[str, None]:
            yield "realm in sync"

        return lines()

    monkeypatch.setattr(ctl, "_run_py", run_py)
    monkeypatch.setattr(ctl, "run_core_verb", role_sync)
    return fake


@pytest.fixture
def client(config_dir: Path, tmp_path: Path, core: FakeCore) -> Iterator[TestClient]:
    get_settings.cache_clear()
    module_catalog.reset_readers()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    settings = get_settings().model_copy(
        update={
            "papaia_config_dir": str(config_dir),
            "papaia_workspace_dir": str(workspace),
            "manager_identity_admin_role": "papaia-admin",
        }
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    yield TestClient(app, follow_redirects=False)
    get_settings.cache_clear()
    module_catalog.reset_readers()


def _as(client: TestClient, *roles: str) -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": "11111111-1111-1111-1111-111111111111",
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
    return client


def _dora(client: TestClient) -> TestClient:
    return _as(client, "admin", "papaia-admin")


def _put(client: TestClient, path: str, body: dict[str, Any]) -> Any:
    return _dora(client).put(f"{_BASE}{path}", json=body, headers={"X-CSRF-Token": _CSRF})


def _log(config_dir: Path) -> list[dict[str, Any]]:
    path = audit_path(str(config_dir))
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _outbox(config_dir: Path) -> list[dict[str, Any]]:
    path = kit_audit.outbox_path(str(config_dir))
    if not path.exists():
        return []
    with sqlite3.connect(path) as conn:
        found = conn.execute("SELECT body FROM audit_outbox ORDER BY source_seq").fetchall()
    return [json.loads(body) for (body,) in found]


def _sets(core: FakeCore) -> list[list[str]]:
    return [call for call in core.calls if call[0] != "settings-show"
            and "--show" not in call]


# -- brand: name, suffix, logo ------------------------------------------------------------


def test_saving_the_name_reaches_a_stub_module_within_60_seconds(
    client: TestClient, core: FakeCore, config_dir: Path
) -> None:
    core.render()
    now = [1000.0]
    stub = ServicesReader(config_dir / "platform" / "services.json", clock=lambda: now[0])
    before = stub.get()
    assert before is not None and before.branding is not None
    assert before.branding["name"] == "papAIa"

    response = _put(client, "/brand", {"name": "Beispiel GmbH", "suffix": "Arbeitsplattform"})

    assert response.status_code == 200
    assert response.json()["branding"] == {
        "name": "Beispiel GmbH", "suffix": "Arbeitsplattform", "logo": False,
    }
    assert _sets(core) == [
        ["settings-set", "--key=branding.name", "--value=Beispiel GmbH"],
        ["settings-set", "--key=branding.suffix", "--value=Arbeitsplattform"],
    ]
    # A module reads the catalog again once its copy is older than the kit's maximum age.
    assert MAX_AGE_SECONDS <= 60
    now[0] += MAX_AGE_SECONDS + 0.001
    after = stub.get()
    assert after is not None and after.branding is not None
    assert after.branding["name"] == "Beispiel GmbH"
    assert after.branding["suffix"] == "Arbeitsplattform"


def test_the_manager_shows_the_new_name_at_once(client: TestClient, core: FakeCore) -> None:
    core.render()
    assert "Beispiel GmbH" not in _dora(client).get("/help").text

    assert _put(client, "/brand", {"name": "Beispiel GmbH", "suffix": ""}).status_code == 200

    assert "Beispiel GmbH" in _dora(client).get("/help").text


def test_an_empty_field_removes_the_value(client: TestClient, core: FakeCore) -> None:
    core.overlay = {"name": "Old", "suffix": "Line"}
    assert _put(client, "/brand", {"name": " ", "suffix": None}).status_code == 200
    assert _sets(core) == [
        ["settings-unset", "--key=branding.name"],
        ["settings-unset", "--key=branding.suffix"],
    ]


@pytest.mark.parametrize(
    "body",
    [
        {"name": "x" * 61},
        {"suffix": "x" * 61},
        {"name": "tab\there"},
        {"name": "ok", "colour": "red"},
        {"name": 5},
    ],
)
def test_an_invalid_brand_is_refused_before_the_core_is_asked(
    client: TestClient, core: FakeCore, body: dict[str, Any], config_dir: Path
) -> None:
    assert _put(client, "/brand", body).status_code == 422
    assert _sets(core) == []
    assert _outbox(config_dir) == []


def test_a_logo_goes_to_the_core_and_every_module_serves_it(
    client: TestClient, core: FakeCore, config_dir: Path
) -> None:
    response = _dora(client).post(
        f"{_BASE}/logo", files={"file": ("logo.png", _PNG, "image/png")},
        headers={"X-CSRF-Token": _CSRF},
    )

    assert response.status_code == 200
    assert response.json()["branding"]["logo"] is True
    assert core.logo_seen == _PNG
    # The file handed to the core is gone again.
    assert [p.name for p in (config_dir / "manager").iterdir()
            if p.name.startswith(".brand-logo-")] == []
    served = _dora(client).get("/_kit/brand/logo")
    assert served.status_code == 200
    assert served.content == _PNG
    assert served.headers["content-type"] == "image/png"


@pytest.mark.parametrize("data", [_SVG, b"", b"GIF89a....", _PNG + b"\0" * (1024 * 1024)])
def test_a_logo_the_core_does_not_take_is_refused_here(
    client: TestClient, core: FakeCore, data: bytes
) -> None:
    response = _dora(client).post(
        f"{_BASE}/logo", files={"file": ("logo", data, "image/png")},
        headers={"X-CSRF-Token": _CSRF},
    )
    assert response.status_code == 422
    assert _sets(core) == []


def test_removing_the_logo_unsets_it_in_the_core(client: TestClient, core: FakeCore) -> None:
    core.logo = _PNG
    response = _dora(client).delete(f"{_BASE}/logo", headers={"X-CSRF-Token": _CSRF})
    assert response.status_code == 200
    assert _sets(core) == [["settings-unset", "--key=branding.logo"]]


# -- contact point ------------------------------------------------------------------------


def test_the_contact_point_is_stored_by_the_core_and_audited_without_its_values(
    client: TestClient, core: FakeCore, config_dir: Path
) -> None:
    response = _put(client, "/contact",
                    {"name": "IT-Service", "mail": "it@example.org", "phone": ""})

    assert response.status_code == 200
    assert response.json()["contact"] == {"name": "IT-Service", "mail": "it@example.org"}
    assert _sets(core) == [
        ["settings-set", "--key=contact.name", "--value=IT-Service"],
        ["settings-set", "--key=contact.mail", "--value=it@example.org"],
        ["settings-unset", "--key=contact.phone"],
    ]
    text = audit_path(str(config_dir)).read_text(encoding="utf-8")
    assert "IT-Service" not in text and "it@example.org" not in text
    [entry] = _log(config_dir)
    assert entry["action"] == "settings.installation.contact"
    assert entry["params"] == {"keys": ["contact.name", "contact.mail", "contact.phone"]}


def test_without_a_name_the_contact_point_is_removed(client: TestClient, core: FakeCore) -> None:
    core.overlay = {"contact": {"name": "IT"}}
    assert _put(client, "/contact", {"name": ""}).status_code == 200
    assert _sets(core) == [["settings-unset", "--key=contact"]]


@pytest.mark.parametrize(
    "body",
    [
        {"name": "", "mail": "it@example.org"},
        {"name": "IT", "mail": "not-a-mail"},
        {"name": "IT", "phone": "call me"},
        {"name": "x" * 121},
        {"name": "IT", "fax": "123"},
    ],
)
def test_an_invalid_contact_point_is_refused_before_the_core_is_asked(
    client: TestClient, core: FakeCore, body: dict[str, Any]
) -> None:
    assert _put(client, "/contact", body).status_code == 422
    assert _sets(core) == []


def test_a_denial_names_the_contact_point(client: TestClient, core: FakeCore) -> None:
    core.overlay = {"contact": {"name": "IT-Service", "mail": "it@example.org"}}
    core.render()
    page = _as(client, "user").get("/settings")
    assert page.status_code == 403
    assert "IT-Service" in page.text and "it@example.org" in page.text


def test_without_a_contact_point_a_denial_says_so(client: TestClient, core: FakeCore) -> None:
    core.render()
    page = _as(client, "user").get("/settings")
    assert page.status_code == 403
    # The wording UXA-N04-A2 asks for, in the language of the page (German by default).
    assert "Ansprechstelle nicht hinterlegt" in page.text


# -- four eyes ----------------------------------------------------------------------------


def test_switching_four_eyes_is_audited_by_the_core_and_the_manager(
    client: TestClient, core: FakeCore, config_dir: Path
) -> None:
    on = _put(client, "/four-eyes", {"four_eyes": "on"})
    off = _put(client, "/four-eyes", {"four_eyes": "off"})

    assert on.status_code == 200 and on.json()["four_eyes"] is True
    assert off.status_code == 200 and off.json()["four_eyes"] is False
    assert core.rows == [
        {"event": "four_eyes_changed", "object": "approvals.four_eyes", "result": "allowed"},
        {"event": "four_eyes_changed", "object": "approvals.four_eyes", "result": "allowed"},
    ]
    assert [(e["action"], e["params"], e["result"]) for e in _log(config_dir)] == [
        ("settings.installation.four_eyes", {"four_eyes": "on"}, "ok"),
        ("settings.installation.four_eyes", {"four_eyes": "off"}, "ok"),
    ]
    rows = _outbox(config_dir)
    assert len(rows) == 2
    for row in rows:
        validate_event(row, stage="submission")
        assert row["event"] == "config_changed"
        assert row["object_ref"] == {"type": "setting", "id": "approvals.four_eyes"}
        assert row["sub"] == "11111111-1111-1111-1111-111111111111"
        assert row["identity_kind"] == "person"
        assert row["result"] == "allowed"


def test_in_kritis_four_eyes_is_shown_as_fixed(client: TestClient, core: FakeCore) -> None:
    core.profile = "kritis"
    shown = _dora(client).get(_BASE).json()
    assert shown["four_eyes"] is True and shown["four_eyes_fixed"] is True

    page = _dora(client).get("/settings/installation")
    assert page.status_code == 200
    assert 'id="four-eyes-fixed"' in page.text
    toggle = page.text[page.text.index('id="four-eyes"'):]
    assert "disabled" in toggle[: toggle.index(">")]


def test_in_kritis_switching_four_eyes_off_is_refused_and_audited(
    client: TestClient, core: FakeCore, config_dir: Path
) -> None:
    core.profile = "kritis"
    response = _put(client, "/four-eyes", {"four_eyes": "off"})

    assert response.status_code == 409
    assert "kritis" in response.json()["detail"]
    assert core.rows == [
        {"event": "four_eyes_changed", "object": "approvals.four_eyes", "result": "denied"}
    ]
    [entry] = _log(config_dir)
    assert entry["result"] == "refused"
    [row] = _outbox(config_dir)
    validate_event(row, stage="submission")
    assert row["result"] == "denied"


@pytest.mark.parametrize("body", [{"four_eyes": "ON"}, {"four_eyes": True}, {}, {"value": "on"}])
def test_four_eyes_takes_on_or_off_only(
    client: TestClient, core: FakeCore, body: dict[str, Any]
) -> None:
    assert _put(client, "/four-eyes", body).status_code == 422
    assert _sets(core) == []


# -- delegation lifetime ------------------------------------------------------------------


def test_changing_the_lifetime_is_audited_rendered_and_synced(
    client: TestClient, core: FakeCore, config_dir: Path
) -> None:
    response = _put(client, "/delegations", {"idle_days": 30, "max_days": 90})

    assert response.status_code == 200
    body = response.json()
    assert body["delegations"]["idle_days"] == 30 and body["delegations"]["max_days"] == 90
    assert body["realm_synced"] is True
    assert _sets(core) == [
        ["delegation-lifetime", "--idle-days=30", "--max-days=90"],
        ["catalog-render"],
    ]
    assert core.synced == 1
    assert core.rows == [
        {"event": "delegation_lifetime_changed", "object": "delegations", "result": "allowed"}
    ]
    [entry] = _log(config_dir)
    assert entry["action"] == "settings.installation.delegations"
    assert entry["params"] == {"idle_days": 30, "max_days": 90}
    [row] = _outbox(config_dir)
    validate_event(row, stage="submission")
    assert row["object_ref"] == {"type": "setting", "id": "delegations"}


@pytest.mark.parametrize(("idle", "total"), [(8, 30), (7, 31), (90, 180)])
def test_in_kritis_a_lifetime_above_7_or_30_days_is_refused(
    client: TestClient, core: FakeCore, config_dir: Path, idle: int, total: int
) -> None:
    core.profile = "kritis"
    response = _put(client, "/delegations", {"idle_days": idle, "max_days": total})

    assert response.status_code == 422
    assert "kritis" in response.json()["detail"]
    assert core.lifetime is None
    assert core.synced == 0
    assert core.rows == [
        {"event": "delegation_lifetime_changed", "object": "delegations", "result": "denied"}
    ]
    [entry] = _log(config_dir)
    assert entry["result"] == "refused"


def test_in_kritis_7_and_30_days_are_taken(client: TestClient, core: FakeCore) -> None:
    core.profile = "kritis"
    assert _put(client, "/delegations", {"idle_days": 7, "max_days": 30}).status_code == 200
    assert core.lifetime == (7, 30)


@pytest.mark.parametrize(
    "body",
    [
        {"idle_days": 0, "max_days": 30},
        {"idle_days": 30, "max_days": 366},
        {"idle_days": 60, "max_days": 30},
        {"idle_days": "30", "max_days": 90},
        {"idle_days": True, "max_days": 90},
        {"idle_days": 30, "max_days": 90, "extra": 1},
    ],
)
def test_an_invalid_lifetime_is_refused_before_the_core_is_asked(
    client: TestClient, core: FakeCore, body: dict[str, Any]
) -> None:
    assert _put(client, "/delegations", body).status_code == 422
    assert _sets(core) == []


# -- rights -------------------------------------------------------------------------------


@pytest.mark.parametrize("roles", [("admin",), ("papaia-admin",), ("user",)])
def test_only_an_administrator_with_papaia_admin_reads_or_changes_them(
    client: TestClient, core: FakeCore, roles: tuple[str, ...], config_dir: Path
) -> None:
    who = _as(client, *roles)
    assert who.get(_BASE).status_code == 403
    assert who.get("/settings/installation").status_code == 403
    response = who.put(f"{_BASE}/four-eyes", json={"four_eyes": "on"},
                       headers={"X-CSRF-Token": _CSRF})
    assert response.status_code == 403
    assert core.calls == []
    assert _outbox(config_dir) == []


def test_a_change_needs_the_csrf_token(client: TestClient, core: FakeCore) -> None:
    response = _dora(client).put(f"{_BASE}/four-eyes", json={"four_eyes": "on"})
    assert response.status_code == 403
    assert _sets(core) == []


def test_the_page_shows_what_the_core_keeps(client: TestClient, core: FakeCore) -> None:
    core.overlay = {"name": "Beispiel GmbH", "contact": {"name": "IT-Service"}}
    page = _dora(client).get("/settings/installation")
    assert page.status_code == 200
    assert 'value="Beispiel GmbH"' in page.text
    assert 'value="IT-Service"' in page.text
    assert 'value="90"' in page.text and 'value="180"' in page.text
