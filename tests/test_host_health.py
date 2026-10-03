"""Host health: reading `doctor --json`, and running it no more often than needed.

The parser is tested against documents shaped like the core's real output, and
the runner against a stand-in for `run_py_cli`, so none of this needs a Docker
daemon, an `nvidia-smi`, an `openssl` or a core checkout. The clock is replaced
where a test is about time, because the cache's whole job is deciding when a
reading is stale, and the interval it decides by is a setting.
"""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.core import host_health
from app.core.ctl import CtlError
from app.core.host_health import HostState, parse_doctor
from app.core.services import ServiceHealth
from app.core.settings_store import DEFAULT_REFRESH_SECONDS

_GIB = 1024**3


@pytest.fixture(autouse=True)
def _fresh_cache() -> Iterator[None]:
    host_health.reset_cache()
    yield
    host_health.reset_cache()


def _disk(label: str, path: str, free_gib: float, total_gib: float, status: str) -> dict[str, Any]:
    return {
        "label": label,
        "path": path,
        "free_bytes": int(free_gib * _GIB),
        "total_bytes": int(total_gib * _GIB),
        "status": status,
    }


def _cert(path: str, days: int, status: str, not_after: str = "2026-10-14T11:15:00Z") -> dict:
    return {"path": path, "not_after": not_after, "days_left": days, "status": status}


def _doctor(
    *,
    paths: list[dict[str, Any]] | None = None,
    certificates: list[dict[str, Any]] | None = None,
    extra: list[dict[str, Any]] | None = None,
    disk_status: str = "pass",
    cert_status: str = "pass",
    schema_version: int = 1,
) -> str:
    """A `doctor --json` document with the two checks the manager reads."""
    checks: list[dict[str, Any]] = [
        {
            "name": "disk_space",
            "status": disk_status,
            "summary": "",
            "details": {"paths": paths or [], "notes": []},
        },
        {
            "name": "certs",
            "status": cert_status,
            "summary": "",
            "details": {"certificates": certificates or []},
        },
        *(extra or []),
    ]
    return json.dumps(
        {
            "schema_version": schema_version,
            "generated_at": "2026-10-02T12:00:00Z",
            "platform_version": "1.4.0",
            "checks": checks,
            "summary": {"pass": 2, "warn": 0, "fail": 0, "skip": 0},
            "ok": True,
        }
    )


HEALTHY = _doctor(
    paths=[
        _disk("config_dir", "/srv/papaia-config", 189, 320, "pass"),
        _disk("backup_dir", "/mnt/backup", 80, 100, "pass"),
    ],
    certificates=[_cert("certs/keycloak.crt", 3412, "pass")],
)


def _check(name: str, status: str, summary: str, details: dict[str, Any]) -> dict[str, Any]:
    return {"name": name, "status": status, "summary": summary, "details": details}


def _memory(status: str = "pass", summary: str = "16.0 GiB total, 5.0 GiB available") -> dict:
    return _check(
        "memory",
        status,
        summary,
        {
            "source": "proc",
            "total_bytes": 16 * _GIB,
            "available_bytes": 5 * _GIB,
            "used_percent": 68.8,
            "swap_total_bytes": 4 * _GIB,
            "swap_used_bytes": 1 * _GIB,
        },
    )


def _cpu(status: str = "pass", summary: str = "8 core(s), load 1.50 2.25 3.00") -> dict:
    return _check(
        "cpu",
        status,
        summary,
        {
            "source": "os",
            "cores": 8,
            "load1": 1.5,
            "load5": 2.25,
            "load15": 3.0,
            "load_per_core": 0.28,
        },
    )


def _gpu_device(index: int = 0, name: str | None = "NVIDIA GeForce RTX 4060") -> dict[str, Any]:
    return {
        "index": index,
        "name": name,
        "driver": "560.35.03",
        "memory_total_mib": 24576,
        "memory_used_mib": 3072,
        "vram_percent": 12.3,
        "utilization_percent": 12,
        "temperature_c": 54,
    }


def _gpu(
    status: str = "pass", summary: str = "", devices: list[dict[str, Any]] | None = None
) -> dict:
    return _check(
        "gpu",
        status,
        summary,
        {
            "variant": "nvidia-cuda-13",
            "override_present": True,
            "nvidia_runtime": True,
            "gpus": [_gpu_device()] if devices is None else devices,
        },
    )


def _clock(status: str = "pass", synchronized: bool | None = True) -> dict:
    details = {} if synchronized is None else {"ntp_synchronized": synchronized}
    return _check("time_sync", status, "system clock is synchronized", details)


def _with_resources(*checks: dict[str, Any]) -> str:
    return _doctor(
        paths=[_disk("config_dir", "/c", 189, 320, "pass")],
        certificates=[_cert("certs/keycloak.crt", 3412, "pass")],
        extra=list(checks),
    )


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_a_healthy_document_reads_as_all_ok() -> None:
    health = parse_doctor(HEALTHY)

    assert health.available
    assert [c.state for c in health.checks] == [HostState.OK] * 3
    assert (health.total, health.ok_count, health.issue_count) == (3, 3, 0)
    assert health.overall is HostState.OK
    assert health.severity is ServiceHealth.HEALTHY


def test_the_cores_verdict_is_taken_as_is_and_the_percentage_decides_nothing() -> None:
    # 91 % used would be a warning under a percentage rule. The core judges by
    # free bytes, and said "pass" for 90 GiB free: so it is OK here, whatever the
    # bar shows. The reverse -- a mostly empty disk the core calls critical --
    # keeps its colour too.
    health = parse_doctor(
        _doctor(
            paths=[
                _disk("config_dir", "/c", 90, 1000, "pass"),
                _disk("backup_dir", "/b", 1, 1000, "fail"),
            ]
        )
    )

    config, backup = health.disks
    assert config.used_percent == 91.0
    assert config.state is HostState.OK
    assert backup.state is HostState.CRITICAL


def test_a_warning_and_a_failure_set_the_overall_state_and_the_severity() -> None:
    warn = parse_doctor(_doctor(paths=[_disk("backup_dir", "/b", 9, 100, "warn")]))
    assert warn.overall is HostState.WARN
    assert warn.severity is ServiceHealth.UNHEALTHY
    assert warn.issue_count == 1

    both = parse_doctor(
        _doctor(
            paths=[_disk("backup_dir", "/b", 9, 100, "warn")],
            certificates=[_cert("certs/x.crt", 3, "fail")],
        )
    )
    assert both.overall is HostState.CRITICAL
    assert both.severity is ServiceHealth.STOPPED
    assert (both.warn_count, both.critical_count, both.issue_count) == (1, 1, 2)


def test_disk_rows_get_readable_labels_and_the_path_as_detail() -> None:
    health = parse_doctor(
        _doctor(
            paths=[
                _disk("config_dir", "/srv/c", 10, 20, "pass"),
                _disk("backup_dir", "/srv/b", 10, 20, "pass"),
                _disk("docker_root", "/var/lib/docker", 10, 20, "pass"),
            ]
        )
    )

    assert [(d.label, d.detail) for d in health.disks] == [
        ("Config disk", "/srv/c"),
        ("Backup disk", "/srv/b"),
        ("Docker data", "/var/lib/docker"),
    ]
    assert health.docker_root_measured


def test_a_docker_root_the_container_cannot_see_is_not_measured_rather_than_empty() -> None:
    health = parse_doctor(_doctor(paths=[_disk("config_dir", "/c", 10, 20, "pass")]))

    assert not health.docker_root_measured
    assert [d.key for d in health.disks] == ["config_dir"]


def test_certificates_are_named_by_domain_or_file_and_sorted_worst_first() -> None:
    live = "infra/nginx/nginx-letsencrypt/live"
    health = parse_doctor(
        _doctor(
            certificates=[
                _cert("certs/keycloak.crt", 3412, "pass"),
                _cert(f"{live}/ai.example.com/fullchain.pem", 12, "warn"),
                _cert("certs/local-ca.crt", 3410, "pass"),
                _cert(f"{live}/old.example.com/fullchain.pem", -2, "fail"),
            ]
        )
    )

    assert [(c.label, c.detail, c.state) for c in health.certs] == [
        ("old.example.com", "Let's Encrypt", HostState.CRITICAL),
        ("ai.example.com", "Let's Encrypt", HostState.WARN),
        ("local-ca.crt", "Bundled", HostState.OK),
        ("keycloak.crt", "Bundled", HostState.OK),
    ]


def test_a_certificate_date_is_shown_the_way_a_person_writes_it() -> None:
    health = parse_doctor(_doctor(certificates=[_cert("certs/a.crt", 12, "warn")]))

    assert health.certs[0].expires_on == "14 Oct 2026"


def test_an_unreadable_certificate_has_no_verdict_and_does_not_count() -> None:
    unreadable = {"path": "certs/broken.crt", "status": "skip", "reason": "openssl not found"}
    health = parse_doctor(_doctor(certificates=[unreadable, _cert("certs/ok.crt", 400, "pass")]))

    broken = next(c for c in health.certs if c.key == "certs/broken.crt")
    assert broken.state is HostState.UNKNOWN
    assert broken.reason == "openssl not found"
    assert health.total == 1
    assert health.overall is HostState.OK


def test_nothing_judged_means_no_severity_so_the_chip_can_leave_the_host_out() -> None:
    unreadable = {"path": "certs/broken.crt", "status": "skip", "reason": "openssl not found"}
    health = parse_doctor(_doctor(certificates=[unreadable]))

    assert health.available
    assert health.overall is HostState.UNKNOWN
    assert health.severity is None
    assert health.total == 0


def test_a_check_that_measured_nothing_leaves_the_cores_reason_as_a_note() -> None:
    document = json.loads(_doctor(paths=[_disk("config_dir", "/c", 10, 20, "pass")]))
    document["checks"][1].update(
        status="skip", summary="no certificates found in the configuration directory"
    )

    health = parse_doctor(json.dumps(document))

    assert health.notes == ("no certificates found in the configuration directory",)
    assert health.certs == ()


def test_a_crashed_check_is_a_note_and_not_a_verdict() -> None:
    document = json.loads(HEALTHY)
    document["checks"][0] = {
        "name": "disk_space",
        "status": "fail",
        "summary": "check crashed: OSError: boom",
        "details": {},
    }

    health = parse_doctor(json.dumps(document))

    assert health.available
    assert health.disks == ()
    assert health.notes == ("check crashed: OSError: boom",)


def test_checks_that_were_not_asked_for_are_ignored() -> None:
    # A check the core adds later must not show up unreviewed. `ports` is one the
    # core has today and this panel skips by name; a check of any other name is
    # no different.
    health = parse_doctor(
        _doctor(
            paths=[_disk("config_dir", "/c", 10, 20, "pass")],
            extra=[
                {"name": name, "status": "fail", "summary": "x", "details": {"paths": [
                    _disk("other0", "/dev/x", 0, 1, "fail")
                ]}}
                for name in ("ports", "a_check_from_the_future")
            ],
        )
    )

    assert [d.key for d in health.disks] == ["config_dir"]
    assert health.resources == ()
    assert health.overall is HostState.OK


@pytest.mark.parametrize(
    ("stdout", "reason"),
    [
        ("", "did not return JSON"),
        ("Traceback (most recent call last):", "did not return JSON"),
        ("[1, 2]", "not return a JSON object"),
        (_doctor(schema_version=2), "schema 2"),
        (json.dumps({"schema_version": 1, "checks": []}), "none of the host checks"),
        (
            json.dumps({"schema_version": 1, "checks": [{"name": "ports", "status": "pass"}]}),
            "none of the host checks",
        ),
    ],
)
def test_an_unusable_document_is_unavailable_with_a_reason(stdout: str, reason: str) -> None:
    health = parse_doctor(stdout)

    assert not health.available
    assert reason in health.reason
    assert health.severity is None


def test_malformed_rows_are_dropped_not_fatal() -> None:
    document = json.loads(HEALTHY)
    document["checks"][0]["details"]["paths"] += [
        "nope",
        {"label": "", "path": "/x"},
        {"label": "backup_dir", "path": "/b", "free_bytes": True, "total_bytes": "9", "status": 3},
    ]

    health = parse_doctor(json.dumps(document))

    backup = next(d for d in health.disks if d.key == "backup_dir" and d.detail == "/b")
    # A JSON `true` is not a byte count, and a status that is not a string is not
    # a verdict.
    assert backup.free_bytes is None
    assert backup.total_bytes is None
    assert backup.used_percent is None
    assert backup.state is HostState.UNKNOWN


@pytest.mark.parametrize(
    ("value", "text"),
    [
        (512, "512 B"),
        (512 * 1024**2, "512 MiB"),
        (int(9.0 * _GIB), "9.0 GiB"),
        (189 * _GIB, "189 GiB"),
        (int(1.5 * 1024**4), "1.5 TiB"),
    ],
)
def test_sizes_are_binary_units_with_three_significant_digits(value: int, text: str) -> None:
    assert host_health.format_bytes(value) == text


# ---------------------------------------------------------------------------
# Resources: memory, CPU, GPU, clock
# ---------------------------------------------------------------------------


def test_the_four_resource_checks_become_rows_in_the_cores_order() -> None:
    health = parse_doctor(_with_resources(_clock(), _gpu(), _cpu(), _memory()))

    # The order is the panel's, not the document's.
    assert [r.label for r in health.resources] == [
        "Memory",
        "CPU",
        "NVIDIA GeForce RTX 4060",
        "System clock",
    ]
    assert all(r.state is HostState.OK for r in health.resources)
    assert [r.kind for r in health.resources] == ["memory", "cpu", "gpu", "clock"]


def test_memory_shows_what_is_available_of_the_total_and_the_swap_in_use() -> None:
    (memory,) = parse_doctor(_with_resources(_memory())).resources

    assert memory.detail == "swap 1.0 GiB of 4.0 GiB used"
    assert memory.measure == "5.0 GiB available of 16.0 GiB"
    assert memory.figure == "69 % used"
    assert memory.used_percent == 68.8


def test_a_host_without_swap_says_so() -> None:
    document = _memory()
    document["details"].update(swap_total_bytes=0, swap_used_bytes=0)

    (memory,) = parse_doctor(_with_resources(document)).resources

    assert memory.detail == "no swap"


def test_memory_without_proc_has_only_a_total_and_no_bar() -> None:
    # The core's fallback when there is no /proc/meminfo: `docker info` knows the
    # total and nothing else, and says null for the rest.
    document = _check(
        "memory",
        "pass",
        "15.4 GiB total",
        {
            "source": "docker_info",
            "total_bytes": int(15.4 * _GIB),
            "available_bytes": None,
            "used_percent": None,
            "swap_total_bytes": None,
            "swap_used_bytes": None,
        },
    )

    (memory,) = parse_doctor(_with_resources(document)).resources

    assert memory.measure == "15.4 GiB total"
    assert memory.detail == ""
    assert memory.figure == ""
    assert memory.used_percent is None


def test_cpu_is_drawn_as_load_per_core_and_the_bar_stops_at_full() -> None:
    (cpu,) = parse_doctor(_with_resources(_cpu())).resources

    assert cpu.detail == "8 cores · load 1.50 / 2.25 / 3.00"
    assert cpu.measure == "load per core, 5 min average"
    assert cpu.figure == "0.28 / core"
    assert cpu.used_percent == 28.0

    saturated = _cpu("warn", "8 core(s), load 24.00 (5 min: 3.00 per core)")
    saturated["details"]["load_per_core"] = 3.0
    (busy,) = parse_doctor(_with_resources(saturated)).resources
    assert busy.used_percent == 100.0
    assert busy.figure == "3.00 / core"


def test_a_gpu_row_has_the_vram_bar_and_the_devices_own_figures() -> None:
    (gpu,) = parse_doctor(_with_resources(_gpu())).resources

    assert gpu.label == "NVIDIA GeForce RTX 4060"
    assert gpu.detail == "driver 560.35.03 · util 12 % · 54 °C"
    assert gpu.measure == "3.0 GiB of 24.0 GiB VRAM"
    assert gpu.figure == "12 % used"
    assert gpu.used_percent == 12.3


def test_every_gpu_gets_a_row_and_a_missing_name_falls_back_to_the_index() -> None:
    health = parse_doctor(
        _with_resources(_gpu(devices=[_gpu_device(0), _gpu_device(1, name=None)]))
    )

    assert [(r.key, r.label) for r in health.resources] == [
        ("gpu0", "NVIDIA GeForce RTX 4060"),
        ("gpu1", "GPU 1"),
    ]


def test_a_gpu_with_nothing_but_a_sentence_shows_the_sentence() -> None:
    # Intel and Vulkan have no per-device figures, and AMD without `rocm-smi` none
    # either: the core's sentence is the reading.
    document = _check(
        "gpu",
        "pass",
        "Intel GPU render node present (utilization is not measured)",
        {"variant": "intel", "override_present": True, "render_node": True},
    )

    (gpu,) = parse_doctor(_with_resources(document)).resources

    assert gpu.label == "GPU"
    assert gpu.detail == "variant intel"
    assert gpu.measure.startswith("Intel GPU render node present")
    assert gpu.used_percent is None


def test_the_clock_reads_synchronized_or_not() -> None:
    (synced,) = parse_doctor(_with_resources(_clock())).resources
    (drifting,) = parse_doctor(_with_resources(_clock("warn", False))).resources

    assert (synced.measure, synced.state) == ("synchronized", HostState.OK)
    assert (drifting.measure, drifting.state) == ("not synchronized", HostState.WARN)
    # The boolean says it all; the core's sentence would only repeat it.
    assert drifting.reason == ""


def test_a_resource_verdict_is_the_cores_whatever_the_bar_shows() -> None:
    # 99 % used would be a warning under a rule of this page's own. The core
    # judges memory by what is *available*, and said pass.
    full = _memory()
    full["details"].update(used_percent=99.0)

    (memory,) = parse_doctor(_with_resources(full)).resources

    assert memory.used_percent == 99.0
    assert memory.state is HostState.OK
    assert memory.reason == ""


def test_a_warning_quotes_the_cores_sentence_because_only_it_names_the_limit() -> None:
    sentence = "1.0 GiB available (6 % of RAM; warn below 10 %)"

    (memory,) = parse_doctor(_with_resources(_memory("warn", sentence))).resources

    assert memory.state is HostState.WARN
    assert memory.reason == sentence


def test_the_sentence_goes_on_the_first_gpu_row_only_but_every_row_has_the_colour() -> None:
    sentence = "GPU 1 VRAM 94 % used (warn at 90 %)"
    health = parse_doctor(
        _with_resources(_gpu("warn", sentence, [_gpu_device(0), _gpu_device(1)]))
    )

    first, second = health.resources
    assert (first.state, second.state) == (HostState.WARN, HostState.WARN)
    assert (first.reason, second.reason) == (sentence, "")


def test_resources_count_towards_the_totals_the_overall_state_and_the_severity() -> None:
    health = parse_doctor(
        _with_resources(_memory("warn", "low"), _cpu(), _gpu(), _clock())
    )

    # Four resources, one disk, one certificate; the memory warning is the one.
    assert health.total == 6
    assert (health.ok_count, health.warn_count, health.critical_count) == (5, 1, 0)
    assert health.overall is HostState.WARN
    assert health.severity is ServiceHealth.UNHEALTHY
    assert health.issue_count == 1


def test_a_skipped_resource_is_a_row_that_says_so_and_has_no_verdict() -> None:
    inside_a_container = _check(
        "gpu",
        "skip",
        "not measurable from inside a container: the host's driver tools are not visible",
        {"variant": "nvidia-cuda-13", "override_present": True},
    )
    no_timedatectl = _check("time_sync", "skip", "timedatectl is not usable here", {})

    health = parse_doctor(_with_resources(_memory(), inside_a_container, no_timedatectl))

    gpu, clock = health.resources[1:]
    assert (gpu.label, gpu.state) == ("GPU", HostState.UNKNOWN)
    assert gpu.reason.startswith("not measurable from inside a container")
    assert (clock.label, clock.state, clock.reason) == (
        "System clock",
        HostState.UNKNOWN,
        "timedatectl is not usable here",
    )
    # They are rows, but not checks that were judged: counts and overall ignore them.
    assert (health.resources_measured, health.resources_not_measurable) == (1, 2)
    assert health.total == 3
    assert health.overall is HostState.OK


def test_a_gpu_that_skipped_with_nothing_configured_gets_no_row() -> None:
    # No LocalAI, or the CPU image: the core skips with no details at all. Nothing
    # is missing from the panel, so nothing is said.
    health = parse_doctor(
        _with_resources(_memory(), _check("gpu", "skip", "LocalAI is not enabled", {}))
    )

    assert [r.label for r in health.resources] == ["Memory"]


def test_a_core_that_predates_the_resource_checks_has_no_resource_rows() -> None:
    health = parse_doctor(HEALTHY)

    assert health.available
    assert health.resources == ()
    assert (health.resources_measured, health.resources_not_measurable) == (0, 0)


def test_only_resources_is_still_a_reading() -> None:
    document = json.loads(_with_resources(_memory()))
    document["checks"] = [c for c in document["checks"] if c["name"] == "memory"]

    health = parse_doctor(json.dumps(document))

    assert health.available
    assert [r.label for r in health.resources] == ["Memory"]
    assert health.disks == () and health.certs == ()


def test_a_crashed_resource_check_is_critical_and_does_not_repeat_itself() -> None:
    crashed = _check("memory", "fail", "check crashed: OSError: boom", {})

    (memory,) = parse_doctor(_with_resources(crashed)).resources

    assert memory.state is HostState.CRITICAL
    assert memory.measure == "check crashed: OSError: boom"
    assert memory.reason == ""


def test_malformed_resource_details_are_dropped_not_fatal() -> None:
    mangled = _check(
        "memory",
        "pass",
        "something",
        {"total_bytes": True, "available_bytes": "5", "used_percent": "high", "swap_total_bytes": []},
    )
    garbled_gpu = _check(
        "gpu", "pass", "ok", {"variant": "nvidia-cuda-13", "gpus": ["nope", 3, None]}
    )
    not_a_dict = {"name": "cpu", "status": "pass", "summary": "x", "details": "nope"}

    health = parse_doctor(_with_resources(mangled, garbled_gpu, not_a_dict))

    memory, cpu, gpu = health.resources
    assert (memory.measure, memory.used_percent, memory.detail) == ("something", None, "")
    assert (cpu.detail, cpu.used_percent, cpu.measure) == ("", None, "x")
    # No usable device: one row carrying the core's sentence.
    assert (gpu.label, gpu.measure) == ("GPU", "ok")


# ---------------------------------------------------------------------------
# Running
# ---------------------------------------------------------------------------


class _Doctor:
    """A stand-in for `run_py_cli` that records how it was called."""

    def __init__(
        self,
        stdout: str = HEALTHY,
        *,
        code: int = 0,
        stderr: str = "",
        error: CtlError | None = None,
        delay: float = 0.0,
    ) -> None:
        self.stdout, self.code, self.stderr, self.error, self.delay = (
            stdout,
            code,
            stderr,
            error,
            delay,
        )
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> tuple[int, str, str]:
        self.calls.append(kwargs)
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.code, self.stdout, self.stderr


@pytest.fixture
def workspace(tmp_path: Path) -> str:
    """A workspace whose core has a `doctor`."""
    lib = tmp_path / "papaia" / "tools" / "lib"
    lib.mkdir(parents=True)
    (lib / "doctor.py").write_text("", encoding="utf-8")
    return str(tmp_path)


@pytest.fixture
def doctor(monkeypatch: pytest.MonkeyPatch) -> _Doctor:
    stub = _Doctor()
    monkeypatch.setattr(host_health, "run_py_cli", stub)
    return stub


async def _load(
    workspace: str, *, force: bool = False, config_dir: str = "/cfg"
) -> host_health.HostHealth:
    return await host_health.load_host_health(
        config_dir=config_dir, workspace_dir=workspace, force=force
    )


def _set_interval(tmp_path: Path, seconds: int) -> str:
    """A config directory whose settings carry this interval. Returns its path."""
    config = tmp_path / "config"
    (config / "manager").mkdir(parents=True, exist_ok=True)
    (config / "manager" / "settings.yaml").write_text(
        f"host:\n  refresh_seconds: {seconds}\n", encoding="utf-8"
    )
    return str(config)


async def test_doctor_is_asked_for_the_six_host_checks_only_and_bounded(
    workspace: str, doctor: _Doctor
) -> None:
    await _load(workspace)

    (call,) = doctor.calls
    assert call["command"] == "doctor"
    assert call["config_dir"] == "/cfg"
    assert call["limit"] == host_health.RUN_TIMEOUT_SECONDS
    flags = call["extra_flags"]
    assert flags[0] == "--json"
    skipped = flags[1].removeprefix("--skip=").split(",")
    # Everything the core's registry held before the resource checks existed, and
    # nothing newer: `--skip` refuses a name an older core does not know, so the
    # new checks are asked for by *not* skipping them.
    assert sorted(skipped) == [
        "addon_compat",
        "container_health",
        "dns",
        "docker_version",
        "ports",
    ]
    assert not {"memory", "cpu", "gpu", "time_sync", "disk_space", "certs"} & set(skipped)


async def test_exit_two_with_a_document_is_a_result_not_a_failure(
    workspace: str, doctor: _Doctor
) -> None:
    # The core exits 2 when any check failed -- and still prints everything.
    doctor.stdout = _doctor(paths=[_disk("backup_dir", "/b", 1, 100, "fail")], disk_status="fail")
    doctor.code = 2

    health = await _load(workspace)

    assert health.available
    assert health.overall is HostState.CRITICAL


async def test_exit_two_with_nothing_on_stdout_is_a_refused_argument(
    workspace: str, doctor: _Doctor
) -> None:
    doctor.stdout, doctor.code = "", 2
    doctor.stderr = "ERROR: Unknown check(s): gpu. Valid: docker_version, disk_space.\n"

    health = await _load(workspace)

    assert not health.available
    assert health.reason.startswith("ERROR: Unknown check(s): gpu.")


async def test_a_timeout_or_a_missing_interpreter_is_unavailable_not_an_exception(
    workspace: str, doctor: _Doctor
) -> None:
    doctor.error = CtlError("python3 -m lib.cli doctor timed out after 30s", exit_code=124)

    health = await _load(workspace)

    assert not health.available
    assert "timed out after 30s" in health.reason


async def test_a_core_without_doctor_is_reported_without_forking(
    tmp_path: Path, doctor: _Doctor
) -> None:
    health = await _load(str(tmp_path))

    assert not health.available
    assert "1.4.0" in health.reason
    assert doctor.calls == []


async def test_an_unexpected_error_inside_the_run_still_yields_a_reading(
    workspace: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def explode(**_: Any) -> tuple[int, str, str]:
        raise RuntimeError("boom")

    monkeypatch.setattr(host_health, "run_py_cli", explode)

    health = await _load(workspace)

    assert not health.available


# ---------------------------------------------------------------------------
# Caching
# ---------------------------------------------------------------------------


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """The cache's notion of now, advanced by hand.

    Only the module's `time` is replaced. Patching `time.monotonic` itself would
    stop the event loop's own clock.
    """
    now = [1000.0]
    monkeypatch.setattr(host_health, "time", SimpleNamespace(monotonic=lambda: now[0]))
    return now


async def test_a_reading_is_reused_for_a_minute_unless_settings_say_otherwise(
    workspace: str, doctor: _Doctor, clock: list[float]
) -> None:
    first = await _load(workspace)
    clock[0] += DEFAULT_REFRESH_SECONDS - 1
    assert await _load(workspace) is first
    assert len(doctor.calls) == 1

    clock[0] += 2
    await _load(workspace)
    assert len(doctor.calls) == 2


async def test_the_configured_interval_decides_how_long_a_reading_is_reused(
    workspace: str, doctor: _Doctor, clock: list[float], tmp_path: Path
) -> None:
    config = _set_interval(tmp_path, 300)

    first = await _load(workspace, config_dir=config)
    # Past the default minute, well inside the five that were asked for.
    clock[0] += 200
    assert await _load(workspace, config_dir=config) is first
    assert len(doctor.calls) == 1

    clock[0] += 101
    await _load(workspace, config_dir=config)
    assert len(doctor.calls) == 2


async def test_shortening_the_interval_does_not_wait_out_the_old_one(
    workspace: str, doctor: _Doctor, clock: list[float], tmp_path: Path
) -> None:
    config = _set_interval(tmp_path, 3600)
    await _load(workspace, config_dir=config)

    # Same reading, same age; only the setting changed.
    clock[0] += 11
    _set_interval(tmp_path, 10)
    await _load(workspace, config_dir=config)

    assert len(doctor.calls) == 2


async def test_the_chip_and_the_dot_keep_a_reading_for_three_intervals_at_least(
    workspace: str, doctor: _Doctor, clock: list[float], tmp_path: Path
) -> None:
    config = _set_interval(tmp_path, 3600)
    await _load(workspace, config_dir=config)

    # Longer than the 180 s a default reading survives, shorter than the next due run.
    clock[0] += 3600
    assert host_health.cached_host_health() is not None

    clock[0] += 3 * 3600
    assert host_health.cached_host_health() is None


async def test_ensure_fresh_follows_the_configured_interval(
    workspace: str, doctor: _Doctor, clock: list[float], tmp_path: Path
) -> None:
    config = _set_interval(tmp_path, 120)
    await _load(workspace, config_dir=config)

    clock[0] += 100
    host_health.ensure_fresh(config_dir=config, workspace_dir=workspace)
    await asyncio.sleep(0.05)
    assert len(doctor.calls) == 1

    clock[0] += 21
    host_health.ensure_fresh(config_dir=config, workspace_dir=workspace)
    await asyncio.sleep(0.05)
    assert len(doctor.calls) == 2


async def test_a_failure_is_remembered_for_less_than_a_success(
    workspace: str, doctor: _Doctor, clock: list[float]
) -> None:
    doctor.stdout, doctor.code = "", 2
    await _load(workspace)
    await _load(workspace)
    assert len(doctor.calls) == 1

    clock[0] += host_health.FAILURE_TTL_SECONDS + 1
    doctor.stdout, doctor.code = HEALTHY, 0
    health = await _load(workspace)

    assert len(doctor.calls) == 2
    assert health.available


async def test_a_failure_is_never_remembered_longer_than_the_interval(
    workspace: str, doctor: _Doctor, clock: list[float], tmp_path: Path
) -> None:
    config = _set_interval(tmp_path, 10)
    doctor.stdout, doctor.code = "", 2
    await _load(workspace, config_dir=config)

    # 11 s is under the 15 s a failure is normally kept, over the interval.
    clock[0] += 11
    await _load(workspace, config_dir=config)

    assert len(doctor.calls) == 2


async def test_recheck_skips_the_ttl_but_not_the_minimum_interval(
    workspace: str, doctor: _Doctor, clock: list[float]
) -> None:
    await _load(workspace)

    clock[0] += host_health.RECHECK_MIN_INTERVAL_SECONDS - 1
    await _load(workspace, force=True)
    assert len(doctor.calls) == 1

    clock[0] += 2
    await _load(workspace, force=True)
    assert len(doctor.calls) == 2


async def test_concurrent_viewers_share_one_run(workspace: str, doctor: _Doctor) -> None:
    doctor.delay = 0.05

    readings = await asyncio.gather(*(_load(workspace) for _ in range(8)))

    assert len(doctor.calls) == 1
    assert all(r is readings[0] for r in readings)


async def test_a_viewer_who_leaves_does_not_cancel_the_run_for_the_next(
    workspace: str, doctor: _Doctor
) -> None:
    doctor.delay = 0.05
    leaver = asyncio.ensure_future(_load(workspace))
    await asyncio.sleep(0.01)
    stayer = asyncio.ensure_future(_load(workspace))
    await asyncio.sleep(0.01)

    leaver.cancel()
    health = await stayer

    assert health.available
    assert len(doctor.calls) == 1


async def test_the_cache_only_view_never_forks_and_goes_stale(
    workspace: str, doctor: _Doctor, clock: list[float]
) -> None:
    assert host_health.cached_host_health() is None

    health = await _load(workspace)
    assert host_health.cached_host_health() is health

    clock[0] += host_health.STALE_AFTER_SECONDS + 1
    assert host_health.cached_host_health() is None
    assert len(doctor.calls) == 1


async def test_ensure_fresh_starts_one_background_run_and_the_cache_fills(
    workspace: str, doctor: _Doctor
) -> None:
    doctor.delay = 0.02

    for _ in range(3):
        host_health.ensure_fresh(config_dir="/cfg", workspace_dir=workspace)
    assert host_health.cached_host_health() is None

    await asyncio.sleep(0.1)
    assert host_health.cached_host_health() is not None
    assert len(doctor.calls) == 1

    # Fresh now: asking again starts nothing.
    host_health.ensure_fresh(config_dir="/cfg", workspace_dir=workspace)
    await asyncio.sleep(0.05)
    assert len(doctor.calls) == 1
