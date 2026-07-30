"""Backup catalogue reading and restore-point id validation.

Two things are worth pinning down here. The catalogue is written by papaia-ctl,
so every read has to survive a file that is absent, truncated or from a newer
core -- an operator who has never taken a backup must see an empty page, not a
stack trace. And the restore-point id ends up in both a path join and a
`--restore-point=` argv, so the pattern that guards it is a security control, not
a formatting nicety.
"""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.core.backups import (
    RestorePoint,
    find_restore_point,
    is_reachable,
    is_valid_restore_point_id,
    load_restore_points,
    resolve_backup_dir,
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


@pytest.mark.parametrize("env_text", ["", "PAPAIA_HOST=https://papaia.test\n", "PAPAIA_BACKUP_DIR=\n"])
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
