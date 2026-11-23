"""Upgrade of the manager's own data from 1.6.0 to 1.8.0 (PLT-34, decision E-05).

`tests/fixtures/manager-1.6.0/` is the `manager/` folder of a configuration directory as manager
1.6.0 writes it, produced by the 1.6.0 code itself (`tests/fixtures/make_manager_1_6_0.py`):
branding, logo and host refresh, the backup schedule, dashboard tiles, add-on catalogs, an
installed add-on with an earlier version, one account's language, a notification recipient, a
saved overlay version and audit entries. 1.8.0 has to read every one of them as written.

One value moves: the sidebar brand of 1.6.0 (`manager/settings.yaml`, `manager/branding/`) is
shown by 1.8.0 only while the core publishes no platform catalog. With papAIa core 1.10.0 the
catalog's brand is the one every module shows, so the manager hands the old brand to the core's
installation settings once (`app.core.migrations`), and records that in
`manager/migrations.json`. These tests replace the core's settings commands by the `FakeCore` of
`test_installation_settings.py`.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import sqlite3
import tempfile
import time
from collections.abc import Iterator
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
    "PAPAIA_CONFIG_DIR": tempfile.mkdtemp(prefix="papaia-upgrade-config-"),
    "PAPAIA_WORKSPACE_DIR": tempfile.mkdtemp(prefix="papaia-upgrade-workspace-"),
}.items():
    os.environ.setdefault(_key, _value)

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app.config import Settings, get_settings  # noqa: E402
from app.core import (  # noqa: E402
    catalogs,
    ctl,
    kit_audit,
    migrations,
    module_catalog,
    schedule,
    settings_store,
    snapshots,
    tiles,
    user_prefs,
)
from app.core.audit import AuditFilter, audit_path, query_entries  # noqa: E402
from app.core.config import history  # noqa: E402
from app.core.notifications import store as notifications_store  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.test_installation_settings import FakeCore  # noqa: E402

DATA_SET = Path(__file__).parent / "fixtures" / "manager-1.6.0"
LEGACY_LOGO = DATA_SET / "manager" / "branding" / "logo-a845481d6c10.png"
_CSRF = "test-csrf-token-value"
_SVG = b'<svg xmlns="http://www.w3.org/2000/svg"><rect width="1" height="1"/></svg>'


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    """A configuration directory holding the 1.6.0 data set."""
    directory = tmp_path / "config"
    directory.mkdir()
    shutil.copytree(DATA_SET / "manager", directory / "manager")
    return directory


@pytest.fixture
def settings(config_dir: Path, tmp_path: Path) -> Iterator[Settings]:
    get_settings.cache_clear()
    module_catalog.reset_readers()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    yield get_settings().model_copy(
        update={
            "papaia_config_dir": str(config_dir),
            "papaia_workspace_dir": str(workspace),
            "manager_identity_admin_role": "papaia-admin",
        }
    )
    get_settings.cache_clear()
    module_catalog.reset_readers()


@pytest.fixture
def core(config_dir: Path, monkeypatch: pytest.MonkeyPatch) -> FakeCore:
    """papAIa core 1.10.0: the settings commands and a rendered platform catalog."""
    fake = FakeCore(config_dir)

    async def run_py(*, command: str, workspace_dir: str, config_dir: str,
                     extra_flags: list[str] | None = None, **_: Any) -> tuple[int, str, str]:
        return fake.run(["--config-dir", config_dir, command, *(extra_flags or [])])

    monkeypatch.setattr(ctl, "_run_py", run_py)
    fake.render()
    return fake


def _run(settings: Settings) -> list[migrations.Outcome]:
    return asyncio.run(migrations.run_pending(settings))


def _sets(core: FakeCore) -> list[list[str]]:
    return [call for call in core.calls if call[0] == "settings-set"]


def _ledger(config_dir: Path) -> dict[str, Any]:
    return json.loads((config_dir / "manager" / "migrations.json").read_text(encoding="utf-8"))


def _log(config_dir: Path) -> list[dict[str, Any]]:
    path = audit_path(str(config_dir))
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _outbox(config_dir: Path) -> list[dict[str, Any]]:
    path = kit_audit.outbox_path(str(config_dir))
    if not path.exists():
        return []
    with sqlite3.connect(path) as conn:
        found = conn.execute("SELECT body FROM audit_outbox ORDER BY source_seq").fetchall()
    return [json.loads(body) for (body,) in found]


def _write_legacy(config_dir: Path, branding: dict[str, Any]) -> None:
    (config_dir / "manager" / "settings.yaml").write_text(
        json.dumps({"branding": branding, "host": {"refresh_seconds": 120}}), encoding="utf-8"
    )


# -- every file of 1.6.0 is read by 1.8.0 as written ------------------------------------------


def test_the_settings_of_1_6_0_are_read(config_dir: Path) -> None:
    stored = settings_store.load_settings(str(config_dir))
    assert stored.branding.name == "Beispiel GmbH"
    assert stored.branding.tagline == "IT-Betrieb"
    assert stored.branding.logo == LEGACY_LOGO.name
    assert stored.host.refresh_seconds == 120
    logo = settings_store.logo_path(str(config_dir), stored)
    assert logo is not None and logo.read_bytes() == LEGACY_LOGO.read_bytes()


def test_the_backup_schedule_of_1_6_0_is_read(config_dir: Path) -> None:
    loaded = schedule.load_schedule(str(config_dir))
    assert loaded.error is None
    assert loaded.schedule is not None
    assert loaded.schedule.cron == "30 2 * * *"
    assert loaded.schedule.timezone == "Europe/Berlin"
    assert loaded.schedule.retention_days == 14


def test_the_dashboard_of_1_6_0_is_read(config_dir: Path) -> None:
    loaded = tiles.load_tiles(str(config_dir))
    [group] = loaded.groups
    assert group.name == "Arbeitsplatz"
    link, widget = group.tiles
    assert isinstance(link, tiles.Tile) and link.href == "https://wiki.example.org"
    assert isinstance(widget, tiles.WidgetTile) and widget.widget == "core_version"


def test_the_catalogs_and_the_installed_add_on_of_1_6_0_are_read(config_dir: Path) -> None:
    registry = catalogs.load_registry(str(config_dir))
    assert [(c.name, c.type) for c in registry.catalogs] == [("fidonis", "git"),
                                                              ("haus", "local")]
    installed = snapshots.load_installed(str(config_dir))
    assert installed["paperless"].manifest_version == "2.0.0"
    assert [h.manifest_version for h in installed["paperless"].history] == ["1.9.0"]


def test_the_language_notifications_and_overlay_history_of_1_6_0_are_read(
    config_dir: Path,
) -> None:
    subject = "22222222-2222-2222-2222-222222222222"
    assert user_prefs.stored_locale(str(config_dir), subject) == "de"

    loaded = notifications_store.load_config(str(config_dir))
    assert loaded.error is None
    [recipient] = loaded.config.recipients
    assert recipient.target == "betrieb@example.org"
    assert [e.value for e in recipient.events] == ["job_failed", "cert_expiring"]
    assert loaded.config.settings.check_interval_minutes == 10

    [version] = history.versions(str(config_dir), "litellm")
    assert history.read(str(config_dir), "litellm", version.id) == b"model_list: []\n"


def test_the_audit_log_of_1_6_0_is_read_and_continued(config_dir: Path) -> None:
    page = query_entries(str(config_dir), AuditFilter(), limit=10, offset=0)
    assert page.corrupt_lines == 0
    assert [e.action for e in page.entries] == ["addon.install", "settings.branding.update"]


def test_the_1_8_0_app_serves_the_1_6_0_data(settings: Settings) -> None:
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    client = TestClient(app, follow_redirects=False)
    session = {
        "user": {"sub": "11111111-1111-1111-1111-111111111111", "preferred_username": "dora",
                 "roles": ["admin"], "exp": int(time.time()) + 3600},
        "_csrf_token": _CSRF,
    }
    signed = TimestampSigner(settings.manager_session_secret).sign(
        base64.b64encode(json.dumps(session).encode())
    ).decode()
    client.cookies.set("papaia_manager_session", signed)

    assert client.get("/api/v1/settings").json()["host"]["refresh_seconds"] == 120
    assert client.get("/api/v1/maintenance/schedule").json()["schedule"]["cron"] == "30 2 * * *"
    recipients = client.get("/api/v1/notifications").json()["recipients"]
    assert [r["name"] for r in recipients] == ["Betrieb"]
    assert [c["name"] for c in client.get("/api/v1/catalogs").json()] == ["fidonis", "haus"]
    assert "Arbeitsplatz" in json.dumps(client.get("/api/v1/tiles").json())
    assert "addon.install" in client.get("/partials/audit").text


def test_reading_the_data_set_rewrites_none_of_it(config_dir: Path, settings: Settings) -> None:
    before = {p.relative_to(config_dir): p.read_bytes()
              for p in (config_dir / "manager").rglob("*") if p.is_file()}
    test_the_1_8_0_app_serves_the_1_6_0_data(settings)
    after = {p.relative_to(config_dir): p.read_bytes()
             for p in (config_dir / "manager").rglob("*") if p.is_file()}
    assert {k: v for k, v in after.items() if k in before} == before


# -- the one move: the sidebar brand goes to the core's installation settings ------------------


def test_the_brand_of_1_6_0_is_handed_to_the_core_once(
    settings: Settings, core: FakeCore, config_dir: Path
) -> None:
    [outcome] = _run(settings)

    assert outcome.id == "1.8.0-branding"
    assert outcome.done
    assert outcome.fields == {
        "branding.name": "applied", "branding.suffix": "applied", "branding.logo": "applied",
    }
    assert core.overlay == {"name": "Beispiel GmbH", "suffix": "IT-Betrieb"}
    assert core.logo == LEGACY_LOGO.read_bytes()
    # Every module now shows it: the catalog carries the brand and the logo's hash.
    catalog = json.loads((config_dir / "platform" / "services.json").read_text(encoding="utf-8"))
    assert catalog["branding"]["name"] == "Beispiel GmbH"
    assert catalog["branding"]["suffix"] == "IT-Betrieb"
    assert "logo_sha256" in catalog["branding"]
    # Recorded, so the next start asks the core nothing.
    assert _ledger(config_dir)["migrations"]["1.8.0-branding"]["fields"] == outcome.fields
    calls = len(core.calls)
    [again] = _run(settings)
    assert again.done and again.fields == outcome.fields
    assert len(core.calls) == calls
    # The manager's own copy is left as it was: a rollback to 1.7.0 still finds it.
    assert settings_store.load_settings(str(config_dir)).branding.name == "Beispiel GmbH"


def test_the_hand_over_is_audited_without_the_values(
    settings: Settings, core: FakeCore, config_dir: Path
) -> None:
    _run(settings)

    entry = _log(config_dir)[-1]
    assert entry["action"] == "migration.branding"
    assert entry["user"] == "papaia-manager"
    assert entry["result"] == "ok"
    assert "Beispiel GmbH" not in json.dumps(entry)
    assert "IT-Betrieb" not in json.dumps(entry)
    [row] = _outbox(config_dir)
    assert row["operation"] == "migration.branding"
    assert row["identity_kind"] == "service"
    assert row["result"] == "allowed"
    assert row["correlation_id"]
    # The core wrote one row of its own per setting.
    assert [r["object"] for r in core.rows] == ["branding.name", "branding.suffix",
                                                "branding.logo"]


def test_a_brand_already_set_in_the_core_is_kept(
    settings: Settings, core: FakeCore, config_dir: Path
) -> None:
    core.overlay["name"] = "Neue Plattform"
    core.logo = b"\x89PNG\r\n\x1a\nother"
    core.render()

    [outcome] = _run(settings)

    assert outcome.fields == {
        "branding.name": "kept", "branding.suffix": "applied", "branding.logo": "kept",
    }
    assert _sets(core) == [["settings-set", "--key=branding.suffix", "--value=IT-Betrieb"]]
    assert core.overlay["name"] == "Neue Plattform"


def test_the_defaults_of_1_6_0_hand_over_nothing(
    settings: Settings, core: FakeCore, config_dir: Path
) -> None:
    # No name, the default second line, no logo: the platform keeps its own defaults.
    (config_dir / "manager" / "settings.yaml").unlink()

    [outcome] = _run(settings)

    assert outcome.done and outcome.fields == {}
    assert core.calls == []
    assert _ledger(config_dir)["migrations"]["1.8.0-branding"]["fields"] == {}
    assert not audit_path(str(config_dir)).read_text(encoding="utf-8").count("migration.")


def test_a_hidden_second_line_is_not_a_suffix(
    settings: Settings, core: FakeCore, config_dir: Path
) -> None:
    _write_legacy(config_dir, {"name": "Beispiel GmbH", "tagline": ""})

    [outcome] = _run(settings)

    assert outcome.fields == {"branding.name": "applied"}
    assert "suffix" not in core.overlay


def test_an_svg_logo_stays_with_the_manager(
    settings: Settings, core: FakeCore, config_dir: Path
) -> None:
    folder = config_dir / "manager" / "branding"
    (folder / "logo-0123456789ab.svg").write_bytes(_SVG)
    _write_legacy(config_dir, {"name": "Beispiel GmbH", "logo": "logo-0123456789ab.svg"})

    [outcome] = _run(settings)

    assert outcome.done
    assert outcome.fields == {"branding.name": "applied", "branding.logo": "unsupported"}
    assert core.logo is None
    assert _log(config_dir)[-1]["params"]["fields"]["branding.logo"] == "unsupported"


def test_a_value_the_core_refuses_is_recorded_and_not_asked_again(
    settings: Settings, core: FakeCore, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = core.run

    def refusing(argv: list[str]) -> tuple[int, str, str]:
        if "--key=branding.suffix" in argv:
            return 2, "", "ERROR: branding.suffix: refused"
        return run(argv)

    monkeypatch.setattr(core, "run", refusing)

    [outcome] = _run(settings)

    assert outcome.done
    assert outcome.fields["branding.suffix"] == "refused"
    assert outcome.fields["branding.name"] == "applied"
    assert _log(config_dir)[-1]["result"] == "refused"
    calls = len(core.calls)
    _run(settings)
    assert len(core.calls) == calls


def test_a_core_that_cannot_be_reached_is_asked_again_at_the_next_start(
    settings: Settings, core: FakeCore, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = core.run

    def broken(argv: list[str]) -> tuple[int, str, str]:
        if "settings-show" in argv:
            raise ctl.CtlError("lib.cli did not answer", 124)
        return run(argv)

    monkeypatch.setattr(core, "run", broken)
    [outcome] = _run(settings)
    assert not outcome.done
    assert not (config_dir / "manager" / "migrations.json").exists()
    assert _log(config_dir)[-1]["result"] == "failed"

    monkeypatch.setattr(core, "run", run)
    [outcome] = _run(settings)
    assert outcome.done
    assert core.overlay["name"] == "Beispiel GmbH"


def test_without_a_platform_catalog_the_brand_waits(
    settings: Settings, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Core 1.8.0 / 1.9.0 has no catalog and no settings commands; the manager still shows the
    # brand itself there, so nothing is lost by waiting.
    def no_core(**_: Any) -> Any:
        raise AssertionError("the core must not be asked")

    monkeypatch.setattr(ctl, "_run_py", no_core)

    [outcome] = _run(settings)

    assert not outcome.done
    assert not (config_dir / "manager" / "migrations.json").exists()
    assert settings_store.effective_branding(str(config_dir)).name == "Beispiel GmbH"


def test_a_ledger_that_cannot_be_read_is_not_overwritten(
    settings: Settings, core: FakeCore, config_dir: Path
) -> None:
    ledger = config_dir / "manager" / "migrations.json"
    ledger.write_text("{not json", encoding="utf-8")

    assert _run(settings) == []

    assert core.calls == []
    assert ledger.read_text(encoding="utf-8") == "{not json"


def test_the_ledger_is_private(settings: Settings, core: FakeCore, config_dir: Path) -> None:
    _run(settings)
    assert (config_dir / "manager" / "migrations.json").stat().st_mode & 0o777 == 0o600


def test_the_manager_runs_the_migrations_when_it_starts(
    settings: Settings, core: FakeCore, config_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PAPAIA_CONFIG_DIR", settings.papaia_config_dir)
    monkeypatch.setenv("PAPAIA_WORKSPACE_DIR", settings.papaia_workspace_dir)
    get_settings.cache_clear()

    with TestClient(create_app()):
        deadline = time.monotonic() + 10
        while not (config_dir / "manager" / "migrations.json").exists():
            assert time.monotonic() < deadline, "the migrations did not run"
            time.sleep(0.05)

    assert core.overlay["name"] == "Beispiel GmbH"
