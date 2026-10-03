"""The Host page, its nav dot, and the Host row in the sidebar status.

What matters most here is the edge between the two audiences. The page names
paths and host names and is for administrators; the chip is in front of every
authenticated account and may only say how many checks are fine, never which
disk or which domain is not.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-host-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-host-workspace-")

for _key, _value in {
    "OIDC_ISSUER_KC_AUTH": "https://kc.test/auth",
    "OIDC_ISSUER_KC_TOKEN": "https://kc.test/token",
    "OIDC_ISSUER_KC_CERTS": "https://kc.test/certs",
    "MANAGER_ADMIN_ROLE": "admin",
    "MANAGER_USER_ROLE": "user",
    "MANAGER_HOST": "http://localhost:8120",
    "MANAGER_OIDC_CLIENT_SECRET": "client-secret",
    "MANAGER_SESSION_SECRET": "test-session-secret-value",
    "PAPAIA_CONFIG_DIR": _CONFIG_DIR,
    "PAPAIA_WORKSPACE_DIR": _WORKSPACE_DIR,
}.items():
    os.environ.setdefault(_key, _value)

from fastapi.testclient import TestClient  # noqa: E402
from itsdangerous import TimestampSigner  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.core import docker_usage, host_health  # noqa: E402
from app.core.ctl import CtlError  # noqa: E402
from app.core.services import (  # noqa: E402
    ServiceContainer,
    ServiceHealth,
    ServiceModule,
    StackSnapshot,
)
from app.main import create_app  # noqa: E402
from app.routers import ui  # noqa: E402

_GIB = 1024**3
_BACKUP_PATH = "/mnt/backup-secret-volume"
_DOMAIN = "ai.internal-example.com"
_LE = f"infra/nginx/nginx-letsencrypt/live/{_DOMAIN}/fullchain.pem"


_GPU_NAME = "NVIDIA GeForce RTX 4060"
_CPU_SENTENCE = "8 core(s), load 9.00 (5 min: 1.12 per core; warn at 1.00)"
_GPU_SKIP = "not measurable from inside a container: the driver tools of the host are not visible"


def _resources(*, cpu_status: str = "warn", gpu: str = "skip") -> list[dict[str, Any]]:
    """The four resource checks, as the core's `doctor` writes them."""
    gpu_check: dict[str, Any] = {"name": "gpu", "status": gpu}
    if gpu == "skip":
        gpu_check.update(
            summary=_GPU_SKIP, details={"variant": "nvidia-cuda-13", "override_present": True}
        )
    else:
        gpu_check.update(
            summary="",
            details={
                "variant": "nvidia-cuda-13",
                "gpus": [
                    {
                        "index": 0,
                        "name": _GPU_NAME,
                        "driver": "560.35.03",
                        "memory_total_mib": 8192,
                        "memory_used_mib": 1024,
                        "vram_percent": 12.5,
                        "utilization_percent": 7,
                        "temperature_c": 50,
                    }
                ],
            },
        )
    return [
        {
            "name": "memory",
            "status": "pass",
            "summary": "16.0 GiB total, 5.0 GiB available (69 % used)",
            "details": {
                "total_bytes": 16 * _GIB,
                "available_bytes": 5 * _GIB,
                "used_percent": 68.8,
                "swap_total_bytes": 4 * _GIB,
                "swap_used_bytes": 1 * _GIB,
            },
        },
        {
            "name": "cpu",
            "status": cpu_status,
            "summary": _CPU_SENTENCE,
            "details": {
                "cores": 8,
                "load1": 9.0,
                "load5": 9.0,
                "load15": 8.0,
                "load_per_core": 1.12,
            },
        },
        gpu_check,
        {
            "name": "time_sync",
            "status": "pass",
            "summary": "system clock is synchronized",
            "details": {"ntp_synchronized": True},
        },
    ]


def _doc(
    *,
    backup_status: str = "warn",
    cert_status: str = "warn",
    resources: list[dict[str, Any]] | None = None,
    docker_root: bool = False,
) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "generated_at": "2026-10-02T12:00:00Z",
            "platform_version": "1.4.0",
            "checks": [
                {
                    "name": "disk_space",
                    "status": backup_status,
                    "summary": "",
                    "details": {
                        "paths": [
                            {
                                "label": "config_dir",
                                "path": "/srv/papaia-config",
                                "free_bytes": 189 * _GIB,
                                "total_bytes": 320 * _GIB,
                                "status": "pass",
                            },
                            {
                                "label": "backup_dir",
                                "path": _BACKUP_PATH,
                                "free_bytes": 9 * _GIB,
                                "total_bytes": 100 * _GIB,
                                "status": backup_status,
                            },
                            # Only where the core can see the data root itself.
                            *(
                                [
                                    {
                                        "label": "docker_root",
                                        "path": "/var/lib/docker",
                                        "free_bytes": 80 * _GIB,
                                        "total_bytes": 200 * _GIB,
                                        "status": "pass",
                                    }
                                ]
                                if docker_root
                                else []
                            ),
                        ],
                        "notes": [],
                    },
                },
                {
                    "name": "certs",
                    "status": cert_status,
                    "summary": "",
                    "details": {
                        "certificates": [
                            {
                                "path": _LE,
                                "not_after": "2026-10-14T11:15:00Z",
                                "days_left": 12,
                                "status": cert_status,
                            },
                            {
                                "path": "certs/keycloak.crt",
                                "not_after": "2036-06-03T11:15:00Z",
                                "days_left": 3412,
                                "status": "pass",
                            },
                        ]
                    },
                },
                *(resources or []),
            ],
            "summary": {"pass": 0, "warn": 0, "fail": 0, "skip": 0},
            "ok": True,
        }
    )


class _Doctor:
    def __init__(self, stdout: str) -> None:
        self.stdout = stdout
        self.calls = 0

    async def __call__(self, **_: Any) -> tuple[int, str, str]:
        self.calls += 1
        return 0, self.stdout, ""


def _healthy_core() -> StackSnapshot:
    container = ServiceContainer(
        name="papaia-keycloak-1",
        service="keycloak",
        role="",
        state="running",
        status_text="Up 2 hours (healthy)",
        health=ServiceHealth.HEALTHY,
    )
    return StackSnapshot(core=[ServiceModule(name="keycloak", containers=[container])])


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    host_health.reset_cache()
    yield
    host_health.reset_cache()


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """A workspace whose core has a `doctor`."""
    lib = tmp_path / "workspace" / "papaia" / "tools" / "lib"
    lib.mkdir(parents=True)
    (lib / "doctor.py").write_text("", encoding="utf-8")
    return tmp_path / "workspace"


@pytest.fixture
def client(
    workspace: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[TestClient]:
    get_settings.cache_clear()
    settings = get_settings().model_copy(
        update={
            "papaia_config_dir": str(tmp_path / "config"),
            "papaia_workspace_dir": str(workspace),
        }
    )
    app = create_app()
    app.dependency_overrides[get_settings] = lambda: settings
    # The chip also reads the container snapshot; a healthy core makes the host
    # the only thing that can move its headline.
    monkeypatch.setattr(ui, "load_snapshot", lambda *_: _healthy_core())
    yield TestClient(app, follow_redirects=False)
    get_settings.cache_clear()


def _as(client: TestClient, *roles: str) -> TestClient:
    session: dict[str, Any] = {
        "user": {
            "sub": "u-1",
            "preferred_username": "tester",
            "roles": list(roles),
            "exp": int(time.time()) + 3600,
        }
    }
    payload = base64.b64encode(json.dumps(session).encode())
    signed = TimestampSigner(get_settings().manager_session_secret).sign(payload).decode()
    client.cookies.clear()
    client.cookies.set("papaia_manager_session", signed)
    return client


def _prime(monkeypatch: pytest.MonkeyPatch, workspace: Path, stdout: str) -> _Doctor:
    """Run the host check once so the cache-only views have something to read."""
    stub = _Doctor(stdout)
    monkeypatch.setattr(host_health, "run_py_cli", stub)
    asyncio.run(host_health.load_host_health(config_dir="/cfg", workspace_dir=str(workspace)))
    return stub


# ---------------------------------------------------------------------------
# The page
# ---------------------------------------------------------------------------


def test_the_page_shell_polls_its_partial_and_offers_a_recheck(client: TestClient) -> None:
    body = _as(client, "admin").get("/host").text

    assert 'hx-get="/partials/host"' in body
    assert "every 60s" in body
    assert "Re-check" in body
    assert "/partials/host?fresh=true" in body


def _write_interval(tmp_path: Path, seconds: int) -> None:
    manager = tmp_path / "config" / "manager"
    manager.mkdir(parents=True, exist_ok=True)
    (manager / "settings.yaml").write_text(
        f"host:\n  refresh_seconds: {seconds}\n", encoding="utf-8"
    )


def test_the_page_polls_at_the_interval_from_settings(client: TestClient, tmp_path: Path) -> None:
    _write_interval(tmp_path, 300)

    body = _as(client, "admin").get("/host").text

    assert "every 300s" in body
    assert "every 60s" not in body


def test_the_interval_is_shown_but_not_editable_on_the_page_and_links_to_settings(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _prime(monkeypatch, workspace, _doc())
    admin = _as(client, "admin")

    default = admin.get("/partials/host").text
    assert "refreshes every 60 s" in default
    assert 'href="/settings#host-monitoring"' in default
    assert "<input" not in default

    _write_interval(tmp_path, 300)
    assert "refreshes every 5 min" in admin.get("/partials/host").text

    _write_interval(tmp_path, 45)
    assert "refreshes every 45 s" in admin.get("/partials/host").text


def test_the_partial_lists_the_resources_with_the_cores_verdicts(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime(monkeypatch, workspace, _doc(resources=_resources(gpu="pass")))

    body = _as(client, "admin").get("/partials/host").text

    assert "Resources" in body
    assert "4 measured" in body
    assert "Memory" in body
    assert "5.0 GiB available of 16.0 GiB" in body
    assert "69 % used" in body
    assert "swap 1.0 GiB of 4.0 GiB used" in body
    assert "CPU" in body
    assert "8 cores · load 9.00 / 9.00 / 8.00" in body
    assert "1.12 / core" in body
    # The core warned about the CPU, and said why in its own words.
    assert _CPU_SENTENCE in body
    assert _GPU_NAME in body
    assert "1.0 GiB of 8.0 GiB VRAM" in body
    assert "driver 560.35.03 · util 7 % · 50 °C" in body
    assert "System clock" in body
    assert ">synchronized</p>" in body
    assert "progress-warning" in body


def test_a_resource_the_core_could_not_read_is_a_row_that_says_so(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime(monkeypatch, workspace, _doc(resources=_resources()))

    body = _as(client, "admin").get("/partials/host").text

    assert "3 measured, 1 not measurable" in body
    assert ">GPU</p>" in body
    assert _GPU_SKIP in body
    # The panel does not invent a bar for a reading there is none of.
    assert 'aria-label="GPU used"' not in body
    assert 'aria-label="Memory used"' in body


_USAGE_TYPES = {
    "images": {
        "count": 53,
        "active": 31,
        "size_bytes": 50_460_000_000,
        "reclaimable_bytes": 14_080_000_000,
    },
    "containers": {
        "count": 32,
        "active": 31,
        "size_bytes": 235_900_000,
        "reclaimable_bytes": 16_380,
    },
    "volumes": {
        "count": 75,
        "active": 22,
        "size_bytes": 26_080_000_000,
        "reclaimable_bytes": 23_460_000_000,
    },
    "build_cache": {
        "count": 897,
        "active": 0,
        "size_bytes": 25_530_000_000,
        "reclaimable_bytes": 20_730_000_000,
    },
}


def _usage_doc(status: str = "pass", summary: str = "Docker uses 102.3GB") -> str:
    details = {"types": _USAGE_TYPES} if status == "pass" else {}
    return json.dumps(
        {
            "schema_version": 1,
            "generated_at": "2026-10-03T12:00:00Z",
            "platform_version": "1.4.0",
            "checks": [
                {"name": "docker_usage", "status": status, "summary": summary, "details": details}
            ],
            "summary": {"pass": 1, "warn": 0, "fail": 0, "skip": 0},
            "ok": True,
        }
    )


class _UsageDoctor:
    """Stands in for the `doctor` that measures Docker's disk use, apart from the host's."""

    def __init__(self, stdout: str, error: Exception | None = None) -> None:
        self.stdout, self.error, self.calls = stdout, error, 0

    async def __call__(self, **_: Any) -> tuple[int, str, str]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return 0, self.stdout, ""


def _core_with_docker_usage(
    workspace: Path, monkeypatch: pytest.MonkeyPatch, stub: _UsageDoctor
) -> None:
    """A core that has the `docker_usage` check, whose own run is `stub`."""
    (workspace / "papaia" / "tools" / "lib" / "doctor.py").write_text(
        'CHECKS = [\n    ("disk_space", check_disk_space),\n'
        '    ("docker_usage", check_docker_usage),\n]\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(docker_usage, "run_py_cli", stub)


def test_the_partial_lists_what_docker_holds_when_the_core_can_say(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _UsageDoctor(_usage_doc())
    _core_with_docker_usage(workspace, monkeypatch, stub)
    _prime(monkeypatch, workspace, _doc())

    body = _as(client, "admin").get("/partials/host").text

    assert "Docker usage" in body
    assert "102.3 GB used, 58.27 GB reclaimable" in body
    for label in ("Images", "Containers", "Volumes", "Build cache"):
        assert f">{label}</p>" in body
    assert "50.46 GB" in body
    assert "14.08 GB reclaimable" in body
    assert "53 total · 31 active" in body
    assert "25.53 GB" in body
    assert "<code>docker system df</code>" in body
    # The free space of the data root is not measurable from here, and there is no
    # row that says so next to what Docker holds: it would read as a hole.
    assert "Docker data" not in body
    assert "aria-label=\"Volumes share of Docker's data\"" in body


def test_a_core_without_the_docker_usage_check_has_no_such_section_and_no_run(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _UsageDoctor(_usage_doc())
    monkeypatch.setattr(docker_usage, "run_py_cli", stub)
    _prime(monkeypatch, workspace, _doc())

    body = _as(client, "admin").get("/partials/host").text

    assert "Docker usage" not in body
    assert stub.calls == 0


def test_docker_usage_that_could_not_be_read_is_a_note_and_the_rest_of_the_page_stays(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _UsageDoctor(_usage_doc(status="skip", summary="docker system df failed: no daemon"))
    _core_with_docker_usage(workspace, monkeypatch, stub)
    _prime(monkeypatch, workspace, _doc())

    body = _as(client, "admin").get("/partials/host").text

    assert "Docker usage is not available: docker system df failed: no daemon" in body
    # What the host reading carries is all still there.
    assert "Config disk" in body
    assert "Backup disk" in body
    assert _DOMAIN in body


def test_a_crashing_docker_usage_run_cannot_take_the_host_page_down(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _UsageDoctor("", error=CtlError("doctor timed out after 30s", exit_code=124))
    _core_with_docker_usage(workspace, monkeypatch, stub)
    _prime(monkeypatch, workspace, _doc())

    response = _as(client, "admin").get("/partials/host")

    assert response.status_code == 200
    assert "Docker usage is not available: doctor timed out after 30s" in response.text
    assert "Config disk" in response.text


def test_docker_usage_never_reaches_the_chip_or_the_dot(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # It is a report with no verdict: the counts are the same with or without it.
    _prime(monkeypatch, workspace, _doc())
    without = _as(client, "user").get("/partials/service-status").text

    stub = _UsageDoctor(_usage_doc())
    _core_with_docker_usage(workspace, monkeypatch, stub)
    _as(client, "admin").get("/partials/host")
    with_usage = _as(client, "user").get("/partials/service-status").text

    assert "2 / 4 ok" in without
    assert "2 / 4 ok" in with_usage
    assert "Docker usage" not in with_usage
    assert "102.3" not in with_usage


def test_polling_the_page_does_not_measure_docker_again_and_recheck_waits_half_a_minute(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _UsageDoctor(_usage_doc())
    _core_with_docker_usage(workspace, monkeypatch, stub)
    _prime(monkeypatch, workspace, _doc())
    admin = _as(client, "admin")

    for _ in range(3):
        admin.get("/partials/host")
    admin.get("/partials/host?fresh=true")
    admin.get("/partials/host?fresh=true")

    assert stub.calls == 1


def test_a_core_without_the_resource_checks_just_has_no_resources_section(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime(monkeypatch, workspace, _doc())

    body = _as(client, "admin").get("/partials/host").text

    assert "Resources" not in body
    assert "Disk space" in body


def test_the_partial_lists_disks_and_certificates_with_the_cores_verdicts(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime(monkeypatch, workspace, _doc())

    body = _as(client, "admin").get("/partials/host").text

    assert "Config disk" in body
    assert "/srv/papaia-config" in body
    assert "189 GiB free of 320 GiB" in body
    assert "Backup disk" in body
    assert _BACKUP_PATH in body
    assert "9.0 GiB free of 100 GiB" in body
    assert "91 % used" in body
    assert "progress-warning" in body
    assert _DOMAIN in body
    assert "Let&#39;s Encrypt" in body
    assert "12 days" in body
    assert "expires 14 Oct 2026" in body
    assert "keycloak.crt" in body
    assert "3412 days" in body


def test_a_docker_root_the_panel_cannot_see_gets_no_row_and_no_hint(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A row that only said "not measurable" read as a hole where the reading
    # belonged. There is none, and the header does not point at the gap either.
    _prime(monkeypatch, workspace, _doc())

    body = _as(client, "admin").get("/partials/host").text

    assert "Docker data" not in body
    assert "only the host can see" not in body
    assert "not visible" not in body
    assert "Disk space" in body
    assert "2 measured" in body


def test_a_docker_root_the_core_can_measure_is_an_ordinary_disk_row(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime(monkeypatch, workspace, _doc(docker_root=True))

    body = _as(client, "admin").get("/partials/host").text

    assert "Docker data" in body
    assert "/var/lib/docker" in body
    assert "80.0 GiB free of 200 GiB" in body
    assert "3 measured" in body


def test_a_critical_check_is_drawn_as_critical(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime(monkeypatch, workspace, _doc(backup_status="fail"))

    body = _as(client, "admin").get("/partials/host").text

    assert "progress-error" in body
    assert "border-l-error" in body


def test_a_core_without_doctor_gets_an_explanation_instead_of_a_blank_page(
    client: TestClient, tmp_path: Path
) -> None:
    empty = tmp_path / "old-core"
    empty.mkdir()
    settings = get_settings().model_copy(update={"papaia_workspace_dir": str(empty)})
    client.app.dependency_overrides[get_settings] = lambda: settings  # type: ignore[attr-defined]

    body = _as(client, "admin").get("/partials/host").text

    assert "Host checks are not available" in body
    assert "1.4.0" in body


def test_re_checking_twice_in_a_row_runs_doctor_once(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    stub = _prime(monkeypatch, workspace, _doc())
    admin = _as(client, "admin")

    admin.get("/partials/host?fresh=true")
    admin.get("/partials/host?fresh=true")

    assert stub.calls == 1


# ---------------------------------------------------------------------------
# The chip
# ---------------------------------------------------------------------------


def test_the_chip_counts_the_host_in_for_every_role_and_shows_only_numbers(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime(monkeypatch, workspace, _doc())

    for roles in (("user",), ("admin",)):
        body = _as(client, *roles).get("/partials/service-status").text

        assert "Host" in body
        # Two disks and two certificates; the backup disk and the Let's Encrypt
        # certificate are the two that warn.
        assert "2 / 4 ok" in body
        assert "2 issues" in body
        # What the page shows to administrators must not reach the chip.
        for secret in (_BACKUP_PATH, _DOMAIN, "/srv/papaia-config", "Backup disk", "keycloak.crt"):
            assert secret not in body


def test_the_chip_counts_the_resources_in_and_leaves_what_it_could_not_read_out(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Two disks, two certificates, memory, CPU and the clock were judged; the GPU
    # was skipped and has no verdict. Backup disk, certificate and CPU warn.
    _prime(monkeypatch, workspace, _doc(resources=_resources(gpu="skip")))

    for roles in (("user",), ("admin",)):
        body = _as(client, *roles).get("/partials/service-status").text

        assert "4 / 7 ok" in body
        assert "3 issues" in body
        # Neither a device name, a figure nor the core's sentence is for non-admins.
        for secret in (_CPU_SENTENCE, _GPU_SKIP, "Memory", "swap", "System clock"):
            assert secret not in body


def test_a_warning_from_a_resource_alone_moves_the_dot_and_the_chip(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime(
        monkeypatch,
        workspace,
        _doc(backup_status="pass", cert_status="pass", resources=_resources(cpu_status="warn")),
    )

    assert "bg-warning" in _as(client, "admin").get("/partials/nav/host-indicator").text
    assert "1 issue" in _as(client, "user").get("/partials/service-status").text


def test_a_skipped_resource_alone_does_not_move_anything(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime(
        monkeypatch,
        workspace,
        _doc(backup_status="pass", cert_status="pass", resources=_resources(cpu_status="pass")),
    )

    assert "rounded-full" not in _as(client, "admin").get("/partials/nav/host-indicator").text
    assert "All healthy" in _as(client, "user").get("/partials/service-status").text


def test_only_an_administrator_is_pointed_at_the_host_page(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime(monkeypatch, workspace, _doc())

    assert 'href="/host"' not in _as(client, "user").get("/partials/service-status").text
    assert 'href="/host"' in _as(client, "admin").get("/partials/service-status").text


def test_a_healthy_host_leaves_the_chip_green(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime(monkeypatch, workspace, _doc(backup_status="pass", cert_status="pass"))

    body = _as(client, "user").get("/partials/service-status").text

    assert "All healthy" in body
    assert "4 / 4 ok" in body
    # A row of the sidebar, not a pill: in the rail the label goes and the dot stays.
    assert '<span class="sidebar-label">All healthy</span>' in body


def test_a_critical_host_turns_the_chip_red_without_touching_the_stack_rows(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime(monkeypatch, workspace, _doc(backup_status="fail", cert_status="pass"))

    body = _as(client, "user").get("/partials/service-status").text

    assert "text-error" in body
    assert "1 issue" in body
    # The core stack row still reads fully running: the host has its own row.
    assert "1 / 1 running" in body


def test_without_a_reading_the_chip_has_no_host_row_and_is_not_dragged_down(
    client: TestClient, tmp_path: Path
) -> None:
    empty = tmp_path / "old-core"
    empty.mkdir()
    settings = get_settings().model_copy(update={"papaia_workspace_dir": str(empty)})
    client.app.dependency_overrides[get_settings] = lambda: settings  # type: ignore[attr-defined]

    body = _as(client, "user").get("/partials/service-status").text

    assert "All healthy" in body
    assert "ok</span>" not in body


# ---------------------------------------------------------------------------
# The sidebar
# ---------------------------------------------------------------------------


def _between(body: str, start: str, end: str) -> str:
    return body[body.index(start) : body.index(end)]


def _nav_groups(body: str) -> dict[str, list[str]]:
    """Caption -> the entries under it, in order; the caption-less lead is ''."""
    nav = _between(body, "<nav", "</nav>")
    parts = re.split(r'<p class="sidebar-label[^>]*>([^<]+)</p>', nav)
    groups = {"": re.findall(r'aria-label="([^"]+)"', parts[0])}
    for caption, chunk in zip(parts[1::2], parts[2::2], strict=True):
        groups[caption] = re.findall(r'aria-label="([^"]+)"', chunk)
    return groups


def test_the_admin_nav_is_grouped_by_what_an_entry_acts_on(client: TestClient) -> None:
    groups = _nav_groups(_as(client, "admin").get("/host").text)

    assert list(groups) == ["", "Monitor", "Extensions", "System"]
    assert groups[""] == ["Dashboard"]
    assert groups["Monitor"] == ["Services", "Host", "Jobs"]
    assert groups["Extensions"] == ["Add-Ons", "Catalogs"]
    # Backup and upgrade are stack-level commands, so they sit with the system.
    assert groups["System"] == ["Backup / Restore", "Upgrade", "Audit log", "Settings"]


def test_a_user_without_the_admin_role_sees_the_dashboard_and_no_groups(
    client: TestClient,
) -> None:
    assert _nav_groups(_as(client, "user").get("/").text) == {"": ["Dashboard"]}


def test_the_status_row_is_in_the_sidebar_for_every_role_and_not_in_the_header(
    client: TestClient,
) -> None:
    for roles in (("user",), ("admin",)):
        body = _as(client, *roles).get("/").text
        header = _between(body, "<header", "</header>")
        sidebar = _between(body, "<aside", "</aside>")

        assert "/partials/service-status" not in header
        # Between the nav and the footer, so the footer's divider sits right
        # under it and the nav can scroll without taking it along.
        assert (
            sidebar.index("</nav>")
            < sidebar.index('hx-get="/partials/service-status"')
            < sidebar.index('aria-label="Toggle sidebar"')
        )


# ---------------------------------------------------------------------------
# The nav dot
# ---------------------------------------------------------------------------


def test_the_nav_dot_is_empty_until_there_is_something_to_look_at(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    admin = _as(client, "admin")
    assert "rounded-full" not in admin.get("/partials/nav/host-indicator").text

    _prime(monkeypatch, workspace, _doc(backup_status="pass", cert_status="pass"))
    assert "rounded-full" not in admin.get("/partials/nav/host-indicator").text


def test_the_nav_dot_follows_the_worst_state(
    client: TestClient, workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _prime(monkeypatch, workspace, _doc())
    assert "bg-warning" in _as(client, "admin").get("/partials/nav/host-indicator").text

    host_health.reset_cache()
    _prime(monkeypatch, workspace, _doc(backup_status="fail"))
    assert "bg-error" in _as(client, "admin").get("/partials/nav/host-indicator").text


def test_the_sidebar_links_to_the_host_page_for_administrators_only(client: TestClient) -> None:
    assert 'href="/host"' in _as(client, "admin").get("/host").text
    assert 'href="/host"' not in _as(client, "user").get("/").text
