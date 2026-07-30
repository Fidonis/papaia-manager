"""Restore-runner argument assembly and inspect parsing.

The runner's whole security and correctness surface is the argv it hands to
`docker run` and the mounts it inherits, so both are asserted against a fixture
`docker inspect` payload. Nothing here talks to a Docker daemon.
"""
from __future__ import annotations

import json

import pytest

from app.core.runner import (
    RUNNER_NAME_PREFIX,
    ContainerSpec,
    RunnerError,
    _spec_from_inspect,
    _status_from_inspect,
    build_run_args,
    runner_name,
)

_RESTORE_POINT = "2026-07-30_12-41-24"

_WORKSPACE = "/srv/papaia/workspace"
_CONFIG = "/srv/papaia/config"
_BACKUP = "/mnt/backups/papaia"

# Trimmed to the fields the runner reads, in the shape `docker inspect` emits.
_INSPECT = [
    {
        "Id": "abc123",
        "Name": "/papaia-papaia-manager-1",
        "Config": {
            "Image": "ghcr.io/fidonis/papaia-manager:0.2.0",
            "User": "1000:1000",
            "Labels": {"de.fidonis.module": "papaia-manager"},
        },
        "HostConfig": {
            "Binds": [
                "/var/run/docker.sock:/var/run/docker.sock",
                f"{_WORKSPACE}:{_WORKSPACE}",
                f"{_CONFIG}:{_CONFIG}",
                f"{_BACKUP}:{_BACKUP}",
                f"{_CONFIG}/certs:/certs:ro",
            ],
            "GroupAdd": ["999"],
            "ExtraHosts": ["host.docker.internal:host-gateway"],
        },
    }
]


@pytest.fixture
def spec() -> ContainerSpec:
    return _spec_from_inspect(json.loads(json.dumps(_INSPECT)))


def _args(spec: ContainerSpec, **kwargs: object) -> list[str]:
    return build_run_args(
        spec,
        restore_point=_RESTORE_POINT,
        workspace_dir=_WORKSPACE,
        config_dir=_CONFIG,
        backup_dir=_BACKUP,
        **kwargs,  # type: ignore[arg-type]
    )


# ---------------------------------------------------------------------------
# Spec inheritance
# ---------------------------------------------------------------------------


def test_spec_is_read_from_the_inspect_payload(spec: ContainerSpec) -> None:
    assert spec.image == "ghcr.io/fidonis/papaia-manager:0.2.0"
    assert spec.user == "1000:1000"
    assert spec.group_add == ["999"]
    assert spec.extra_hosts == ["host.docker.internal:host-gateway"]
    assert f"{_BACKUP}:{_BACKUP}" in spec.binds


@pytest.mark.parametrize("payload", [[], {}, [[]], [{"Config": {}, "HostConfig": {}}]])
def test_an_unusable_inspect_payload_raises(payload: object) -> None:
    with pytest.raises(RunnerError):
        _spec_from_inspect(payload)


def test_every_inherited_bind_is_passed_through(spec: ContainerSpec) -> None:
    args = _args(spec)
    for bind in spec.binds:
        assert bind in args, f"bind {bind} was dropped"
        assert args[args.index(bind) - 1] == "--volume"


def test_the_backup_directory_is_mounted_at_its_host_path(spec: ContainerSpec) -> None:
    # Path parity is what makes --backup-dir mean the same thing on both sides.
    assert f"{_BACKUP}:{_BACKUP}" in _args(spec)


def test_user_and_group_membership_are_inherited(spec: ContainerSpec) -> None:
    args = _args(spec)
    assert args[args.index("--user") + 1] == "1000:1000"
    assert args[args.index("--group-add") + 1] == "999"
    assert args[args.index("--add-host") + 1] == "host.docker.internal:host-gateway"


# ---------------------------------------------------------------------------
# The papaia-ctl command line
# ---------------------------------------------------------------------------


def test_the_runner_is_detached_named_and_not_restarted(spec: ContainerSpec) -> None:
    args = _args(spec)
    assert "--detach" in args
    assert args[args.index("--name") + 1] == f"{RUNNER_NAME_PREFIX}{_RESTORE_POINT}"
    assert args[args.index("--restart") + 1] == "no"


def test_the_runner_carries_the_discovery_labels(spec: ContainerSpec) -> None:
    args = _args(spec)
    labels = [args[i + 1] for i, a in enumerate(args) if a == "--label"]
    assert "de.fidonis.module=papaia-restore" in labels
    assert f"de.fidonis.restore-point={_RESTORE_POINT}" in labels


def test_papaia_ctl_is_invoked_from_the_mounted_workspace(spec: ContainerSpec) -> None:
    args = _args(spec)
    image_at = args.index(spec.image)
    command = args[image_at + 1 :]
    assert command[0] == "bash"
    assert command[1].replace("\\", "/") == f"{_WORKSPACE}/papaia/tools/papaia-ctl"
    assert command[2] == "restore"


def test_the_command_is_non_interactive_and_scoped(spec: ContainerSpec) -> None:
    args = _args(spec)
    # -y is mandatory: papaia-ctl refuses to restore without confirmation and
    # there is no TTY in a detached container.
    assert "-y" in args
    assert f"--restore-point={_RESTORE_POINT}" in args
    assert f"--config-dir={_CONFIG}" in args
    assert f"--backup-dir={_BACKUP}" in args


def test_restart_clean_is_absent_unless_requested(spec: ContainerSpec) -> None:
    assert "--restart-clean" not in _args(spec)
    assert "--restart-clean" in _args(spec, restart_clean=True)


@pytest.mark.parametrize(
    "bad_id", ["", "latest", "../../etc", "-y", "2026-07-30_12-41-24 --restart-clean"]
)
def test_an_invalid_restore_point_is_refused_before_any_argv_is_built(
    spec: ContainerSpec, bad_id: str
) -> None:
    with pytest.raises(ValueError, match="not a valid snapshot id"):
        build_run_args(
            spec,
            restore_point=bad_id,
            workspace_dir=_WORKSPACE,
            config_dir=_CONFIG,
            backup_dir=_BACKUP,
        )


# ---------------------------------------------------------------------------
# Status parsing
# ---------------------------------------------------------------------------


def _status_payload(status: str, exit_code: int) -> list[dict[str, object]]:
    return [
        {
            "Name": f"/{RUNNER_NAME_PREFIX}{_RESTORE_POINT}",
            "State": {
                "Status": status,
                "ExitCode": exit_code,
                "StartedAt": "2026-07-30T10:45:00Z",
                "FinishedAt": "2026-07-30T10:52:11Z",
            },
            "Config": {"Labels": {"de.fidonis.restore-point": _RESTORE_POINT}},
        }
    ]


def test_a_running_runner_reports_no_exit_code() -> None:
    # Docker reports ExitCode 0 while a container runs, which would otherwise
    # read as a completed, successful restore.
    status = _status_from_inspect(_status_payload("running", 0))
    assert status.is_running
    assert status.exit_code is None
    assert not status.succeeded
    assert status.restore_point == _RESTORE_POINT


def test_a_clean_exit_is_reported_as_success() -> None:
    status = _status_from_inspect(_status_payload("exited", 0))
    assert not status.is_running
    assert status.succeeded
    assert status.exit_code == 0


def test_a_non_zero_exit_is_reported_as_failure() -> None:
    status = _status_from_inspect(_status_payload("exited", 3))
    assert not status.is_running
    assert not status.succeeded
    assert status.exit_code == 3


def test_the_restore_point_falls_back_to_the_container_name() -> None:
    payload = _status_payload("exited", 0)
    payload[0]["Config"] = {"Labels": {}}
    assert _status_from_inspect(payload).restore_point == _RESTORE_POINT


def test_runner_name_is_derived_from_the_restore_point() -> None:
    assert runner_name(_RESTORE_POINT) == f"{RUNNER_NAME_PREFIX}{_RESTORE_POINT}"


@pytest.mark.parametrize("payload", [[], {}, [None]])
def test_an_unusable_status_payload_raises(payload: object) -> None:
    with pytest.raises(RunnerError):
        _status_from_inspect(payload)
