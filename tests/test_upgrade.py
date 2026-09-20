"""Parsing what the core says about an upgrade, and what its runner reports.

Nothing here forks a process or touches a network. Every function under test
turns output the core produced into something the page can render, and the whole
value of that layer is that it keeps saying the same thing when the core changes
wording it did not promise to keep. So the fixtures are verbatim samples of what
papaia-ctl and `lib.cli` actually emit.
"""
from __future__ import annotations

import os
import subprocess
import tempfile
from pathlib import Path

import pytest

from app.core import upgrade
from app.core.upgrade import (
    PHASE_DONE,
    PHASE_FAILED,
    PHASE_PENDING,
    PHASE_RUNNING,
    PHASE_SKIPPED,
    UpgradeError,
    current_version,
    fetch_hint,
    fetch_tags,
    https_equivalent,
    is_valid_target_version,
    newer_than,
    origin_url,
    parse_gate_json,
    parse_plan_tsv,
    parse_resolve_tsv,
    parse_tags,
    phases_from_log,
    read_upgrade_log,
    recovery_from_log,
    synthetic_recovery,
)

_RESOLVE = "CURRENT\t1.0.0\nTARGET\t1.2.0\nTAG\tv1.2.0\nSTATUS\tok\n"

_PLAN = (
    "MIGRATION\t1.1.0__split_litellm_env\t1.1.0\t/w/tools/migrations/a.sh\tsh\n"
    "MIGRATION\t1.2.0__qdrant_rename\t1.2.0\t/w/tools/migrations/b.py\tpy\n"
)


# ---------------------------------------------------------------------------
# upgrade-resolve
# ---------------------------------------------------------------------------


def test_the_resolve_output_is_read_as_four_fields() -> None:
    assert parse_resolve_tsv(_RESOLVE) == {
        "CURRENT": "1.0.0",
        "TARGET": "1.2.0",
        "TAG": "v1.2.0",
        "STATUS": "ok",
    }


def test_a_crlf_resolve_output_parses_the_same() -> None:
    # The shell pipes this through `tr -d '\r'` because the Windows interpreter
    # emits CRLF, and a tag name ending in CR matches no ref. The same output
    # reaches this parser, so it strips the same character.
    assert parse_resolve_tsv(_RESOLVE.replace("\n", "\r\n"))["TAG"] == "v1.2.0"


def test_up_to_date_is_a_status_not_an_absence() -> None:
    parsed = parse_resolve_tsv("CURRENT\t1.2.0\nTARGET\t1.2.0\nTAG\tv1.2.0\nSTATUS\tup-to-date\n")
    assert parsed["STATUS"] == "up-to-date"


def test_unknown_keys_are_ignored() -> None:
    # A newer core adding a line must not make the parser raise on a page that
    # only needs the four it knows.
    assert "WHATEVER" not in parse_resolve_tsv(_RESOLVE + "WHATEVER\tx\n")


# ---------------------------------------------------------------------------
# upgrade-plan
# ---------------------------------------------------------------------------


def test_the_migration_plan_keeps_its_order() -> None:
    plan = parse_plan_tsv(_PLAN)
    assert [m.id for m in plan] == ["1.1.0__split_litellm_env", "1.2.0__qdrant_rename"]
    assert [m.version for m in plan] == ["1.1.0", "1.2.0"]
    assert [m.kind for m in plan] == ["sh", "py"]


def test_non_migration_lines_are_skipped() -> None:
    assert parse_plan_tsv("something else\n" + _PLAN) != []
    assert len(parse_plan_tsv("something else\n" + _PLAN)) == 2


def test_an_empty_plan_is_no_migrations() -> None:
    assert parse_plan_tsv("") == []


# ---------------------------------------------------------------------------
# addon-check --json
# ---------------------------------------------------------------------------


def test_a_passing_gate_is_read_from_the_exit_code() -> None:
    gate = parse_gate_json('[{"name": "paperless", "status": "OK"}]', exit_code=0)
    assert gate.passed
    assert not gate.blocking
    assert not gate.forceable


def test_an_incompatible_gate_is_forceable() -> None:
    # papaia-ctl degrades INCOMPATIBLE to a warning under --force, so the page
    # may offer the override here.
    gate = parse_gate_json(
        '[{"name": "n8n", "status": "INCOMPATIBLE", "reason": "needs addon api 3"}]',
        exit_code=2,
    )
    assert not gate.passed
    assert gate.forceable
    assert [r.name for r in gate.blocking] == ["n8n"]


def test_an_error_gate_is_not_forceable() -> None:
    # `compat.gate` returns 2 for an ERROR whatever the flag says, so offering
    # --force there would promise something the CLI cannot deliver.
    gate = parse_gate_json(
        '[{"name": "broken", "status": "ERROR", "reason": "manifest unreadable"}]',
        exit_code=2,
    )
    assert gate.has_error
    assert not gate.forceable


def test_a_deployment_without_addons_passes() -> None:
    gate = parse_gate_json("[]", exit_code=0)
    assert gate.passed
    assert gate.results == ()


def test_a_list_requirement_is_flattened_for_display() -> None:
    gate = parse_gate_json(
        '[{"name": "a", "status": "OK", "requirement": [1, 2], "core_value": null}]',
        exit_code=0,
    )
    assert gate.results[0].requirement == "1,2"
    assert gate.results[0].core_value == ""


# ---------------------------------------------------------------------------
# Tags and versions
# ---------------------------------------------------------------------------


def test_only_stable_release_tags_are_offered() -> None:
    # A pre-release is never picked implicitly by papaia-ctl either -- the only
    # way onto one is naming it, which this page does not do.
    tags = parse_tags(["v0.8.0", "v1.0.0", "v1.2.0", "v1.3.0-rc.1", "nightly", "1.1.0"])
    assert tags == ["1.2.0", "1.0.0", "0.8.0"]


def test_tags_sort_by_number_not_by_string() -> None:
    assert parse_tags(["v1.9.0", "v1.10.0"])[0] == "1.10.0"


def test_only_newer_releases_are_selectable() -> None:
    assert newer_than(["1.2.0", "1.1.0", "1.0.0", "0.8.0"], "1.0.0") == ["1.2.0", "1.1.0"]


def test_an_unparseable_current_version_offers_everything() -> None:
    # Better to show the releases and let papaia-ctl refuse the move than to
    # hide them because the bundle carries something unexpected.
    assert newer_than(["1.2.0"], "0.0.0-dev") == ["1.2.0"]


@pytest.mark.parametrize("value", ["1.2.0", "0.0.1", "10.20.30"])
def test_release_versions_are_accepted(value: str) -> None:
    assert is_valid_target_version(value)


@pytest.mark.parametrize("value", ["", "v1.2.0", "1.2", "1.2.0-rc.1", "--force", "1.2.0 "])
def test_anything_else_is_not_a_release_version(value: str) -> None:
    assert not is_valid_target_version(value)


# ---------------------------------------------------------------------------
# Versions on disk
# ---------------------------------------------------------------------------


def _dirs(deployment: str | None, version: str | None) -> tuple[str, str]:
    config = tempfile.mkdtemp(prefix="papaia-config-")
    workspace = tempfile.mkdtemp(prefix="papaia-workspace-")
    if deployment is not None:
        Path(config, "deployment.yaml").write_text(deployment, encoding="utf-8")
    if version is not None:
        papaia = Path(workspace, "papaia")
        papaia.mkdir()
        (papaia / "VERSION").write_text(version, encoding="utf-8")
    return config, workspace


def test_the_recorded_version_wins_over_the_checkout() -> None:
    # The bundle is what the migrations start from, which is why papaia-ctl
    # prefers it too: an operator who moved the checkout by hand still has a
    # configuration shaped like the old release.
    config, workspace = _dirs("platform_version: 1.0.0\n", "1.2.0\n")
    state = current_version(config, workspace)
    assert state.current == "1.0.0"
    assert state.mismatch


def test_matching_versions_are_not_a_mismatch() -> None:
    config, workspace = _dirs("platform_version: 1.2.0\n", "1.2.0\n")
    assert not current_version(config, workspace).mismatch


def test_the_checkout_is_the_fallback() -> None:
    config, workspace = _dirs(None, "1.2.0\n")
    state = current_version(config, workspace)
    assert state.current == "1.2.0"
    assert not state.mismatch


def test_neither_source_is_an_empty_answer_not_a_crash() -> None:
    config, workspace = _dirs(None, None)
    assert current_version(config, workspace).current == ""


# ---------------------------------------------------------------------------
# upgrade.log
# ---------------------------------------------------------------------------


# Assembled rather than written as one block only to keep the source lines
# short: the value is byte-for-byte what `_upgrade_log` appends.
_LOG = "".join(
    line + "\n"
    for line in (
        "2026-06-14T08:22:10Z upgrade from=0.8.0 to=1.0.0 result=ok"
        " migrations=1 restore_point=2026-06-14_10-21-55",
        "2026-09-01T09:00:00Z upgrade from=1.0.0 to=1.2.0 result=checkout"
        " restore_point=2026-09-01_10-59-12",
        "2026-09-01T09:04:31Z upgrade from=1.0.0 to=1.2.0 result=failed"
        " stage=migration id=1.2.0__qdrant_rename",
        "this line is not an upgrade record at all",
        "2026-09-02T11:00:00Z upgrade from=1.0.0 to=1.1.0 result=ok"
        " migrations=1 restore_point=none",
    )
)


def _log_dir() -> str:
    config = tempfile.mkdtemp(prefix="papaia-config-")
    Path(config, "upgrade.log").write_text(_LOG, encoding="utf-8")
    return config


def test_the_history_is_newest_first() -> None:
    entries = read_upgrade_log(_log_dir())
    assert [e.at for e in entries][0] == "2026-09-02T11:00:00Z"
    assert len(entries) == 4


def test_a_garbage_line_is_skipped_rather_than_raised() -> None:
    # A log written by a newer core must not take the page down.
    assert all(e.result for e in read_upgrade_log(_log_dir()))


def test_the_failure_stage_is_read_out_of_the_details() -> None:
    failed = next(e for e in read_upgrade_log(_log_dir()) if e.result == "failed")
    assert failed.stage == "migration"
    assert failed.details["id"] == "1.2.0__qdrant_rename"


def test_a_restore_point_of_none_is_no_restore_point() -> None:
    # `--no-backup` writes the literal string, and linking it would produce a
    # dead link into the backup page.
    entry = next(e for e in read_upgrade_log(_log_dir()) if e.to_version == "1.1.0")
    assert entry.restore_point == ""


def test_a_recorded_restore_point_is_carried_through() -> None:
    entry = next(e for e in read_upgrade_log(_log_dir()) if e.result == "checkout")
    assert entry.restore_point == "2026-09-01_10-59-12"


def test_an_installation_that_never_upgraded_has_no_history() -> None:
    assert read_upgrade_log(tempfile.mkdtemp(prefix="papaia-config-")) == []


def test_the_history_is_capped() -> None:
    assert len(read_upgrade_log(_log_dir(), limit=2)) == 2


# ---------------------------------------------------------------------------
# Phases, read out of the runner's output
# ---------------------------------------------------------------------------


_RUNNING_LOG = """[papaia-ctl] Creating a restore point before the upgrade...
[ok] backup complete: 2026-09-02_14-03-19
[papaia-ctl] Stopping and removing the containers (volumes are kept)...
[papaia-ctl] Moving the checkout to v1.2.0...
[papaia-ctl] Running 1.2.0's papaia-ctl from here on.
[papaia-ctl] Running 2 migration(s)...
[papaia-ctl]   1.1.0__split_litellm_env
"""

_COMPLETE_LOG = _RUNNING_LOG + (
    "[papaia-ctl] Applying 1.2.0's configuration to /srv/papaia/config...\n"
    "[ok] setup complete (re-render only).\n"
    "[papaia-ctl] Starting the stack...\n"
    "[ok] start complete.\n"
    "[ok] upgrade complete: 1.0.0 -> 1.2.0\n"
)

_FAILED_LOG = _RUNNING_LOG + (
    "[error] Migration 1.2.0__qdrant_rename failed. The upgrade stops here.\n"
    "[error]\n"
    "[error] The checkout is on v1.2.0 and the stack is stopped.\n"
    "[error]\n"
    "[error] To go back to 1.0.0:\n"
    "[error]     git -C /srv/papaia/papaia checkout v1.0.0\n"
    "[error]     papaia-ctl restore --restore-point=2026-09-02_14-03-19"
    " --config-dir=/srv/papaia/config\n"
)


def _states(log: str, *, running: bool) -> dict[str, str]:
    return {p.key: p.state for p in phases_from_log(log, running=running)}


def test_a_running_upgrade_marks_the_announced_phase() -> None:
    states = _states(_RUNNING_LOG, running=True)
    assert states["backup"] == PHASE_DONE
    assert states["checkout"] == PHASE_DONE
    assert states["migrations"] == PHASE_RUNNING
    assert states["render"] == PHASE_PENDING
    assert states["start"] == PHASE_PENDING


def test_a_finished_upgrade_marks_everything_done() -> None:
    states = _states(_COMPLETE_LOG, running=False)
    assert set(states.values()) == {PHASE_DONE}


def test_a_stopped_run_marks_the_last_phase_failed() -> None:
    # The container exited without ever printing "upgrade complete", so the
    # phase it was in is where it stopped.
    states = _states(_FAILED_LOG, running=False)
    assert states["migrations"] == PHASE_FAILED
    assert states["render"] == PHASE_PENDING


def test_a_phase_that_never_ran_is_skipped_not_pending() -> None:
    # --no-backup produces exactly this: leaving a spinner on a step that will
    # never run would read as a stall.
    log = _RUNNING_LOG.split("\n", 2)[2]
    assert _states(log, running=True)["backup"] == PHASE_SKIPPED


def test_no_migrations_still_counts_as_that_phase() -> None:
    # "No migrations to run." replaces the "Running N migration(s)" line, and a
    # release without migrations must not look stuck on the one before it.
    log = (
        _RUNNING_LOG.rsplit("[papaia-ctl] Running 2", 1)[0]
        + "[papaia-ctl] No migrations to run.\n"
    )
    phases = {p.key: p for p in phases_from_log(log, running=True)}
    assert phases["migrations"].state == PHASE_RUNNING
    assert phases["migrations"].detail == "none in this release"


def test_an_empty_log_is_all_pending() -> None:
    assert set(_states("", running=True).values()) == {PHASE_PENDING}


def test_the_checkout_phase_carries_the_tag() -> None:
    phases = {p.key: p for p in phases_from_log(_RUNNING_LOG, running=True)}
    assert phases["checkout"].detail == "v1.2.0"


# ---------------------------------------------------------------------------
# The way back
# ---------------------------------------------------------------------------


def test_the_recovery_block_is_taken_verbatim() -> None:
    recovery = recovery_from_log(_FAILED_LOG)
    assert recovery.startswith("The checkout is on v1.2.0")
    assert "git -C /srv/papaia/papaia checkout v1.0.0" in recovery
    assert "--restore-point=2026-09-02_14-03-19" in recovery


def test_the_error_prefix_is_stripped_from_the_recovery_block() -> None:
    # It is a shell log prefix, not part of the command an operator pastes.
    assert "[error]" not in recovery_from_log(_FAILED_LOG)


def test_a_successful_run_has_no_recovery_block() -> None:
    assert recovery_from_log(_COMPLETE_LOG) == ""


# ---------------------------------------------------------------------------
# The way back, rebuilt when the hand-off ate the shell
# ---------------------------------------------------------------------------


# The run gets as far as moving the checkout and then the container exits: the
# phase-1 -> phase-2 `exec` failed, so phase 2 never prints and neither does
# `_upgrade_failed`. This is byte-for-byte what such a log looks like.
_HANDOFF_ABORT_LOG = """[papaia-ctl] Stopping and removing the containers (volumes are kept)...
[ok] stop complete.
[papaia-ctl] Moving the checkout to v1.1.0...
"""


def test_a_run_that_dies_at_the_hand_off_carries_no_recovery_block() -> None:
    assert recovery_from_log(_HANDOFF_ABORT_LOG) == ""


def test_synthetic_recovery_rebuilds_a_way_back_from_the_versions() -> None:
    config, workspace = _dirs("platform_version: 1.0.0\n", "1.1.0\n")
    text = synthetic_recovery(
        current_version(config, workspace),
        workspace_dir=workspace,
        config_dir=config,
        target="1.1.0",
        exit_code=126,
    )
    assert "exit 126" in text
    assert f"git -C {Path(workspace, 'papaia')} checkout v1.0.0" in text
    assert "start --addons" in text
    assert "upgrade --version=1.1.0" in text
    assert "restore --restore-point" not in text


def test_synthetic_recovery_prefers_a_restore_point_when_one_exists() -> None:
    config, workspace = _dirs("platform_version: 1.0.0\n", "1.1.0\n")
    text = synthetic_recovery(
        current_version(config, workspace),
        workspace_dir=workspace,
        config_dir=config,
        target="1.1.0",
        restore_point="2026-09-01_10-59-12",
        exit_code=126,
    )
    assert "restore --restore-point=2026-09-01_10-59-12" in text
    assert "start --addons" not in text


def test_synthetic_recovery_is_empty_once_the_versions_agree() -> None:
    # After a successful re-run the bundle records the target too; the block
    # must disappear rather than linger on the acknowledged runner.
    config, workspace = _dirs("platform_version: 1.1.0\n", "1.1.0\n")
    assert (
        synthetic_recovery(
            current_version(config, workspace),
            workspace_dir=workspace,
            config_dir=config,
            target="1.1.0",
        )
        == ""
    )


# ---------------------------------------------------------------------------
# The tag fetch
# ---------------------------------------------------------------------------
#
# An SSH `origin` cannot be fetched from inside the manager container -- there
# is no SSH client, key or known_hosts -- and used to leave a checkout cloned as
# `git@github.com:owner/repo.git` blind to every release published after it.


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("git@github.com:Fidonis/papaia.git", "https://github.com/Fidonis/papaia.git"),
        ("git@git.example.org:team/sub/papaia.git", "https://git.example.org/team/sub/papaia.git"),
        ("git@github.com:/Fidonis/papaia.git", "https://github.com/Fidonis/papaia.git"),
        ("  git@github.com:Fidonis/papaia.git\n", "https://github.com/Fidonis/papaia.git"),
        ("ssh://git@github.com/Fidonis/papaia.git", "https://github.com/Fidonis/papaia.git"),
        (
            "ssh://git@git.example.org:2222/team/papaia.git",
            "https://git.example.org/team/papaia.git",
        ),
        ("ssh://github.com/Fidonis/papaia.git", "https://github.com/Fidonis/papaia.git"),
    ],
)
def test_an_ssh_origin_has_an_https_equivalent(url: str, expected: str) -> None:
    assert https_equivalent(url) == expected


@pytest.mark.parametrize(
    "url",
    [
        "https://github.com/Fidonis/papaia.git",
        "http://git.example.org/papaia.git",
        "file:///srv/git/papaia.git",
        "/srv/git/papaia.git",
        "C:/srv/git/papaia.git",
        "../papaia.git",
        "",
        "   ",
    ],
)
def test_anything_that_is_not_ssh_has_no_equivalent(url: str) -> None:
    # None means "use origin as it is": there is nothing to translate, and a
    # rewritten URL for a local path would send the fetch somewhere else.
    assert https_equivalent(url) is None


class _FakeGit:
    """Stands in for `_run_git`: records every call, answers `remote get-url`."""

    def __init__(self, origin: str, *, fetch_error: str = "") -> None:
        self.origin = origin
        self.fetch_error = fetch_error
        self.calls: list[tuple[str, ...]] = []

    async def __call__(self, repo: Path, args: tuple[str, ...], limit: float) -> str:
        self.calls.append(args)
        if args[:2] == ("remote", "get-url"):
            return self.origin + "\n"
        if args[0] == "fetch" and self.fetch_error:
            raise UpgradeError(f"git fetch failed: {self.fetch_error}")
        return ""

    @property
    def fetches(self) -> list[tuple[str, ...]]:
        return [c for c in self.calls if c[0] == "fetch"]


async def test_an_ssh_origin_is_fetched_over_https(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGit("git@github.com:Fidonis/papaia.git")
    monkeypatch.setattr(upgrade, "_run_git", fake)

    assert await fetch_tags("/w") == ""

    assert fake.fetches == [("fetch", "--tags", "--quiet", "https://github.com/Fidonis/papaia.git")]
    # The checkout's configuration is the operator's; nothing here may write it.
    assert not any(c[0] == "remote" and c[1] != "get-url" for c in fake.calls)
    assert not any(c[0] == "config" for c in fake.calls)


async def test_an_https_origin_is_fetched_as_origin(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGit("https://github.com/Fidonis/papaia.git")
    monkeypatch.setattr(upgrade, "_run_git", fake)

    assert await fetch_tags("/w") == ""

    assert fake.fetches == [("fetch", "--tags", "--quiet", "origin")]


async def test_a_failed_fetch_is_returned_not_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = _FakeGit(
        "git@github.com:Fidonis/papaia.git",
        fetch_error="fatal: could not read Username for 'https://github.com'",
    )
    monkeypatch.setattr(upgrade, "_run_git", fake)

    error = await fetch_tags("/w")

    assert error.startswith("git fetch failed:")
    assert "could not read Username" in error


async def test_a_checkout_without_an_origin_falls_through_to_origin(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def no_origin(repo: Path, args: tuple[str, ...], limit: float) -> str:
        if args[:2] == ("remote", "get-url"):
            raise UpgradeError("git remote failed: error: No such remote 'origin'")
        raise UpgradeError("git fetch failed: fatal: 'origin' does not appear to be a repository")

    monkeypatch.setattr(upgrade, "_run_git", no_origin)

    assert await origin_url("/w") == ""
    assert "does not appear to be a repository" in await fetch_tags("/w")


def _run(cwd: Path, *args: str) -> None:
    subprocess.run(  # noqa: S603
        ["git", *args],  # noqa: S607
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.org",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.org",
        },
    )


async def test_a_release_published_after_the_clone_becomes_visible(tmp_path: Path) -> None:
    # Real git, no network: a bare repository stands in for the remote.
    remote = tmp_path / "remote.git"
    seed = tmp_path / "seed"
    workspace = tmp_path / "ws"
    seed.mkdir()
    workspace.mkdir()
    _run(tmp_path, "init", "--bare", "-b", "main", str(remote))
    _run(seed, "init", "-b", "main")
    (seed / "VERSION").write_text("1.1.0\n", encoding="utf-8")
    _run(seed, "add", "VERSION")
    _run(seed, "commit", "-m", "1.1.0")
    _run(seed, "tag", "v1.1.0")
    _run(seed, "push", str(remote), "main", "--tags")
    _run(workspace, "clone", str(remote), "papaia")

    (seed / "VERSION").write_text("1.2.0\n", encoding="utf-8")
    _run(seed, "commit", "-am", "1.2.0")
    _run(seed, "tag", "v1.2.0")
    _run(seed, "push", str(remote), "main", "--tags")

    assert await upgrade.local_tags(str(workspace)) == ["1.1.0"]
    assert await fetch_tags(str(workspace)) == ""
    assert await upgrade.local_tags(str(workspace)) == ["1.2.0", "1.1.0"]


# ---------------------------------------------------------------------------
# The hint
# ---------------------------------------------------------------------------

_REPO = Path("/srv/papaia/workspace/papaia")
_SSH = "git@github.com:Fidonis/papaia.git"
_HTTPS = "https://github.com/Fidonis/papaia.git"


def _hint(error: str, origin: str = _HTTPS) -> str:
    return fetch_hint(error, repo=_REPO, origin=origin, uid=1000, gid=1001)


def test_a_fetch_that_worked_needs_no_hint() -> None:
    assert _hint("") == ""


def test_an_ssh_origin_names_the_host_fetch_and_the_https_switch() -> None:
    text = _hint("git fetch failed: fatal: could not read Username", _SSH)
    assert "SSH URL" in text
    assert f"git -C {_REPO} fetch --tags origin" in text
    assert f"git -C {_REPO} remote set-url origin {_HTTPS}" in text


def test_ssh_key_errors_are_not_read_as_a_permission_problem() -> None:
    text = _hint("git fetch failed: git@github.com: Permission denied (publickey).", _SSH)
    assert "chown" not in text
    assert "SSH" in text


def test_a_permission_error_names_the_owner_the_container_runs_as() -> None:
    text = _hint("git fetch failed: error: cannot open '.git/FETCH_HEAD': Permission denied")
    assert f"chown -R 1000:1001 {_REPO}/.git" in text
    assert f"git -C {_REPO} fetch --tags origin" in text


def test_a_network_error_names_the_host() -> None:
    text = _hint("git fetch failed: fatal: unable to access: Could not resolve host: github.com")
    assert "github.com" in text
    assert "443" in text
    assert "chown" not in text


def test_a_timeout_reads_as_a_network_problem() -> None:
    assert "443" in _hint("git fetch timed out after 60s")


def test_an_ssh_origin_whose_https_fetch_failed_still_gets_the_host_fetch() -> None:
    # A private repository: the HTTPS fallback asks for credentials, which the
    # manager cannot give. The host has the key, so the host fetch is the answer.
    text = _hint("git fetch failed: fatal: could not read Username for 'https://github.com'", _SSH)
    assert f"git -C {_REPO} fetch --tags origin" in text
    assert "remote set-url" in text


def test_an_unrecognised_error_still_names_the_host_fetch() -> None:
    text = _hint("git fetch failed: something new")
    assert "does not recognise" in text
    assert f"git -C {_REPO} fetch --tags origin" in text
    assert "remote set-url" not in text
