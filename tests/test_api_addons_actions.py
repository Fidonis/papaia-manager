"""`POST /api/v1/addons/{name}/actions/{action}` runs a maintenance action the add-on declares.

Only a declared action of an installed, active add-on is accepted, and the request carries the
action name only: papaia-ctl looks it up in the manifest again and runs it. The queue and
`run_addon_action` are replaced, so no Docker daemon or worker is needed.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import tempfile
import time
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

_SESSION_SECRET = "test-session-secret-value"
_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-addon-actions-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-addon-actions-workspace-")

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
from app.core import nodes  # noqa: E402
from app.core.actions import addon_actions, last_run  # noqa: E402
from app.core.ctl import CtlError  # noqa: E402
from app.core.jobs import Job, JobCallback, JobContext, JobQueue  # noqa: E402
from app.core.snapshots import record_installed  # noqa: E402
from app.main import create_app  # noqa: E402
from app.routers import api_addons  # noqa: E402
from app.templating import templates  # noqa: E402

_CSRF = "test-csrf-token"


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
        self.enqueued.append({"action": action, "target": target, "callback": callback})
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
def _fresh_state() -> None:
    Path(_CONFIG_DIR, "manager", "audit.log").unlink(missing_ok=True)
    Path(_CONFIG_DIR, "deployment.yaml").unlink(missing_ok=True)
    Path(_CONFIG_DIR, "installed.yaml").unlink(missing_ok=True)


_MANIFEST = {
    "name": "n8n",
    "version": "1.0.0",
    "actions": {
        "reset": {
            "service": "job",
            "args": ["--reset"],
            "description": "Reset",
            "destructive": True,
        },
        "check": {"service": "job", "description": "Self-check"},
    },
}


def _install(path: Path, *, active: bool = True) -> None:
    """Register `n8n` with the manifest above, installed and (by default) active."""
    path.mkdir(parents=True, exist_ok=True)
    (path / "papaia-app.yaml").write_text(yaml.safe_dump(_MANIFEST), encoding="utf-8")
    Path(_CONFIG_DIR, "deployment.yaml").write_text(
        yaml.safe_dump({"addons": [{"name": "n8n", "path": str(path), "active": active}]}),
        encoding="utf-8",
    )
    record_installed(_CONFIG_DIR, name="n8n", catalog="cat", commit="abc", manifest_version="1.0.0")


@pytest.fixture
def addon_dir(tmp_path: Path) -> Path:
    path = tmp_path / "n8n"
    _install(path)
    return path


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


def _run(
    client: TestClient,
    action: str,
    *,
    role: str = "admin",
    csrf: str | None = _CSRF,
    body: dict[str, Any] | None = None,
) -> Any:
    headers = {"X-CSRF-Token": csrf} if csrf else {}
    return _as(client, role).post(
        f"/api/v1/addons/n8n/actions/{action}", json=body or {}, headers=headers
    )


def _audit() -> list[dict[str, Any]]:
    path = Path(_CONFIG_DIR, "manager", "audit.log")
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _run_callback(queue: _FakeQueue, log_path: Path) -> None:
    (entry,) = queue.enqueued
    ctx = JobContext(
        job=Job(
            id="job-1",
            action="addon_action",
            target="n8n",
            user="tester",
            created_at=datetime.now(tz=UTC),
        ),
        log_path=log_path,
    )
    asyncio.run(entry["callback"](ctx))


def _record_actions(
    monkeypatch: pytest.MonkeyPatch, *, failing: bool = False
) -> list[tuple[str, str]]:
    calls: list[tuple[str, str]] = []

    async def run_addon_action(*, name: str, action: str, **_: Any) -> AsyncIterator[str]:
        calls.append((name, action))

        async def lines() -> AsyncIterator[str]:
            yield f"{action} output"
            if failing:
                raise CtlError(f"papaia-ctl addon run {name} {action} failed", 3)

        return lines()

    monkeypatch.setattr(api_addons, "run_addon_action", run_addon_action)
    return calls


@pytest.mark.usefixtures("addon_dir")
def test_a_declared_action_runs_by_name_and_is_audited(
    client: TestClient, queue: _FakeQueue, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = _record_actions(monkeypatch)

    response = _run(client, "reset", body={"confirmed": True})
    assert response.status_code == 202, response.text
    assert queue.enqueued[0]["action"] == "addon_action"
    _run_callback(queue, tmp_path / "job.log")

    assert calls == [("n8n", "reset")]
    log = (tmp_path / "job.log").read_text(encoding="utf-8")
    assert "papaia-ctl addon run n8n reset" in log
    assert "reset output" in log
    (entry,) = _audit()
    assert entry["action"] == "addon_action"
    assert entry["target"] == "n8n"
    assert entry["params"] == {"action": "reset"}


@pytest.mark.usefixtures("addon_dir")
def test_an_undeclared_action_is_refused_and_nothing_runs(
    client: TestClient, queue: _FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls = _record_actions(monkeypatch)

    for action in ("purge", "reset;rm", "..%2Fx"):
        response = _run(client, action)
        assert response.status_code in (404, 405), (action, response.text)

    assert queue.enqueued == []
    assert calls == []


def test_an_inactive_addon_offers_no_action(
    client: TestClient, queue: _FakeQueue, tmp_path: Path
) -> None:
    _install(tmp_path / "n8n", active=False)

    assert _run(client, "reset").status_code == 404
    assert queue.enqueued == []


@pytest.mark.usefixtures("addon_dir")
def test_a_failing_action_fails_the_job_and_writes_no_audit_entry(
    client: TestClient, queue: _FakeQueue, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _record_actions(monkeypatch, failing=True)

    assert _run(client, "check").status_code == 202
    with pytest.raises(CtlError) as raised:
        _run_callback(queue, tmp_path / "job.log")

    assert raised.value.exit_code == 3
    assert _audit() == []


@pytest.mark.usefixtures("addon_dir")
def test_a_failing_action_keeps_its_own_exit_code_with_the_job(
    client: TestClient, queue: _FakeQueue, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The queue sets every failed job's exit code to 1; the page shows the action's own.
    _record_actions(monkeypatch, failing=True)
    assert _run(client, "check").status_code == 202
    (entry,) = queue.enqueued
    job = Job(
        id="job-1",
        action="addon_action",
        target="n8n",
        user="tester",
        created_at=datetime.now(tz=UTC),
        params={"action": "check"},
    )
    with pytest.raises(CtlError):
        asyncio.run(entry["callback"](JobContext(job=job, log_path=tmp_path / "job.log")))

    assert job.params == {"action": "check", "exit_code": 3}


@pytest.mark.usefixtures("addon_dir")
@pytest.mark.parametrize("body", [{}, {"confirmed": False}])
def test_a_destructive_action_is_refused_unless_it_was_confirmed(
    client: TestClient,
    queue: _FakeQueue,
    monkeypatch: pytest.MonkeyPatch,
    body: dict[str, Any],
) -> None:
    calls = _record_actions(monkeypatch)

    response = _run(client, "reset", body=body)

    assert response.status_code == 422
    assert "confirmed" in response.json()["detail"]
    assert queue.enqueued == []
    assert calls == []


@pytest.mark.usefixtures("addon_dir")
def test_an_action_that_is_not_destructive_needs_no_confirmation(
    client: TestClient, queue: _FakeQueue
) -> None:
    assert _run(client, "check").status_code == 202
    assert [e["action"] for e in queue.enqueued] == ["addon_action"]


@pytest.mark.usefixtures("addon_dir")
def test_actions_need_the_admin_role_and_the_csrf_token(
    client: TestClient, queue: _FakeQueue
) -> None:
    assert _run(client, "check", role="user").status_code == 403
    assert _run(client, "check", csrf=None).status_code == 403
    assert queue.enqueued == []


@pytest.mark.usefixtures("addon_dir")
def test_actions_are_refused_while_the_stack_is_locked(
    client: TestClient, queue: _FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.core.nodes.lock_reason", lambda config_dir, workspace_dir: "an upgrade is running"
    )

    assert _run(client, "check").status_code == 409
    assert queue.enqueued == []


def _detail(actions: list[dict[str, Any]], last_action: dict[str, Any] | None = None) -> str:
    return templates.env.get_template("partials/addon_detail_content.html").render(
        addon={"name": "n8n", "status": "running", "actions": actions, "last_action": last_action},
        request=None,
    )


_ACTIONS = [
    {"name": "check", "description": "Self-check", "destructive": False},
    {"name": "purge", "description": "Remove the add-on's data", "destructive": True},
]


def test_the_page_lists_the_actions_and_marks_destructive_ones() -> None:
    html = _detail(_ACTIONS)
    assert "Maintenance actions" in html
    assert "btn-error" in html
    # The browser's confirm() is not the question: it can be switched off or auto-answered.
    assert "confirm(" not in html


def test_a_destructive_action_asks_in_a_dialog_that_names_it() -> None:
    html = _detail(_ACTIONS)

    dialog = html[html.index('<dialog x-ref="confirmAction2"'):]
    dialog = dialog[: dialog.index("</dialog>")]
    assert "Run purge on n8n" in dialog
    assert "Remove the add-on&#39;s data" in dialog
    assert "This action cannot be undone." in dialog
    # Only the dialog's own button runs it, and only it sends the confirmation.
    assert "addonMaintenanceAction('n8n', 'purge', true," in dialog
    assert html.count("addonMaintenanceAction('n8n', 'purge'") == 1
    assert "$refs.confirmAction2.showModal()" in html


def test_an_action_that_is_not_destructive_runs_on_click() -> None:
    html = _detail(_ACTIONS)

    assert "addonMaintenanceAction('n8n', 'check', false," in html
    assert 'x-ref="confirmAction1"' not in html
    assert templates.env.loader is not None
    source = templates.env.loader.get_source(templates.env, "base.html")[0]
    assert "function addonMaintenanceAction(name, action, confirmed, onRefused)" in source
    assert "confirmed: !!confirmed" in source
    assert "I18N.maintenanceActionFailed" in source


def test_a_refused_request_is_shown_on_the_page() -> None:
    html = _detail(_ACTIONS)
    assert 'x-text="refused"' in html
    assert "detail => { refused = detail }" in html


def _outcome(**overrides: Any) -> dict[str, Any]:
    return {
        "job_id": "job-7",
        "action": "purge",
        "status": "succeeded",
        "exit_code": 0,
        "finished_at": datetime.now(tz=UTC).isoformat(),
        "output": "[ctl] papaia-ctl addon run n8n purge\nremoved 12 rows",
        "truncated": False,
        **overrides,
    }


def test_the_outcome_of_a_successful_run_is_shown_with_its_exit_status_and_output() -> None:
    html = _detail(_ACTIONS, _outcome())

    assert 'id="addon-action-outcome"' in html
    assert "border-success" in html
    assert "The action \"purge\" finished with exit status 0." in html
    assert "removed 12 rows" in html
    assert 'href="/jobs/job-7"' in html
    assert "<details class=\"mt-2\">" in html


def test_the_outcome_of_a_failed_run_is_shown_open_with_its_exit_status() -> None:
    html = _detail(
        _ACTIONS,
        _outcome(
            status="failed",
            exit_code=3,
            output="boom\n--- ERROR: papaia-ctl exited with code 3 ---",
            truncated=True,
        ),
    )

    assert "border-error" in html
    assert "The action \"purge\" failed with exit status 3." in html
    assert "<details class=\"mt-2\" open>" in html
    assert "Output (the end of it)" in html
    assert "boom" in html


def test_a_running_action_disables_the_buttons_and_says_so() -> None:
    running = _outcome(status="running", exit_code=None, finished_at=None, output="")
    html = _detail(_ACTIONS, running)

    assert "The action \"purge\" is running." in html
    assert "busy: true" in html
    assert "<details" not in html


def test_the_output_is_escaped() -> None:
    html = _detail(_ACTIONS, _outcome(output="<script>alert(1)</script>"))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.usefixtures("addon_dir")
def test_last_run_reads_the_newest_run_of_the_addon_from_the_queue(
    client: TestClient, queue: _FakeQueue, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _record_actions(monkeypatch, failing=True)
    assert _run(client, "check").status_code == 202
    (entry,) = queue.enqueued

    async def scenario() -> dict[str, Any] | None:
        real = JobQueue(str(tmp_path))
        real.start()

        async def other(ctx: JobContext) -> None:
            ctx.log("unrelated")

        await real.enqueue(action="addon_action", target="other", user="u", callback=other)
        job = await real.enqueue(
            action="addon_action",
            target="n8n",
            user="u",
            params={"action": "check"},
            callback=entry["callback"],
        )
        done = asyncio.Event()
        real.add_finished_listener(lambda finished: done.set() if finished is job else None)
        await asyncio.wait_for(done.wait(), timeout=5)
        real.stop()
        return last_run(real, "n8n")

    run = asyncio.run(scenario())

    assert run is not None
    assert (run["action"], run["status"], run["exit_code"]) == ("check", "failed", 3)
    assert "check output" in run["output"]
    assert run["finished_at"] is not None
    assert last_run(None, "n8n") is None


@pytest.mark.usefixtures("addon_dir")
def test_a_distributed_installation_refuses_actions(
    client: TestClient, queue: _FakeQueue, monkeypatch: pytest.MonkeyPatch
) -> None:
    # `papaia-ctl addon run` exits 2 there (Fidonis/papaia#319); a button that always fails
    # would only hide that.
    monkeypatch.setattr(nodes, "is_distributed", lambda config_dir: True)

    response = _run(client, "check")
    assert response.status_code == 409
    assert "distributed" in response.json()["detail"]
    assert queue.enqueued == []


@pytest.mark.parametrize(
    "block",
    [
        {"ok": {"service": "job"}, "bad": {"service": "job", "args": "--reset"}},
        {"ok": {"service": "job"}, "bad": {"service": "job", "description": 3}},
        {"ok": {"service": "job"}, "bad": {"service": "job", "destructive": "yes"}},
        {"ok": {"service": "job"}, "bad": {"description": "no service"}},
        {"ok": {"service": "job"}, "Bad": {"service": "job"}},
    ],
)
def test_one_malformed_entry_offers_no_action_at_all(block: dict[str, Any]) -> None:
    # As in the core: the block is malformed, and papaia-ctl refuses the add-on.
    assert addon_actions({"actions": block}) == []


def test_a_long_name_the_core_accepts_is_offered() -> None:
    name = "a" + "-x" * 40
    assert [a["name"] for a in addon_actions({"actions": {name: {"service": "job"}}})] == [name]
