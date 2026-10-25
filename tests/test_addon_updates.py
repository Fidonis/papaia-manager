"""The snapshot layout, the SemVer rule and the helpers around `addon update` and `addon rollback`.

Everything here runs against a temporary workspace and config directory. papaia-ctl is replaced by
a function that returns the lines it would print, so no Docker and no checkout of the core is
needed; the `tar` and `git` the git catalog path calls are real.
"""
from __future__ import annotations

import asyncio
import os
import subprocess
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core import addon_updates
from app.core.addon_updates import (
    Staged,
    UpdateError,
    answerable_keys,
    changelog_between,
    core_supports_updates,
    fetch_plan,
    next_history,
    parse_plan_output,
    previous_version,
    prune_after,
    stage_revision,
    sync_installed_after_rollback,
    sync_installed_after_update,
    version_state,
    write_answers_file,
)
from app.core.catalogs import Catalog
from app.core.ctl import CtlError
from app.core.resolve import update_available
from app.core.semver import compare, is_newer
from app.core.snapshots import (
    HistoryEntry,
    InstalledAddon,
    installed_addon_dir,
    load_installed,
    managed_snapshot_path,
    prune_snapshots,
    record_installed,
)

# ── SemVer ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("a", "b", "order"),
    [
        ("1.0.0", "1.0.0", 0),
        ("1.0.0", "1.0.1", -1),
        ("1.10.0", "1.9.0", 1),
        ("2.0.0", "1.99.99", 1),
        ("1.0.0-rc.1", "1.0.0", -1),
        ("1.0.0-rc.1", "1.0.0-rc.2", -1),
        ("1.0.0-rc.2", "1.0.0-rc.10", -1),
        ("1.0.0-alpha", "1.0.0-alpha.1", -1),
        ("1.0.0-1", "1.0.0-alpha", -1),
        ("1.0.0+build.5", "1.0.0", 0),
    ],
)
def test_semver_precedence(a: str, b: str, order: int) -> None:
    assert compare(a, b) == order
    assert compare(b, a) == -order


@pytest.mark.parametrize("text", ["1.0", "v1.0.0", "latest", "", "1.0.0.0", "unknown"])
def test_what_is_not_semver_has_no_order(text: str) -> None:
    assert compare(text, "1.0.0") is None
    assert is_newer(text, "1.0.0") is None


def _installed(version: str, *, managed: bool = True) -> InstalledAddon:
    return InstalledAddon(
        catalog="cat", commit="c" * 40, manifest_version=version,
        installed_at=datetime(2026, 10, 1, tzinfo=UTC), managed=managed,
    )


def test_a_higher_catalog_version_is_an_update() -> None:
    assert update_available(_installed("1.0.0"), "1.1.0") is True


def test_an_older_catalog_version_is_not_offered_as_an_update() -> None:
    assert update_available(_installed("1.2.0"), "1.1.0") is False
    assert update_available(_installed("1.2.0"), "1.2.0") is False


def test_a_version_that_is_not_semver_is_an_update_when_it_differs() -> None:
    assert update_available(_installed("2026-10"), "2026-11") is True
    assert update_available(_installed("2026-10"), "2026-10") is False
    assert update_available(_installed("1.0.0"), "next") is True


def test_only_a_managed_installation_with_a_catalog_version_counts() -> None:
    assert update_available(_installed("1.0.0", managed=False), "2.0.0") is False
    assert update_available(_installed("1.0.0"), None) is False
    assert update_available(None, "2.0.0") is False


# ── The layout ──────────────────────────────────────────────────────────────


def test_a_revision_directory_keeps_the_add_on_name_last(tmp_path: Path) -> None:
    path = managed_snapshot_path(str(tmp_path), "cat", "paperless", "0123456789ab")

    assert path == tmp_path / "addons" / "_managed" / "cat" / "0123456789ab" / "paperless"
    assert path.name == "paperless"


def test_without_a_revision_the_path_is_the_one_from_before_updates(tmp_path: Path) -> None:
    assert managed_snapshot_path(str(tmp_path), "cat", "paperless") == (
        tmp_path / "addons" / "_managed" / "cat" / "paperless"
    )


def test_the_installed_directory_is_the_registered_one(tmp_path: Path) -> None:
    entry = {"name": "paperless", "path": str(tmp_path / "elsewhere" / "paperless")}

    assert installed_addon_dir(entry, str(tmp_path), "cat", "paperless") == (
        tmp_path / "elsewhere" / "paperless"
    )


def test_a_relative_registration_is_resolved_against_the_workspace(tmp_path: Path) -> None:
    entry = {"name": "paperless", "path": "addons/paperless"}

    assert installed_addon_dir(entry, str(tmp_path), "cat", "paperless") == (
        tmp_path / "addons" / "paperless"
    )


def test_without_a_registration_the_old_directory_is_the_answer(tmp_path: Path) -> None:
    assert installed_addon_dir(None, str(tmp_path), "cat", "paperless") == (
        tmp_path / "addons" / "_managed" / "cat" / "paperless"
    )


# ── installed.yaml ──────────────────────────────────────────────────────────


def test_the_history_survives_a_round_trip(tmp_path: Path) -> None:
    taken = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    record_installed(
        str(tmp_path), name="paperless", catalog="cat", commit="b" * 40, manifest_version="1.1.0",
        history=[HistoryEntry("a" * 40, "1.0.0", taken)],
    )

    loaded = load_installed(str(tmp_path))["paperless"]

    assert loaded.history == [HistoryEntry("a" * 40, "1.0.0", taken)]


def test_an_entry_from_before_the_history_loads_without_one(tmp_path: Path) -> None:
    path = tmp_path / "manager" / "installed.yaml"
    path.parent.mkdir(parents=True)
    path.write_text(
        yaml.safe_dump({"version": 1, "addons": {"paperless": {
            "catalog": "cat", "commit": "a" * 40, "manifest_version": "1.0.0",
            "installed_at": "2026-09-01T12:00:00+00:00", "managed": True,
        }}}),
        encoding="utf-8",
    )

    assert load_installed(str(tmp_path))["paperless"].history == []


def test_a_record_without_history_keeps_the_one_it_replaces(tmp_path: Path) -> None:
    taken = datetime(2026, 9, 1, tzinfo=UTC)
    kwargs: dict[str, Any] = {"name": "paperless", "catalog": "cat"}
    record_installed(
        str(tmp_path), commit="b" * 40, manifest_version="1.1.0",
        history=[HistoryEntry("a" * 40, "1.0.0", taken)], **kwargs,
    )

    record_installed(str(tmp_path), commit="b" * 40, manifest_version="1.1.0", **kwargs)

    assert [h.manifest_version for h in load_installed(str(tmp_path))["paperless"].history] == [
        "1.0.0"
    ]


def test_the_history_is_cut_to_the_snapshots_that_are_kept(tmp_path: Path) -> None:
    taken = datetime(2026, 9, 1, tzinfo=UTC)
    entries = [HistoryEntry(f"{i}" * 40, f"1.0.{i}", taken) for i in range(6)]
    record_installed(
        str(tmp_path), name="paperless", catalog="cat", commit="f" * 40,
        manifest_version="2.0.0", history=entries,
    )

    assert len(load_installed(str(tmp_path))["paperless"].history) == 3


def test_replacing_a_version_puts_it_first_in_the_history() -> None:
    inst = _installed("1.0.0")
    inst.history = [HistoryEntry("a" * 40, "0.9.0", datetime(2026, 8, 1, tzinfo=UTC))]

    after = next_history(inst, "d" * 40)

    assert [h.manifest_version for h in after] == ["1.0.0", "0.9.0"]


def test_installing_the_commit_that_is_recorded_adds_nothing() -> None:
    inst = _installed("1.0.0")

    assert next_history(inst, inst.commit) == []


def test_there_is_no_history_without_an_installation() -> None:
    assert next_history(None, "d" * 40) == []


# ── Pruning ─────────────────────────────────────────────────────────────────


def _snapshot(workspace: Path, revision: str, name: str = "paperless", age: int = 0) -> Path:
    path = managed_snapshot_path(str(workspace), "cat", name, revision)
    path.mkdir(parents=True)
    (path / "papaia-app.yaml").write_text(f"name: {name}\nversion: {revision}\n", encoding="utf-8")
    stamp = 1_700_000_000 + age
    os.utime(path, (stamp, stamp))
    return path


def test_pruning_keeps_what_is_needed_and_fills_up_with_the_newest(tmp_path: Path) -> None:
    registered = _snapshot(tmp_path, "aaaaaaaaaaaa", age=10)
    previous = _snapshot(tmp_path, "bbbbbbbbbbbb", age=20)
    newer = _snapshot(tmp_path, "cccccccccccc", age=30)
    older = _snapshot(tmp_path, "dddddddddddd", age=5)
    oldest = _snapshot(tmp_path, "eeeeeeeeeeee", age=1)

    removed = prune_snapshots(str(tmp_path), "cat", "paperless", keep=[registered, previous])

    assert sorted(removed) == sorted([older, oldest])
    assert registered.is_dir()
    assert previous.is_dir()
    assert newer.is_dir()


def test_pruning_never_removes_what_is_kept_even_beyond_the_limit(tmp_path: Path) -> None:
    kept = [_snapshot(tmp_path, f"{c}" * 12, age=i) for i, c in enumerate("abcd")]

    assert prune_snapshots(str(tmp_path), "cat", "paperless", keep=kept) == []


def test_the_directory_from_before_updates_goes_when_nothing_points_at_it(tmp_path: Path) -> None:
    legacy = managed_snapshot_path(str(tmp_path), "cat", "paperless")
    legacy.mkdir(parents=True)
    (legacy / "papaia-app.yaml").write_text("name: paperless\n", encoding="utf-8")
    current = _snapshot(tmp_path, "aaaaaaaaaaaa")

    removed = prune_snapshots(str(tmp_path), "cat", "paperless", keep=[current])

    assert removed == [legacy]
    assert not legacy.exists()


def test_the_directory_from_before_updates_stays_while_it_is_registered(tmp_path: Path) -> None:
    legacy = managed_snapshot_path(str(tmp_path), "cat", "paperless")
    legacy.mkdir(parents=True)
    (legacy / "papaia-app.yaml").write_text("name: paperless\n", encoding="utf-8")

    assert prune_snapshots(str(tmp_path), "cat", "paperless", keep=[legacy]) == []
    assert legacy.is_dir()


def test_an_add_on_named_like_a_revision_is_not_taken_for_one(tmp_path: Path) -> None:
    odd = managed_snapshot_path(str(tmp_path), "cat", "abcdef012345")
    odd.mkdir(parents=True)
    (odd / "papaia-app.yaml").write_text("name: abcdef012345\n", encoding="utf-8")
    current = _snapshot(tmp_path, "aaaaaaaaaaaa")

    prune_snapshots(str(tmp_path), "cat", "paperless", keep=[current])

    assert odd.is_dir()


def test_pruning_one_add_on_leaves_another_one_of_the_same_revision(tmp_path: Path) -> None:
    mine = _snapshot(tmp_path, "aaaaaaaaaaaa", "paperless", age=1)
    other = _snapshot(tmp_path, "aaaaaaaaaaaa", "n8n", age=1)
    keep = [_snapshot(tmp_path, "bbbbbbbbbbbb", "paperless", age=2)]
    keep += [_snapshot(tmp_path, "cccccccccccc", "paperless", age=3)]
    keep += [_snapshot(tmp_path, "dddddddddddd", "paperless", age=4)]

    prune_snapshots(str(tmp_path), "cat", "paperless", keep=keep)

    assert not mine.exists()
    assert other.is_dir()


def test_pruning_with_nothing_kept_removes_the_add_on_entirely(tmp_path: Path) -> None:
    first = _snapshot(tmp_path, "aaaaaaaaaaaa")
    second = _snapshot(tmp_path, "bbbbbbbbbbbb")

    removed = prune_snapshots(str(tmp_path), "cat", "paperless", keep=[], limit=0)

    assert sorted(removed) == sorted([first, second])
    assert not first.parent.exists()


# ── papaia-ctl's output and answers ─────────────────────────────────────────


PLAN = '{\n  "schema_version": 1,\n  "name": "paperless",\n  "blocked": false\n}'


def test_the_plan_is_found_among_other_output() -> None:
    lines = ["WARNING: something", *PLAN.splitlines(), "trailing text"]

    assert parse_plan_output(lines) == {"schema_version": 1, "name": "paperless", "blocked": False}


def test_output_without_a_plan_yields_none() -> None:
    assert parse_plan_output(["ERROR: addon 'x' is not registered."]) is None
    assert parse_plan_output(["{", "not json"]) is None
    assert parse_plan_output([]) is None


def test_the_keys_that_may_be_answered_are_the_ones_the_plan_asks_about() -> None:
    plan = {"env": {"new": [
        {"key": "A", "class": "change_me"},
        {"key": "B", "class": "generated"},
        {"key": "C", "class": "replace_secret"},
        {"key": "D", "class": "literal"},
    ]}}

    assert answerable_keys(plan) == {"A", "C"}


def test_the_answers_are_written_to_a_file_only_this_user_can_read() -> None:
    path = write_answers_file({"A": "one", "B": "two words"})
    try:
        assert path.read_text(encoding="utf-8") == "A=one\nB=two words\n"
        if os.name == "posix":
            assert path.stat().st_mode & 0o077 == 0
    finally:
        path.unlink()


def test_a_value_with_a_line_break_is_refused() -> None:
    with pytest.raises(UpdateError):
        write_answers_file({"A": "one\ntwo"})


# ── Asking papaia-ctl for the plan ──────────────────────────────────────────


def _fake_ctl(
    monkeypatch: pytest.MonkeyPatch, lines: list[str], *, fail_with: int | None = None
) -> list[list[str]]:
    flags: list[list[str]] = []

    async def run_addon_verb(
        *, extra_flags: list[str] | None = None, **_: Any
    ) -> AsyncIterator[str]:
        flags.append(list(extra_flags or []))

        async def stream() -> AsyncIterator[str]:
            for line in lines:
                yield line
            if fail_with is not None:
                raise CtlError("papaia-ctl failed", fail_with)

        return stream()

    monkeypatch.setattr(addon_updates, "run_addon_verb", run_addon_verb)
    return flags


def test_the_plan_is_asked_for_as_json_without_changing_anything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    flags = _fake_ctl(monkeypatch, PLAN.splitlines())

    plan = asyncio.run(fetch_plan(
        name="paperless", candidate=tmp_path / "x" / "paperless",
        workspace_dir=str(tmp_path), config_dir=str(tmp_path),
    ))

    assert plan["name"] == "paperless"
    assert flags == [["--dry-run", "--json", f"--path={tmp_path / 'x' / 'paperless'}"]]


def test_a_plan_the_gate_refused_is_still_a_plan(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    blocked = PLAN.replace("false", "true").splitlines()
    _fake_ctl(monkeypatch, blocked, fail_with=2)

    plan = asyncio.run(fetch_plan(
        name="paperless", candidate=tmp_path, workspace_dir=str(tmp_path), config_dir=str(tmp_path),
    ))

    assert plan["blocked"] is True


def test_a_refusal_before_there_is_a_plan_carries_papaia_ctls_message(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_ctl(monkeypatch, ["ERROR: addon 'paperless' is not registered."], fail_with=2)

    with pytest.raises(UpdateError, match="is not registered"):
        asyncio.run(fetch_plan(
            name="paperless", candidate=tmp_path, workspace_dir=str(tmp_path),
            config_dir=str(tmp_path),
        ))


# ── Whether the core has the verbs ──────────────────────────────────────────


def _core(workspace: Path, text: str) -> None:
    script = workspace / "papaia" / "tools" / "lib" / "sh" / "addon.sh"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(text, encoding="utf-8")


def test_a_core_with_the_update_verb_is_recognised(tmp_path: Path) -> None:
    _core(tmp_path, "cmd_addon_update() {\n    :\n}\n")

    assert core_supports_updates(str(tmp_path)) is True


def test_a_core_without_it_is_not(tmp_path: Path) -> None:
    _core(tmp_path, "cmd_addon_install() {\n    :\n}\n")

    assert core_supports_updates(str(tmp_path)) is False


def test_a_missing_core_is_not(tmp_path: Path) -> None:
    assert core_supports_updates(str(tmp_path)) is False


def test_a_core_that_is_upgraded_in_place_is_read_again(tmp_path: Path) -> None:
    _core(tmp_path, "cmd_addon_install() {\n}\n")
    assert core_supports_updates(str(tmp_path)) is False

    script = tmp_path / "papaia" / "tools" / "lib" / "sh" / "addon.sh"
    script.write_text("cmd_addon_update() {\n}\n", encoding="utf-8")
    os.utime(script, (1_800_000_000, 1_800_000_000))

    assert core_supports_updates(str(tmp_path)) is True


# ── Staging ─────────────────────────────────────────────────────────────────


def _write_addon(directory: Path, version: str) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "papaia-app.yaml").write_text(
        f"name: paperless\nversion: {version}\n", encoding="utf-8"
    )
    (directory / ".env.example").write_text("KEY=value\n", encoding="utf-8")


def _local_catalog(tmp_path: Path, version: str) -> Catalog:
    source = tmp_path / "local-catalog"
    _write_addon(source / "paperless", version)
    return Catalog(name="cat", type="local", path=str(source))


def test_a_local_catalog_is_staged_to_a_directory_of_its_own(tmp_path: Path) -> None:
    catalog = _local_catalog(tmp_path, "1.1.0")

    staged = asyncio.run(
        stage_revision(catalog=catalog, workspace_dir=str(tmp_path), name="paperless")
    )

    assert staged.version == "1.1.0"
    assert staged.commit == "local"
    assert staged.revision.startswith("local-")
    assert staged.path == managed_snapshot_path(str(tmp_path), "cat", "paperless", staged.revision)
    assert (staged.path / ".env.example").is_file()


def test_the_reviewed_local_revision_is_used_as_it_is(tmp_path: Path) -> None:
    catalog = _local_catalog(tmp_path, "1.1.0")
    first = asyncio.run(
        stage_revision(catalog=catalog, workspace_dir=str(tmp_path), name="paperless")
    )
    # The working directory changes after the review; the update must not pick that up.
    _write_addon(Path(catalog.path or "") / "paperless", "9.9.9")

    again = asyncio.run(stage_revision(
        catalog=catalog, workspace_dir=str(tmp_path), name="paperless", revision=first.revision,
    ))

    assert again.path == first.path
    assert again.version == "1.1.0"


def test_a_local_revision_that_is_gone_is_refused(tmp_path: Path) -> None:
    catalog = _local_catalog(tmp_path, "1.1.0")

    with pytest.raises(UpdateError, match="gone"):
        asyncio.run(stage_revision(
            catalog=catalog, workspace_dir=str(tmp_path), name="paperless",
            revision="local-20260101000000",
        ))


def test_an_add_on_that_left_the_catalog_is_refused(tmp_path: Path) -> None:
    catalog = Catalog(name="cat", type="local", path=str(tmp_path / "empty"))

    with pytest.raises(UpdateError, match="no longer in catalog"):
        asyncio.run(stage_revision(catalog=catalog, workspace_dir=str(tmp_path), name="paperless"))


def _git(repo: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.org", "-C", str(repo), *args],
        check=True, capture_output=True, text=True,
    )
    return done.stdout.strip()


def _tar_is_gnu() -> bool:
    try:
        out = subprocess.run(["tar", "--version"], capture_output=True, text=True, check=False)
    except OSError:
        return False
    return "GNU tar" in out.stdout


needs_gnu_tar = pytest.mark.skipif(
    not _tar_is_gnu(), reason="materialize_snapshot calls GNU tar (--touch)"
)


def _git_catalog(workspace: Path, version: str) -> tuple[Catalog, Path]:
    clone = workspace / "addons" / "_catalogs" / "cat"
    clone.mkdir(parents=True)
    _git(clone, "init", "-q")
    _write_addon(clone / "paperless", version)
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", f"paperless {version}")
    return Catalog(name="cat", type="git", url="https://example.org/cat.git"), clone


_CHANGELOG = """# Changelog

Intro text that belongs to no version.

## [2.0.0] - 2026-10-24

### Changed
- **Breaking:** the bridge network is renamed.

### Upgrade notes
- Needs papaia 1.3.0 or newer.

## [1.1.0] - 2026-09-04

### Changed
- Nothing but the version.

## [1.0.0] - 2026-08-18

### Added
- First release.
"""


def _with_changelog(tmp_path: Path, text: str = _CHANGELOG) -> Path:
    (tmp_path / "CHANGELOG.md").write_text(text, encoding="utf-8")
    return tmp_path


def test_the_changelog_gives_the_sections_between_the_two_versions(tmp_path: Path) -> None:
    sections = changelog_between(_with_changelog(tmp_path), "1.0.0", "2.0.0")

    assert [(s.version, s.date) for s in sections] == [
        ("2.0.0", "2026-10-24"),
        ("1.1.0", "2026-09-04"),
    ]
    assert "### Upgrade notes" in sections[0].body
    assert "Needs papaia 1.3.0 or newer." in sections[0].body
    assert "Intro text" not in sections[0].body


def test_the_changelog_stops_at_the_candidate_and_starts_after_the_installed_version(
    tmp_path: Path,
) -> None:
    sections = changelog_between(_with_changelog(tmp_path), "1.1.0", "2.0.0")
    assert [s.version for s in sections] == ["2.0.0"]

    sections = changelog_between(_with_changelog(tmp_path), "1.0.0", "1.1.0")
    assert [s.version for s in sections] == ["1.1.0"]


def test_a_changelog_that_is_missing_or_not_in_the_format_gives_nothing(tmp_path: Path) -> None:
    assert changelog_between(tmp_path, "1.0.0", "2.0.0") == []
    assert changelog_between(_with_changelog(tmp_path, "just some text\n"), "1.0.0", "2.0.0") == []
    unversioned = "## [Unreleased]\n- something\n## [not-a-version] - x\n- else\n"
    assert changelog_between(_with_changelog(tmp_path, unversioned), "1.0.0", "2.0.0") == []


def test_a_changelog_that_is_not_text_gives_nothing(tmp_path: Path) -> None:
    (tmp_path / "CHANGELOG.md").write_bytes(b"\xff\xfe\x00 not utf-8")
    assert changelog_between(tmp_path, "1.0.0", "2.0.0") == []


def test_a_long_changelog_is_cut_off_after_the_first_sections(tmp_path: Path) -> None:
    body = "- " + "x" * 3000 + "\n"
    text = "".join(f"## [1.{i}.0] - 2026-01-01\n{body}\n" for i in range(9, 0, -1))
    sections = changelog_between(_with_changelog(tmp_path, text), "1.0.0", "1.9.0")

    assert 1 <= len(sections) < 9
    assert sections[0].version == "1.9.0"


def test_a_single_section_over_the_limit_is_cut_not_dropped(tmp_path: Path) -> None:
    text = "## [1.1.0] - 2026-01-01\n" + "word " * 5000 + "\n"
    (section,) = changelog_between(_with_changelog(tmp_path, text), "1.0.0", "1.1.0")

    assert section.version == "1.1.0"
    assert 0 < len(section.body) <= 6000


def test_a_git_catalog_that_cannot_be_unpacked_is_an_error_of_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog, _clone = _git_catalog(tmp_path, "1.1.0")

    async def _unpack(**_: Any) -> None:
        raise RuntimeError("tar extract failed: tar: Option --touch is not supported")

    monkeypatch.setattr(addon_updates, "materialize_snapshot", _unpack)

    with pytest.raises(UpdateError, match="could not be taken from the catalog.*--touch"):
        asyncio.run(
            stage_revision(catalog=catalog, workspace_dir=str(tmp_path), name="paperless")
        )


def test_a_local_catalog_that_cannot_be_copied_is_an_error_of_the_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = _local_catalog(tmp_path, "1.1.0")

    async def _copy(*_: Any, **__: Any) -> None:
        raise PermissionError("denied")

    monkeypatch.setattr(addon_updates, "materialize_local_addon", _copy)

    with pytest.raises(UpdateError, match="could not be taken from the catalog.*denied"):
        asyncio.run(
            stage_revision(catalog=catalog, workspace_dir=str(tmp_path), name="paperless")
        )


@needs_gnu_tar
def test_a_git_catalog_is_staged_by_commit(tmp_path: Path) -> None:
    catalog, clone = _git_catalog(tmp_path, "1.1.0")
    sha = _git(clone, "rev-parse", "HEAD")

    staged = asyncio.run(
        stage_revision(catalog=catalog, workspace_dir=str(tmp_path), name="paperless")
    )

    assert staged.revision == sha[:12]
    assert staged.commit == sha
    assert staged.version == "1.1.0"
    assert staged.path == managed_snapshot_path(str(tmp_path), "cat", "paperless", sha[:12])
    assert (staged.path / "papaia-app.yaml").is_file()


@needs_gnu_tar
def test_staging_the_same_commit_again_does_not_touch_the_directory(tmp_path: Path) -> None:
    catalog, _clone = _git_catalog(tmp_path, "1.1.0")
    first = asyncio.run(
        stage_revision(catalog=catalog, workspace_dir=str(tmp_path), name="paperless")
    )
    marker = first.path / "written-by-the-add-on"
    marker.write_text("data", encoding="utf-8")

    again = asyncio.run(
        stage_revision(catalog=catalog, workspace_dir=str(tmp_path), name="paperless")
    )

    assert again.path == first.path
    assert marker.read_text(encoding="utf-8") == "data"


@needs_gnu_tar
def test_a_catalog_that_moved_on_since_the_review_is_refused(tmp_path: Path) -> None:
    catalog, clone = _git_catalog(tmp_path, "1.1.0")
    reviewed = asyncio.run(
        stage_revision(catalog=catalog, workspace_dir=str(tmp_path), name="paperless")
    )
    _write_addon(clone / "paperless", "1.2.0")
    _git(clone, "add", "-A")
    _git(clone, "commit", "-q", "-m", "paperless 1.2.0")

    with pytest.raises(UpdateError, match="changed since"):
        asyncio.run(stage_revision(
            catalog=catalog, workspace_dir=str(tmp_path), name="paperless",
            revision=reviewed.revision,
        ))


def test_a_clone_without_a_commit_is_refused(tmp_path: Path) -> None:
    clone = tmp_path / "addons" / "_catalogs" / "cat"
    clone.mkdir(parents=True)
    _git(clone, "init", "-q")
    catalog = Catalog(name="cat", type="git", url="https://example.org/cat.git")

    with pytest.raises(UpdateError, match="no commit"):
        asyncio.run(stage_revision(catalog=catalog, workspace_dir=str(tmp_path), name="paperless"))


# ── Following what papaia-ctl registered ────────────────────────────────────


def _register(config: Path, path: Path | str, **extra: Any) -> None:
    config.mkdir(parents=True, exist_ok=True)
    entry = {"name": "paperless", "path": str(path), "active": True, **extra}
    (config / "deployment.yaml").write_text(yaml.safe_dump({"addons": [entry]}), encoding="utf-8")


def _staged(workspace: Path, revision: str, version: str, commit: str) -> Staged:
    path = _snapshot(workspace, revision)
    (path / "papaia-app.yaml").write_text(f"version: {version}\n", encoding="utf-8")
    return Staged(revision, commit, path, version)


def test_installed_yaml_follows_a_registration_that_moved(tmp_path: Path) -> None:
    config, workspace = tmp_path / "config", tmp_path / "workspace"
    inst = _installed("1.0.0")
    staged = _staged(workspace, "bbbbbbbbbbbb", "1.1.0", "b" * 40)
    _register(config, staged.path)

    assert sync_installed_after_update(
        str(config), str(workspace), name="paperless", inst=inst, staged=staged
    )

    recorded = load_installed(str(config))["paperless"]
    assert (recorded.manifest_version, recorded.commit) == ("1.1.0", "b" * 40)
    assert [h.manifest_version for h in recorded.history] == ["1.0.0"]


def test_installed_yaml_is_left_alone_when_the_registration_did_not_move(tmp_path: Path) -> None:
    config, workspace = tmp_path / "config", tmp_path / "workspace"
    inst = _installed("1.0.0")
    staged = _staged(workspace, "bbbbbbbbbbbb", "1.1.0", "b" * 40)
    _register(config, workspace / "addons" / "_managed" / "cat" / "paperless")

    assert not sync_installed_after_update(
        str(config), str(workspace), name="paperless", inst=inst, staged=staged
    )

    assert load_installed(str(config)) == {}


def test_a_relative_registration_is_followed_too(tmp_path: Path) -> None:
    config, workspace = tmp_path / "config", tmp_path / "workspace"
    staged = _staged(workspace, "bbbbbbbbbbbb", "1.1.0", "b" * 40)
    _register(config, staged.path.relative_to(workspace))

    assert sync_installed_after_update(
        str(config), str(workspace), name="paperless", inst=_installed("1.0.0"), staged=staged
    )


def test_a_rollback_makes_installed_yaml_say_the_earlier_version(tmp_path: Path) -> None:
    config, workspace = tmp_path / "config", tmp_path / "workspace"
    earlier = _staged(workspace, "aaaaaaaaaaaa", "1.0.0", "a" * 40)
    current = _staged(workspace, "bbbbbbbbbbbb", "1.1.0", "b" * 40)
    inst = _installed("1.1.0")
    inst.commit = "b" * 40
    inst.history = [HistoryEntry("a" * 40, "1.0.0", datetime(2026, 9, 1, tzinfo=UTC))]
    _register(config, earlier.path)

    assert sync_installed_after_rollback(
        str(config), str(workspace), name="paperless", inst=inst, was=current.path
    )

    recorded = load_installed(str(config))["paperless"]
    assert (recorded.manifest_version, recorded.commit) == ("1.0.0", "a" * 40)
    assert recorded.history == []


def test_a_rollback_that_did_not_move_the_registration_changes_nothing(tmp_path: Path) -> None:
    config, workspace = tmp_path / "config", tmp_path / "workspace"
    current = _staged(workspace, "bbbbbbbbbbbb", "1.1.0", "b" * 40)
    _register(config, current.path)

    assert not sync_installed_after_rollback(
        str(config), str(workspace), name="paperless", inst=_installed("1.1.0"), was=current.path
    )


def test_pruning_after_a_job_keeps_the_registered_and_previous_directory(
    tmp_path: Path,
) -> None:
    config, workspace = tmp_path / "config", tmp_path / "workspace"
    registered = _snapshot(workspace, "bbbbbbbbbbbb", age=10)
    previous = _snapshot(workspace, "aaaaaaaaaaaa", age=9)
    stale = [_snapshot(workspace, f"{c}" * 12, age=i) for i, c in enumerate("cde")]
    _register(config, registered, previous={"path": str(previous), "version": "1.0.0"})

    prune_after(str(config), str(workspace), name="paperless", catalog="cat")

    assert registered.is_dir()
    assert previous.is_dir()
    assert [p.is_dir() for p in stale].count(True) == 1


def test_a_staged_version_is_kept_while_it_is_reviewed(tmp_path: Path) -> None:
    config, workspace = tmp_path / "config", tmp_path / "workspace"
    registered = _snapshot(workspace, "bbbbbbbbbbbb", age=10)
    candidate = _snapshot(workspace, "cccccccccccc", age=1)
    _register(config, registered)

    prune_after(str(config), str(workspace), name="paperless", catalog="cat", extra=candidate)

    assert candidate.is_dir()


# ── The way back ────────────────────────────────────────────────────────────


def test_there_is_nothing_to_go_back_to_without_a_previous_entry(tmp_path: Path) -> None:
    assert previous_version(str(tmp_path), str(tmp_path), {"name": "paperless"}) is None
    assert previous_version(str(tmp_path), str(tmp_path), None) is None


def test_the_previous_version_says_what_of_it_is_left(tmp_path: Path) -> None:
    config, workspace = tmp_path / "config", tmp_path / "workspace"
    path = _snapshot(workspace, "aaaaaaaaaaaa")
    entry = {
        "previous": {"path": str(path), "version": "1.0.0", "restore_point": "2026-10-25_10-00-00"}
    }

    found = previous_version(str(config), str(workspace), entry)

    assert found is not None
    assert (found.version, found.available) == ("1.0.0", True)
    assert found.restore_point == "2026-10-25_10-00-00"
    # No backup directory is configured, so the restore point cannot be found.
    assert found.restore_point_available is False


def test_the_restore_point_is_available_while_the_catalogue_lists_it(tmp_path: Path) -> None:
    config, workspace = tmp_path / "config", tmp_path / "workspace"
    backups = tmp_path / "backups"
    backups.mkdir()
    (backups / "backup.yaml").write_text(
        yaml.safe_dump({"backups": [{"id": "2026-10-25_10-00-00", "result": "ok"}]}),
        encoding="utf-8",
    )
    config.mkdir()
    (config / ".env").write_text(f"PAPAIA_BACKUP_DIR={backups}\n", encoding="utf-8")
    path = _snapshot(workspace, "aaaaaaaaaaaa")
    entry = {
        "previous": {"path": str(path), "version": "1.0.0", "restore_point": "2026-10-25_10-00-00"}
    }

    found = previous_version(str(config), str(workspace), entry)

    assert found is not None
    assert found.restore_point_available is True


def test_files_that_are_gone_are_reported(tmp_path: Path) -> None:
    entry = {"previous": {"path": str(tmp_path / "gone"), "version": "1.0.0"}}

    found = previous_version(str(tmp_path), str(tmp_path), entry)

    assert found is not None
    assert found.available is False
    assert found.restore_point is None


def test_the_version_state_of_an_old_core_asks_for_a_newer_one(tmp_path: Path) -> None:
    state = version_state(str(tmp_path), str(tmp_path), _installed("1.0.0"), None)

    assert state["updates_supported"] is False
    assert "1.8.0" in state["core_requirement"]
    assert state["previous"] is None


def test_the_version_state_lists_the_history(tmp_path: Path) -> None:
    _core(tmp_path, "cmd_addon_update() {\n}\n")
    inst = _installed("1.1.0")
    inst.history = [HistoryEntry("a" * 40, "1.0.0", datetime(2026, 9, 1, tzinfo=UTC))]

    state = version_state(str(tmp_path), str(tmp_path), inst, None)

    assert state["updates_supported"] is True
    assert state["history"] == [
        {"version": "1.0.0", "commit": "a" * 12, "installed_at": "2026-09-01T00:00:00+00:00"}
    ]
