"""`POST /api/v1/addons/{name}/update-plan | update | rollback` and `GET /api/v1/addons/updates`.

The version in the catalog is staged and papaia-ctl is asked what an update would do before a job
exists, so the refusals (the compatibility gate, a value the new version needs) are errors of the
request. The job then runs `addon update` and records the outcome in installed.yaml. The queue and
`run_addon_verb` are replaced, so no Docker daemon, no papaia checkout and no worker is needed: the
fake plays papaia-ctl's part, including the one thing the manager depends on, which is that the
registration in deployment.yaml has moved afterwards.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import shutil
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

_SESSION_SECRET = "test-session-secret-value"
_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-addon-update-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-addon-update-workspace-")

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
# PAPAIA_CONFIG_DIR / PAPAIA_WORKSPACE_DIR are set per test by the fixture, for
# the reason spelled out in test_api_restore_scoped.py.

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app import main as app_main  # noqa: E402
from app.config import get_settings  # noqa: E402
from app.core import addon_updates  # noqa: E402
from app.core.catalogs import Catalog, CatalogRegistry, save_registry  # noqa: E402
from app.core.ctl import CtlError  # noqa: E402
from app.core.jobs import Job, JobCallback, JobContext  # noqa: E402
from app.core.snapshots import load_installed, managed_snapshot_path, record_installed  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers import api_addons  # noqa: E402

_CSRF = "test-csrf-token"
_CONFIG = Path(_CONFIG_DIR)
_WORKSPACE = Path(_WORKSPACE_DIR)
_CATALOG_SOURCE = _WORKSPACE / "catalog-source"


class _FakeQueue:
    """Keeps the callback instead of handing it to a worker."""

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
        self.enqueued.append(
            {"action": action, "target": target, "params": params, "callback": callback}
        )
        return Job(
            id="job-1",
            action=action,
            target=target,
            user=user,
            created_at=datetime.now(tz=UTC),
            params=params or {},
        )


@pytest.fixture
def queue(monkeypatch: pytest.MonkeyPatch) -> _FakeQueue:
    fake = _FakeQueue()
    monkeypatch.setattr(app_main, "_job_queue", fake)
    return fake


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, queue: _FakeQueue) -> Iterator[TestClient]:
    monkeypatch.setenv("PAPAIA_CONFIG_DIR", _CONFIG_DIR)
    monkeypatch.setenv("PAPAIA_WORKSPACE_DIR", _WORKSPACE_DIR)
    get_settings.cache_clear()
    yield TestClient(create_app(), follow_redirects=False)
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _fresh_state() -> Iterator[None]:
    for child in _CONFIG.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    for child in _WORKSPACE.iterdir():
        shutil.rmtree(child) if child.is_dir() else child.unlink()
    addon_updates.invalidate_outdated_count()
    yield


def _as(client: TestClient, role: str) -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": "u-1",
            "preferred_username": "tester",
            "roles": [role],
            "exp": int(time.time()) + 3600,
        },
        "_csrf_token": _CSRF,
    }
    payload = base64.b64encode(json.dumps(session).encode())
    client.cookies.clear()
    client.cookies.set(
        "papaia_manager_session", TimestampSigner(_SESSION_SECRET).sign(payload).decode()
    )
    return client


# ── The world ───────────────────────────────────────────────────────────────


def _write_addon(directory: Path, version: str, *, env: str = "KEEP=1\n") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "papaia-app.yaml").write_text(
        f"name: paperless\nversion: {version}\n", encoding="utf-8"
    )
    (directory / ".env.example").write_text(env, encoding="utf-8")
    (directory / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")


def _world(
    *,
    installed: str = "1.0.0",
    offered: str = "1.1.0",
    supported: bool = True,
    active: bool = True,
    managed: bool = True,
    candidate_env: str = "KEEP=1\nNEW_MAIL=CHANGE_ME\n",
    with_catalog: bool = True,
) -> Path:
    """paperless `installed` registered at the old directory, `offered` in a local catalog."""
    script = _WORKSPACE / "papaia" / "tools" / "lib" / "sh" / "addon.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text("cmd_addon_update() {\n}\n" if supported else "cmd_addon_install() {\n}\n")

    _write_addon(_CATALOG_SOURCE / "paperless", offered, env=candidate_env)
    if with_catalog:
        save_registry(
            _CONFIG_DIR,
            CatalogRegistry(
                catalogs=[Catalog(name="cat", type="local", path=str(_CATALOG_SOURCE))]
            ),
        )

    current = managed_snapshot_path(_WORKSPACE_DIR, "cat", "paperless", "aaaaaaaaaaaa")
    _write_addon(current, installed)
    _register(current, active=active)
    record_installed(
        _CONFIG_DIR, name="paperless", catalog="cat", commit="a" * 40, manifest_version=installed,
        managed=managed,
    )
    return current


def _register(path: Path, *, active: bool = True, previous: dict[str, Any] | None = None) -> None:
    entry: dict[str, Any] = {"name": "paperless", "path": str(path), "active": active}
    if previous is not None:
        entry["previous"] = previous
    (_CONFIG / "deployment.yaml").write_text(yaml.safe_dump({"addons": [entry]}), encoding="utf-8")


def _deployed() -> dict[str, Any]:
    raw = yaml.safe_load((_CONFIG / "deployment.yaml").read_text(encoding="utf-8"))
    return raw["addons"][0]  # type: ignore[no-any-return]


def _audit() -> list[dict[str, Any]]:
    path = _CONFIG / "manager" / "audit.log"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _plan(
    *, blocked: bool = False, needs: list[str] | None = None, in_place: bool = False,
    new: list[dict[str, Any]] | None = None, reason: str = "needs addon_api 3",
) -> dict[str, Any]:
    keys = new if new is not None else [
        {"key": "NEW_MAIL", "class": "change_me", "value": None, "default": None, "label": "Mail"}
    ]
    return {
        "schema_version": 1,
        "name": "paperless",
        "current": {"version": "1.0.0", "path": "/old"},
        "candidate": {"version": "1.1.0", "path": "/new"},
        "direction": "upgrade",
        "in_place": in_place,
        "env": {"new": keys, "removed": [], "default_changed": []},
        "components": {"added": [], "removed": [], "changed": []},
        "manifest": {
            "version": {"from": "1.0.0", "to": "1.1.0"},
            "addon_api": {"from": 2, "to": 2},
            "attach": {"added": [], "removed": []},
            "replace_secrets_added": [],
            "keycloak": {"clients_added": [], "mappers_added": {}},
        },
        "needs_input": needs if needs is not None else ["NEW_MAIL"],
        "compat": {
            "status": "INCOMPATIBLE" if blocked else "OK", "axis": None, "requirement": None,
            "core_value": None, "reason": reason if blocked else None, "warnings": [],
        },
        "blocked": blocked,
    }


class _FakeCtl:
    """Plays papaia-ctl: answers the dry run, and moves the registration on a real run."""

    def __init__(self, plan: dict[str, Any]) -> None:
        self.plan = plan
        self.calls: list[tuple[str, list[str]]] = []
        self.answers_seen: dict[str, str] | None = None
        self.answers_path: Path | None = None
        self.repoint = True
        self.fail_after_repoint = False

    async def __call__(
        self, *, verb: str, name: str, extra_flags: list[str] | None = None, **_: Any
    ) -> AsyncIterator[str]:
        flags = list(extra_flags or [])
        self.calls.append((verb, flags))

        async def stream() -> AsyncIterator[str]:
            if verb == "update" and "--dry-run" in flags:
                for line in json.dumps(self.plan, indent=2).splitlines():
                    yield line
                if self.plan["blocked"]:
                    raise CtlError("refused", 2)
                return
            if verb == "update":
                target = next(f.split("=", 1)[1] for f in flags if f.startswith("--path="))
                for flag in flags:
                    if flag.startswith("--answers-file="):
                        self.answers_path = Path(flag.split("=", 1)[1])
                        self.answers_seen = dict(
                            line.split("=", 1)
                            for line in self.answers_path.read_text(encoding="utf-8").splitlines()
                        )
                if self.repoint:
                    old = _deployed()["path"]
                    _register(
                        Path(target),
                        previous={"path": old, "version": "1.0.0", "restore_point": None},
                    )
                yield f"update {name} output"
                if self.fail_after_repoint:
                    raise CtlError("containers could not be recreated", 3)
                return
            if verb == "rollback":
                previous = _deployed()["previous"]["path"]
                if self.repoint:
                    _register(Path(previous))
                yield f"rollback {name} output"
                return
            yield f"{verb} {name} output"

        return stream()


@pytest.fixture
def ctl(monkeypatch: pytest.MonkeyPatch) -> _FakeCtl:
    fake = _FakeCtl(_plan())
    monkeypatch.setattr(addon_updates, "run_addon_verb", fake)
    monkeypatch.setattr(api_addons, "run_addon_verb", fake)
    return fake


def _post(
    client: TestClient, path: str, body: dict[str, Any] | None = None, *,
    role: str = "admin", csrf: str | None = _CSRF,
) -> Any:
    headers = {"X-CSRF-Token": csrf} if csrf else {}
    return _as(client, role).post(f"/api/v1/addons/{path}", json=body or {}, headers=headers)


def _run(queue: _FakeQueue, log_path: Path, *, action: str) -> None:
    (entry,) = queue.enqueued
    ctx = JobContext(
        job=Job(
            id="job-1", action=action, target="paperless", user="tester",
            created_at=datetime.now(tz=UTC),
        ),
        log_path=log_path,
    )
    asyncio.run(entry["callback"](ctx))


# ── update-plan ─────────────────────────────────────────────────────────────


def test_the_plan_is_staged_and_returned_with_the_form_for_what_is_asked(
    client: TestClient, ctl: _FakeCtl
) -> None:
    _world()

    response = _post(client, "paperless/update-plan")

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["plan"]["candidate"]["version"] == "1.1.0"
    assert body["revision"].startswith("local-")
    assert [f["key"] for f in body["form"]] == ["NEW_MAIL"]
    staged = managed_snapshot_path(_WORKSPACE_DIR, "cat", "paperless", body["revision"])
    assert (staged / "papaia-app.yaml").is_file()
    (flags,) = [f for verb, f in ctl.calls if verb == "update"]
    assert flags[:2] == ["--dry-run", "--json"]
    assert flags[2] == f"--path={staged}"


def test_the_plan_carries_the_changelog_of_the_versions_it_brings(
    client: TestClient, ctl: _FakeCtl
) -> None:
    _world()
    (_CATALOG_SOURCE / "paperless" / "CHANGELOG.md").write_text(
        "## [1.1.0] - 2026-10-01\n### Upgrade notes\n- Run it twice.\n\n"
        "## [1.0.0] - 2026-09-01\n- First.\n",
        encoding="utf-8",
    )

    body = _post(client, "paperless/update-plan").json()

    assert body["changelog"] == [
        {"version": "1.1.0", "date": "2026-10-01", "body": "### Upgrade notes\n- Run it twice."}
    ]


def test_an_add_on_without_a_changelog_has_an_empty_one(client: TestClient, ctl: _FakeCtl) -> None:
    _world()

    assert _post(client, "paperless/update-plan").json()["changelog"] == []


def test_the_plan_of_an_old_core_is_refused(client: TestClient, ctl: _FakeCtl) -> None:
    _world(supported=False)

    response = _post(client, "paperless/update-plan")

    assert response.status_code == 409
    assert "1.8.0" in response.json()["detail"]
    assert ctl.calls == []


def test_an_add_on_that_is_not_installed_has_no_plan(client: TestClient, ctl: _FakeCtl) -> None:
    _world()
    (_CONFIG / "manager" / "installed.yaml").unlink()

    assert _post(client, "paperless/update-plan").status_code == 404


def test_an_add_on_the_panel_does_not_manage_has_no_plan(client: TestClient, ctl: _FakeCtl) -> None:
    _world(managed=False)

    assert _post(client, "paperless/update-plan").status_code == 409


def test_an_inactive_add_on_has_no_plan(client: TestClient, ctl: _FakeCtl) -> None:
    _world(active=False)

    response = _post(client, "paperless/update-plan")

    assert response.status_code == 409
    assert "not active" in response.json()["detail"]


def test_a_catalog_that_is_gone_has_no_plan(client: TestClient, ctl: _FakeCtl) -> None:
    _world(with_catalog=False)

    assert _post(client, "paperless/update-plan").status_code == 409


def test_the_plan_needs_the_csrf_token_and_an_administrator(
    client: TestClient, ctl: _FakeCtl
) -> None:
    _world()

    assert _post(client, "paperless/update-plan", csrf=None).status_code == 403
    assert _post(client, "paperless/update-plan", role="user").status_code == 403
    assert ctl.calls == []


def test_a_plan_pruning_keeps_the_registered_directory(client: TestClient, ctl: _FakeCtl) -> None:
    current = _world()
    stale = [
        managed_snapshot_path(_WORKSPACE_DIR, "cat", "paperless", f"{c}" * 12) for c in "bcde"
    ]
    for i, path in enumerate(stale):
        _write_addon(path, "0.0.1")
        os.utime(path, (1_700_000_000 + i, 1_700_000_000 + i))

    assert _post(client, "paperless/update-plan").status_code == 200

    assert current.is_dir()
    survivors = [p for p in stale if p.is_dir()]
    # The registered directory, the staged candidate and one more: three in all.
    assert len(survivors) == 1


# ── update ──────────────────────────────────────────────────────────────────


def test_an_update_the_gate_refuses_is_an_error_of_the_request(
    client: TestClient, queue: _FakeQueue, ctl: _FakeCtl
) -> None:
    _world()
    ctl.plan = _plan(blocked=True)

    response = _post(client, "paperless/update", {"env": {"NEW_MAIL": "ops@example.org"}})

    assert response.status_code == 409
    assert response.json()["detail"] == "needs addon_api 3"
    assert queue.enqueued == []


def test_a_value_the_update_needs_is_asked_for(
    client: TestClient, queue: _FakeQueue, ctl: _FakeCtl
) -> None:
    _world()

    response = _post(client, "paperless/update", {})

    assert response.status_code == 422
    assert response.json()["detail"]["field"] == "NEW_MAIL"
    assert queue.enqueued == []


def test_a_key_the_update_does_not_ask_about_is_refused(
    client: TestClient, queue: _FakeQueue, ctl: _FakeCtl
) -> None:
    _world()

    response = _post(
        client, "paperless/update", {"env": {"NEW_MAIL": "a@b.c", "KEEP": "overwritten"}}
    )

    assert response.status_code == 422
    assert response.json()["detail"]["field"] == "KEEP"
    assert queue.enqueued == []


def test_an_update_to_the_directory_the_add_on_runs_from_is_refused(
    client: TestClient, queue: _FakeQueue, ctl: _FakeCtl
) -> None:
    _world()
    ctl.plan = _plan(in_place=True, needs=[])

    assert _post(client, "paperless/update", {}).status_code == 409
    assert queue.enqueued == []


def test_an_update_runs_papaia_ctl_with_the_values_in_a_file_and_records_the_result(
    client: TestClient, queue: _FakeQueue, ctl: _FakeCtl, tmp_path: Path
) -> None:
    _world()
    revision = _post(client, "paperless/update-plan").json()["revision"]

    response = _post(
        client, "paperless/update",
        {"env": {"NEW_MAIL": "ops@example.org"}, "revision": revision},
    )
    assert response.status_code == 202, response.text
    _run(queue, tmp_path / "job.log", action="update")

    (flags,) = [f for verb, f in ctl.calls if verb == "update" and "--dry-run" not in f]
    staged = managed_snapshot_path(_WORKSPACE_DIR, "cat", "paperless", revision)
    assert flags[0] == f"--path={staged}"
    assert "-y" in flags
    assert "--no-backup" not in flags
    # The value reached papaia-ctl through the file and appears on no command line.
    assert ctl.answers_seen == {"NEW_MAIL": "ops@example.org"}
    assert all("ops@example.org" not in f for f in flags)
    assert ctl.answers_path is not None
    assert not ctl.answers_path.exists()

    recorded = load_installed(_CONFIG_DIR)["paperless"]
    assert recorded.manifest_version == "1.1.0"
    assert [h.manifest_version for h in recorded.history] == ["1.0.0"]
    (entry,) = [e for e in _audit() if e["action"] == "update"]
    assert entry["params"]["from"] == "1.0.0"
    assert entry["params"]["to"] == "1.1.0"
    assert entry["params"]["answered"] == ["NEW_MAIL"]
    assert "ops@example.org" not in json.dumps(entry)


def test_the_restore_point_can_be_turned_off(
    client: TestClient, queue: _FakeQueue, ctl: _FakeCtl, tmp_path: Path
) -> None:
    _world(candidate_env="KEEP=1\n")
    ctl.plan = _plan(needs=[], new=[])

    assert _post(client, "paperless/update", {"backup": False}).status_code == 202
    _run(queue, tmp_path / "job.log", action="update")

    (flags,) = [f for verb, f in ctl.calls if verb == "update" and "--dry-run" not in f]
    assert "--no-backup" in flags
    assert not any(f.startswith("--answers-file=") for f in flags)


def test_an_update_whose_containers_fail_still_records_the_new_version(
    client: TestClient, queue: _FakeQueue, ctl: _FakeCtl, tmp_path: Path
) -> None:
    _world(candidate_env="KEEP=1\n")
    ctl.plan = _plan(needs=[], new=[])
    ctl.fail_after_repoint = True

    assert _post(client, "paperless/update", {}).status_code == 202
    with pytest.raises(CtlError):
        _run(queue, tmp_path / "job.log", action="update")

    # papaia-ctl moved the registration before it failed to start the containers, so the
    # panel shows what is registered and offers the way back.
    assert load_installed(_CONFIG_DIR)["paperless"].manifest_version == "1.1.0"


def test_an_update_that_papaia_ctl_refused_leaves_installed_yaml_alone(
    client: TestClient, queue: _FakeQueue, ctl: _FakeCtl, tmp_path: Path
) -> None:
    _world(candidate_env="KEEP=1\n")
    ctl.plan = _plan(needs=[], new=[])
    ctl.repoint = False
    ctl.fail_after_repoint = True

    assert _post(client, "paperless/update", {}).status_code == 202
    with pytest.raises(CtlError):
        _run(queue, tmp_path / "job.log", action="update")

    assert load_installed(_CONFIG_DIR)["paperless"].manifest_version == "1.0.0"


def test_an_update_of_a_catalog_that_moved_since_the_review_is_refused(
    client: TestClient, queue: _FakeQueue, ctl: _FakeCtl
) -> None:
    _world()

    response = _post(
        client, "paperless/update",
        {"env": {"NEW_MAIL": "a@b.c"}, "revision": "local-20200101000000"},
    )

    assert response.status_code == 409
    assert "Review the update again" in response.json()["detail"]
    assert queue.enqueued == []


def test_update_is_denied_to_a_plain_user_and_needs_the_csrf_token(
    client: TestClient, queue: _FakeQueue, ctl: _FakeCtl
) -> None:
    _world()

    assert _post(client, "paperless/update", role="user").status_code == 403
    assert _post(client, "paperless/update", csrf=None).status_code == 403
    assert queue.enqueued == []


# ── rollback ────────────────────────────────────────────────────────────────


def _updated_world(*, restore_point: str | None = None) -> tuple[Path, Path]:
    """paperless registered at the new directory, with the old one as `previous`."""
    old = _world()
    new = managed_snapshot_path(_WORKSPACE_DIR, "cat", "paperless", "bbbbbbbbbbbb")
    _write_addon(new, "1.1.0")
    _register(
        new, previous={"path": str(old), "version": "1.0.0", "restore_point": restore_point}
    )
    record_installed(
        _CONFIG_DIR, name="paperless", catalog="cat", commit="b" * 40, manifest_version="1.1.0",
    )
    return old, new


def test_a_rollback_runs_papaia_ctl_and_records_the_earlier_version(
    client: TestClient, queue: _FakeQueue, ctl: _FakeCtl, tmp_path: Path
) -> None:
    old, _new = _updated_world()

    response = _post(client, "paperless/rollback", {})
    assert response.status_code == 202, response.text
    _run(queue, tmp_path / "job.log", action="rollback")

    assert ctl.calls == [("rollback", ["-y"])]
    assert _deployed()["path"] == str(old)
    assert load_installed(_CONFIG_DIR)["paperless"].manifest_version == "1.0.0"
    (entry,) = [e for e in _audit() if e["action"] == "rollback"]
    assert entry["params"] == {
        "from": "1.1.0", "to": "1.0.0", "restore_data": False, "restore_point": None,
    }


def test_a_rollback_without_an_earlier_version_is_refused(
    client: TestClient, queue: _FakeQueue, ctl: _FakeCtl
) -> None:
    _world()

    response = _post(client, "paperless/rollback", {})

    assert response.status_code == 409
    assert "no earlier version" in response.json()["detail"]
    assert queue.enqueued == []


def test_a_rollback_to_files_that_are_gone_is_refused(
    client: TestClient, queue: _FakeQueue, ctl: _FakeCtl
) -> None:
    old, _new = _updated_world()
    shutil.rmtree(old)

    response = _post(client, "paperless/rollback", {})

    assert response.status_code == 409
    assert "are gone" in response.json()["detail"]


def test_restoring_the_data_needs_a_restore_point_that_still_exists(
    client: TestClient, queue: _FakeQueue, ctl: _FakeCtl
) -> None:
    _updated_world(restore_point="2026-10-25_10-00-00")

    response = _post(client, "paperless/rollback", {"restore_data": True})

    assert response.status_code == 409
    assert "restore point" in response.json()["detail"]
    assert queue.enqueued == []


def test_a_rollback_with_the_data_passes_the_flag_on(
    client: TestClient, queue: _FakeQueue, ctl: _FakeCtl, tmp_path: Path
) -> None:
    _updated_world(restore_point="2026-10-25_10-00-00")
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "backup.yaml").write_text(
        yaml.safe_dump({"backups": [{"id": "2026-10-25_10-00-00", "result": "ok"}]}),
        encoding="utf-8",
    )
    (_CONFIG / ".env").write_text(f"PAPAIA_BACKUP_DIR={backups}\n", encoding="utf-8")

    assert _post(client, "paperless/rollback", {"restore_data": True}).status_code == 202
    _run(queue, tmp_path / "job.log", action="rollback")

    assert ctl.calls == [("rollback", ["-y", "--restore-data"])]
    (entry,) = [e for e in _audit() if e["action"] == "rollback"]
    assert entry["params"]["restore_point"] == "2026-10-25_10-00-00"


def test_rollback_is_refused_by_an_old_core(
    client: TestClient, queue: _FakeQueue, ctl: _FakeCtl
) -> None:
    _updated_world()
    (_WORKSPACE / "papaia" / "tools" / "lib" / "sh" / "addon.sh").write_text("# none\n")
    os.utime(
        _WORKSPACE / "papaia" / "tools" / "lib" / "sh" / "addon.sh", (1_800_000_000, 1_800_000_000)
    )

    assert _post(client, "paperless/rollback", {}).status_code == 409


def test_rollback_is_denied_to_a_plain_user_and_needs_the_csrf_token(
    client: TestClient, queue: _FakeQueue, ctl: _FakeCtl
) -> None:
    _updated_world()

    assert _post(client, "paperless/rollback", role="user").status_code == 403
    assert _post(client, "paperless/rollback", csrf=None).status_code == 403
    assert queue.enqueued == []


# ── the overview ────────────────────────────────────────────────────────────


def test_the_overview_lists_the_installed_add_ons_a_catalog_has_a_higher_version_of(
    client: TestClient,
) -> None:
    _world()

    body = _as(client, "admin").get("/api/v1/addons/updates").json()

    assert body == {
        "supported": True,
        "updates": [
            {"name": "paperless", "installed": "1.0.0", "available": "1.1.0", "catalog": "cat"}
        ],
    }


def test_the_overview_is_empty_when_the_catalog_is_behind(client: TestClient) -> None:
    _world(installed="1.2.0", offered="1.1.0")

    assert _as(client, "admin").get("/api/v1/addons/updates").json()["updates"] == []


def test_the_overview_is_for_administrators(client: TestClient) -> None:
    assert _as(client, "user").get("/api/v1/addons/updates").status_code == 403


def test_the_nav_dot_follows_the_catalogs(client: TestClient) -> None:
    _world()

    dot = _as(client, "admin").get("/partials/nav/addon-updates")
    assert dot.status_code == 200
    assert "bg-accent" in dot.text

    addon_updates.invalidate_outdated_count()
    _world(installed="1.1.0", offered="1.1.0")
    assert "bg-accent" not in _as(client, "admin").get("/partials/nav/addon-updates").text


def _cards_with_an_update(html: str) -> list[str]:
    """The cards that carry the attribute the "show only these" toggle looks for.

    The attribute name also appears in the toggle's own expression, so only a line that is
    nothing but the attribute counts."""
    return re.findall(r"^\s+data-has-update\s*$", html, flags=re.MULTILINE)


def test_the_gallery_lists_the_updates_and_can_narrow_the_cards_to_them(
    client: TestClient,
) -> None:
    _world()

    html = _as(client, "admin").get("/partials/addons").text

    assert 'id="addon-updates"' in html
    assert "/addons/paperless/update" in html
    assert "Show only these" in html
    assert len(_cards_with_an_update(html)) == 1
    assert "1.0.0 → 1.1.0" in html


def test_the_gallery_has_no_update_box_when_the_catalog_is_not_ahead(
    client: TestClient,
) -> None:
    _world(installed="1.1.0", offered="1.1.0")

    html = _as(client, "admin").get("/partials/addons").text

    assert 'id="addon-updates"' not in html
    assert "Show only these" not in html
    assert _cards_with_an_update(html) == []
