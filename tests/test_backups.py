"""Backup catalogue reading and restore-point id validation.

Two things are worth pinning down here. The catalogue is written by papaia-ctl,
so every read has to survive a file that is absent, truncated or from a newer
core -- an operator who has never taken a backup must see an empty page, not a
stack trace. And the restore-point id ends up in both a path join and a
`--restore-point=` argv, so the pattern that guards it is a security control, not
a formatting nicety.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

from app.core.backups import (
    RestorePoint,
    count_older_than,
    find_restore_point,
    is_reachable,
    is_valid_restore_point_id,
    last_backup_age,
    last_successful_backup,
    load_restore_points,
    newest_successful,
    resolve_backup_dir,
    restore_point_time,
    restore_point_to_dict,
    snapshot_manifest,
)

_INDEX = {
    "version": 1,
    "backups": [
        {
            "id": "2026-07-30_10-19-38",
            "path": "/srv/papaia/backup/2026-07-30_10-19-38",
            "created_at": "2026-07-30T08:19:41Z",
            "papaia_version": "1.0.0",
            "project": "papaia",
            "size_mb": 118.4,
            "result": "ok",
            "artifacts": 7,
            "addons": ["paperless"],
        },
        {
            "id": "2026-07-30_12-41-24",
            "path": "/srv/papaia/backup/2026-07-30_12-41-24",
            "created_at": "2026-07-30T10:41:30Z",
            "papaia_version": "1.0.0",
            "project": "papaia",
            "size_mb": 121.0,
            "result": "partial",
            "artifacts": 6,
            "addons": [],
        },
    ],
}


@pytest.fixture
def backup_dir(tmp_path: Path) -> Path:
    target = tmp_path / "backup"
    target.mkdir()
    (target / "backup.yaml").write_text(yaml.safe_dump(_INDEX), encoding="utf-8")
    return target


# ---------------------------------------------------------------------------
# Restore-point ids
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value",
    ["2026-07-30_10-19-38", "1999-01-01_00-00-00"],
)
def test_valid_ids_are_accepted(value: str) -> None:
    assert is_valid_restore_point_id(value)


@pytest.mark.parametrize(
    "value",
    [
        "",
        "latest",
        "2026-07-30",
        "2026-07-30_10-19-38 ",
        "2026-07-30_10-19-38\n2026-07-30_10-19-39",
        "../2026-07-30_10-19-38",
        "2026-07-30_10-19-38/../../etc",
        "/etc/passwd",
        "--restart-clean",
        "-y",
        "2026-07-30_10-19-38; rm -rf /",
    ],
)
def test_hostile_or_malformed_ids_are_rejected(value: str) -> None:
    assert not is_valid_restore_point_id(value)


def test_an_invalid_id_never_reaches_the_filesystem(backup_dir: Path) -> None:
    # Path.joinpath would happily resolve the traversal, so the guard has to run
    # before the join -- both lookups must refuse without touching the disk.
    assert find_restore_point(backup_dir, "../../etc") is None
    assert snapshot_manifest(backup_dir, "../../etc") is None


# ---------------------------------------------------------------------------
# Backup directory resolution
# ---------------------------------------------------------------------------


def test_backup_dir_comes_from_the_config_bundle_env(tmp_path: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text(
        "PAPAIA_HOST=https://papaia.test\nPAPAIA_BACKUP_DIR=/srv/papaia/backup\n",
        encoding="utf-8",
    )
    assert resolve_backup_dir(str(config_dir)) == Path("/srv/papaia/backup")


@pytest.mark.parametrize(
    "env_text", ["", "PAPAIA_HOST=https://papaia.test\n", "PAPAIA_BACKUP_DIR=\n"]
)
def test_missing_backup_dir_resolves_to_none(tmp_path: Path, env_text: str) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text(env_text, encoding="utf-8")
    assert resolve_backup_dir(str(config_dir)) is None


def test_absent_config_env_resolves_to_none(tmp_path: Path) -> None:
    assert resolve_backup_dir(str(tmp_path)) is None


def test_reachability_distinguishes_unset_from_unmounted(tmp_path: Path) -> None:
    assert not is_reachable(None)
    assert not is_reachable(tmp_path / "not-mounted")
    assert is_reachable(tmp_path)


# ---------------------------------------------------------------------------
# Catalogue reading
# ---------------------------------------------------------------------------


def test_restore_points_are_returned_newest_first(backup_dir: Path) -> None:
    points = load_restore_points(backup_dir)
    assert [p.id for p in points] == ["2026-07-30_12-41-24", "2026-07-30_10-19-38"]
    assert points[1].size_mb == 118.4
    assert points[1].artifacts == 7
    assert points[1].addons == ["paperless"]
    assert points[1].papaia_version == "1.0.0"


def test_no_catalogue_yields_no_restore_points(tmp_path: Path) -> None:
    assert load_restore_points(None) == []
    assert load_restore_points(tmp_path) == []


def test_a_corrupt_catalogue_yields_no_restore_points(tmp_path: Path) -> None:
    (tmp_path / "backup.yaml").write_text("backups: [ unterminated", encoding="utf-8")
    assert load_restore_points(tmp_path) == []


def test_a_catalogue_that_is_not_a_mapping_yields_no_restore_points(tmp_path: Path) -> None:
    (tmp_path / "backup.yaml").write_text("- just\n- a\n- list\n", encoding="utf-8")
    assert load_restore_points(tmp_path) == []


def test_unparseable_numeric_fields_fall_back_instead_of_raising(tmp_path: Path) -> None:
    (tmp_path / "backup.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "backups": [
                    {"id": "2026-07-30_10-19-38", "size_mb": "n/a", "artifacts": None}
                ],
            }
        ),
        encoding="utf-8",
    )
    point = load_restore_points(tmp_path)[0]
    assert point.size_mb == 0.0
    assert point.artifacts == 0


def test_find_restore_point_matches_by_id(backup_dir: Path) -> None:
    assert find_restore_point(backup_dir, "2026-07-30_10-19-38") is not None
    assert find_restore_point(backup_dir, "2020-01-01_00-00-00") is None


# ---------------------------------------------------------------------------
# Usability
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("result", "usable"),
    [("ok", True), ("partial", True), ("", True), ("failed", False)],
)
def test_only_a_failed_run_is_unusable(result: str, usable: bool) -> None:
    assert RestorePoint(id="2026-07-30_10-19-38", result=result).is_usable is usable


def test_serialized_restore_point_carries_the_usable_flag() -> None:
    data = restore_point_to_dict(RestorePoint(id="2026-07-30_10-19-38", result="failed"))
    assert data["id"] == "2026-07-30_10-19-38"
    assert data["usable"] is False
    assert data["addons"] == []


# ---------------------------------------------------------------------------
# Snapshot manifest
# ---------------------------------------------------------------------------


def test_manifest_is_read_from_the_snapshot_directory(backup_dir: Path) -> None:
    snapshot = backup_dir / "2026-07-30_10-19-38"
    snapshot.mkdir()
    (snapshot / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "id": "2026-07-30_10-19-38",
                "artifacts": [
                    {
                        "kind": "configdir",
                        "archive": "papaia-config.tar.gz",
                        "target": "/srv/papaia/config",
                        "owner": "core",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    manifest = snapshot_manifest(backup_dir, "2026-07-30_10-19-38")
    assert manifest is not None
    assert manifest["artifacts"][0]["target"] == "/srv/papaia/config"


def test_a_snapshot_without_a_manifest_reads_as_none(backup_dir: Path) -> None:
    (backup_dir / "2026-07-30_12-41-24").mkdir()
    assert snapshot_manifest(backup_dir, "2026-07-30_12-41-24") is None
    assert snapshot_manifest(None, "2026-07-30_12-41-24") is None


# ---------------------------------------------------------------------------
# Age and retention arithmetic
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)


def _at(created_at: str, result: str = "ok", ident: str = "x") -> RestorePoint:
    return RestorePoint(id=ident, created_at=created_at, result=result)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-07-30T08:19:41Z", datetime(2026, 7, 30, 8, 19, 41, tzinfo=UTC)),
        ("2026-07-30T08:19:41+00:00", datetime(2026, 7, 30, 8, 19, 41, tzinfo=UTC)),
        # No offset: the catalogue is UTC, so that is what it is read as.
        ("2026-07-30T08:19:41", datetime(2026, 7, 30, 8, 19, 41, tzinfo=UTC)),
        ("", None),
        ("yesterday", None),
    ],
)
def test_the_time_of_a_restore_point_is_read_as_utc(raw: str, expected: datetime | None) -> None:
    assert restore_point_time(_at(raw)) == expected


def test_the_newest_successful_point_ignores_partial_and_failed_runs() -> None:
    points = [
        _at("2026-07-30T08:00:00Z", "ok", "old-ok"),
        _at("2026-07-31T08:00:00Z", "partial", "partial"),
        _at("2026-08-01T08:00:00Z", "failed", "failed"),
        _at("2026-07-31T09:00:00Z", "ok", "new-ok"),
        _at("garbage", "ok", "no-time"),
    ]
    found = newest_successful(points)
    assert found is not None
    assert found.id == "new-ok"


def test_no_successful_point_is_none() -> None:
    assert newest_successful([]) is None
    assert newest_successful([_at("2026-07-30T08:00:00Z", "failed")]) is None


def test_the_last_successful_backup_comes_from_the_catalogue(backup_dir: Path) -> None:
    found = last_successful_backup(backup_dir)
    assert found is not None
    # The newer entry is `partial`, which is a usable restore point but not a success.
    assert found.id == "2026-07-30_10-19-38"
    assert last_successful_backup(None) is None


def test_the_age_is_measured_from_the_newest_success(tmp_path: Path, backup_dir: Path) -> None:
    config_dir = tmp_path / "config"
    config_dir.mkdir()
    (config_dir / ".env").write_text(f"PAPAIA_BACKUP_DIR={backup_dir}\n", encoding="utf-8")
    now = datetime(2026, 7, 30, 20, 19, 41, tzinfo=UTC)
    assert last_backup_age(str(config_dir), now=now) == timedelta(hours=12)


def test_there_is_no_age_without_a_backup_directory_or_a_success(tmp_path: Path) -> None:
    assert last_backup_age(str(tmp_path)) is None
    empty = tmp_path / "empty"
    empty.mkdir()
    (tmp_path / ".env").write_text(f"PAPAIA_BACKUP_DIR={empty}\n", encoding="utf-8")
    assert last_backup_age(str(tmp_path)) is None


def test_the_retention_count_matches_what_papaia_ctl_would_prune() -> None:
    """Strictly older than the cutoff; an entry whose time cannot be read is kept --
    the same rule as `prune` in the core's backup.py."""
    points = [
        _at("2026-07-18T12:00:00Z", ident="exactly-14-days"),
        _at("2026-07-18T11:59:59Z", ident="just-older"),
        _at("2026-06-01T00:00:00Z", "failed", "old-failed"),
        _at("2026-07-31T00:00:00Z", ident="recent"),
        _at("garbage", ident="unreadable"),
    ]
    assert count_older_than(points, 14, now=_NOW) == 2
    assert count_older_than(points, 365, now=_NOW) == 0
