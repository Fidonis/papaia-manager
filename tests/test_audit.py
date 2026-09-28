"""Unit tests for the audit log's read, filter, export and prune logic.

Portable and self-contained: only `tmp_path`, no app, no settings, no network.
The write path (`write_audit_entry`) and the read/prune side added by M1 are
exercised together, since the prune tests in particular need entries with a
specific timestamp `write_audit_entry` itself cannot produce.
"""
from __future__ import annotations

import json
import os
import stat
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.core.audit import (
    AuditFilter,
    audit_path,
    build_filter,
    csv_safe,
    iter_entries,
    parse_cutoff,
    prune_before,
    query_entries,
    redact_params,
    write_audit_entry,
)


def _write_raw(config_dir: Path, *lines: dict[str, Any] | str) -> None:
    """Append entries directly, bypassing `write_audit_entry`.

    Needed for anything that wants a specific timestamp or a line that is not
    valid JSON at all -- `write_audit_entry` always stamps "now".
    """
    path = audit_path(str(config_dir))
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for line in lines:
            f.write((json.dumps(line) if isinstance(line, dict) else line) + "\n")


def _entry(ts: str, **overrides: Any) -> dict[str, Any]:
    base = {"ts": ts, "user": "tester", "action": "install", "target": "n8n", "result": "ok"}
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Round-trip and ordering
# ---------------------------------------------------------------------------


def test_a_written_entry_round_trips(tmp_path: Path) -> None:
    write_audit_entry(
        str(tmp_path), user="tester", action="install", target="n8n", params={"catalog": "fidonis"}
    )
    page = query_entries(str(tmp_path), AuditFilter(), limit=10, offset=0)
    assert page.total == 1
    entry = page.entries[0]
    assert entry.user == "tester"
    assert (entry.action, entry.target, entry.result) == ("install", "n8n", "ok")
    assert entry.params == {"catalog": "fidonis"}
    assert entry.job_id is None


def test_entries_come_back_newest_first(tmp_path: Path) -> None:
    _write_raw(
        tmp_path,
        _entry("2026-01-01T00:00:00+00:00", target="a"),
        _entry("2026-01-03T00:00:00+00:00", target="c"),
        _entry("2026-01-02T00:00:00+00:00", target="b"),
    )
    page = query_entries(str(tmp_path), AuditFilter(), limit=10, offset=0)
    assert [e.target for e in page.entries] == ["c", "b", "a"]


def test_querying_a_missing_log_is_an_empty_page(tmp_path: Path) -> None:
    page = query_entries(str(tmp_path), AuditFilter(), limit=10, offset=0)
    assert page.total == 0
    assert page.entries == []
    assert page.corrupt_lines == 0
    assert page.facets == {"user": [], "action": [], "result": []}


# ---------------------------------------------------------------------------
# Filters, individually and combined
# ---------------------------------------------------------------------------


@pytest.fixture
def mixed_log(tmp_path: Path) -> Path:
    _write_raw(
        tmp_path,
        _entry("2026-01-01T00:00:00+00:00", user="alice", action="install", target="n8n"),
        _entry(
            "2026-01-02T00:00:00+00:00", user="bob", action="stop", target="paperless",
            result="error",
        ),
        _entry("2026-01-03T00:00:00+00:00", user="alice", action="uninstall", target="n8n"),
    )
    return tmp_path


def test_user_filter_is_a_case_insensitive_substring(mixed_log: Path) -> None:
    page = query_entries(str(mixed_log), AuditFilter(user="ALI"), limit=10, offset=0)
    assert {e.user for e in page.entries} == {"alice"}
    assert page.total == 2


def test_action_filter_is_a_case_insensitive_substring(mixed_log: Path) -> None:
    page = query_entries(str(mixed_log), AuditFilter(action="install"), limit=10, offset=0)
    # Substring, not exact match: "install" also matches "uninstall".
    assert {e.action for e in page.entries} == {"install", "uninstall"}


def test_result_filter(mixed_log: Path) -> None:
    page = query_entries(str(mixed_log), AuditFilter(result="error"), limit=10, offset=0)
    assert [e.user for e in page.entries] == ["bob"]


def test_target_filter(mixed_log: Path) -> None:
    page = query_entries(str(mixed_log), AuditFilter(target="paper"), limit=10, offset=0)
    assert [e.target for e in page.entries] == ["paperless"]


def test_since_is_inclusive(mixed_log: Path) -> None:
    since = datetime(2026, 1, 2, tzinfo=UTC)
    page = query_entries(str(mixed_log), AuditFilter(since=since), limit=10, offset=0)
    assert page.total == 2


def test_before_is_exclusive(mixed_log: Path) -> None:
    before = datetime(2026, 1, 2, tzinfo=UTC)
    page = query_entries(str(mixed_log), AuditFilter(before=before), limit=10, offset=0)
    assert page.total == 1


def test_filters_combine_with_and(mixed_log: Path) -> None:
    page = query_entries(
        str(mixed_log), AuditFilter(user="alice", target="n8n", result="ok"), limit=10, offset=0
    )
    assert page.total == 2
    page = query_entries(
        str(mixed_log), AuditFilter(user="alice", action="stop"), limit=10, offset=0
    )
    assert page.total == 0


def test_facets_reflect_the_unfiltered_log(mixed_log: Path) -> None:
    page = query_entries(str(mixed_log), AuditFilter(user="alice"), limit=10, offset=0)
    assert page.facets == {
        "user": ["alice", "bob"],
        "action": ["install", "stop", "uninstall"],
        "result": ["error", "ok"],
    }


def test_pagination(mixed_log: Path) -> None:
    first = query_entries(str(mixed_log), AuditFilter(), limit=2, offset=0)
    second = query_entries(str(mixed_log), AuditFilter(), limit=2, offset=2)
    assert len(first.entries) == 2
    assert len(second.entries) == 1
    assert first.total == second.total == 3


def test_build_filter_matches_query_entries(mixed_log: Path) -> None:
    flt = build_filter(user="alice", since="2026-01-02")
    page = query_entries(str(mixed_log), flt, limit=10, offset=0)
    assert page.total == 1


def test_iter_entries_yields_newest_first(mixed_log: Path) -> None:
    assert [e.target for e in iter_entries(str(mixed_log), AuditFilter())] == [
        "n8n", "paperless", "n8n",
    ]


# ---------------------------------------------------------------------------
# Corrupt lines
# ---------------------------------------------------------------------------


def test_unreadable_lines_are_counted_and_excluded(tmp_path: Path) -> None:
    _write_raw(
        tmp_path,
        _entry("2026-01-01T00:00:00+00:00"),
        "{not json",
        json.dumps({"user": "tester"}),  # missing required fields
    )
    page = query_entries(str(tmp_path), AuditFilter(), limit=10, offset=0)
    assert page.total == 1
    assert page.corrupt_lines == 2


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_redact_params_masks_secret_keys() -> None:
    out = redact_params({"password": "hunter2", "note": "fine", "API_KEY": "abc"})
    assert out == {"password": "***", "note": "fine", "API_KEY": "***"}


def test_redact_params_masks_nested_secrets() -> None:
    out = redact_params({"auth": {"token": "abc", "username": "bob"}})
    assert out == {"auth": {"token": "***", "username": "bob"}}


def test_redact_params_masks_url_userinfo() -> None:
    out = redact_params({"url": "https://x-access-token:ghp_abc@github.com/acme/repo.git"})
    assert out == {"url": "https://***@github.com/acme/repo.git"}


def test_redact_params_leaves_a_plain_url_alone() -> None:
    out = redact_params({"url": "https://github.com/acme/repo.git"})
    assert out == {"url": "https://github.com/acme/repo.git"}


def test_query_entries_redacts_params_even_if_the_writer_did_not(tmp_path: Path) -> None:
    """Defense in depth: a hand-edited or pre-feature line still comes out clean."""
    _write_raw(
        tmp_path,
        _entry("2026-01-01T00:00:00+00:00", params={"token": "leaked-secret"}),
    )
    page = query_entries(str(tmp_path), AuditFilter(), limit=10, offset=0)
    assert page.entries[0].params == {"token": "***"}


# ---------------------------------------------------------------------------
# csv_safe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("cell", ["=cmd()", "+1", "-1", "@SUM(A1)", "\ttab", "\rcr"])
def test_csv_safe_prefixes_formula_looking_cells(cell: str) -> None:
    assert csv_safe(cell) == "'" + cell


@pytest.mark.parametrize("cell", ["ok", "n8n", "user@example.com", ""])
def test_csv_safe_leaves_ordinary_cells_alone(cell: str) -> None:
    assert csv_safe(cell) == cell


# ---------------------------------------------------------------------------
# parse_cutoff
# ---------------------------------------------------------------------------


def test_parse_cutoff_treats_a_bare_date_as_midnight_utc() -> None:
    assert parse_cutoff("2026-01-01") == datetime(2026, 1, 1, tzinfo=UTC)


def test_parse_cutoff_converts_an_offset_datetime_to_utc() -> None:
    assert parse_cutoff("2026-01-01T10:00:00+02:00") == datetime(2026, 1, 1, 8, tzinfo=UTC)


def test_parse_cutoff_assumes_utc_for_a_naive_datetime() -> None:
    assert parse_cutoff("2026-01-01T10:00:00") == datetime(2026, 1, 1, 10, tzinfo=UTC)


def test_parse_cutoff_rejects_garbage() -> None:
    with pytest.raises(ValueError, match="not a valid date"):
        parse_cutoff("not-a-date")


# ---------------------------------------------------------------------------
# prune_before
# ---------------------------------------------------------------------------


def test_prune_removes_only_strictly_older_entries(tmp_path: Path) -> None:
    _write_raw(
        tmp_path,
        _entry("2025-12-31T23:59:59+00:00", target="old"),
        _entry("2026-01-01T00:00:00+00:00", target="on-cutoff"),
        _entry("2026-01-01T00:00:01+00:00", target="new"),
    )
    result = prune_before(str(tmp_path), datetime(2026, 1, 1, tzinfo=UTC))
    assert (result.removed, result.kept) == (1, 2)
    remaining = query_entries(str(tmp_path), AuditFilter(), limit=10, offset=0)
    assert {e.target for e in remaining.entries} == {"on-cutoff", "new"}


def test_prune_never_deletes_an_unreadable_line(tmp_path: Path) -> None:
    _write_raw(tmp_path, _entry("2020-01-01T00:00:00+00:00"), "{not json")
    result = prune_before(str(tmp_path), datetime(2026, 1, 1, tzinfo=UTC))
    assert result.removed == 1
    assert result.kept == 1
    lines = audit_path(str(tmp_path)).read_text(encoding="utf-8").splitlines()
    assert lines == ["{not json"]


def test_prune_keeps_a_line_with_no_parsable_ts(tmp_path: Path) -> None:
    _write_raw(tmp_path, _entry("not-a-timestamp"))
    result = prune_before(str(tmp_path), datetime(2026, 1, 1, tzinfo=UTC))
    assert result.removed == 0
    assert result.kept == 1


def test_dry_run_reports_but_does_not_change_the_file(tmp_path: Path) -> None:
    _write_raw(tmp_path, _entry("2020-01-01T00:00:00+00:00"))
    path = audit_path(str(tmp_path))
    before_mtime = path.stat().st_mtime_ns
    result = prune_before(str(tmp_path), datetime(2026, 1, 1, tzinfo=UTC), dry_run=True)
    assert result.removed == 1
    assert path.stat().st_mtime_ns == before_mtime
    assert query_entries(str(tmp_path), AuditFilter(), limit=10, offset=0).total == 1


def test_a_zero_hit_prune_leaves_the_file_untouched(tmp_path: Path) -> None:
    _write_raw(tmp_path, _entry("2026-06-01T00:00:00+00:00"))
    path = audit_path(str(tmp_path))
    before_stat = path.stat()
    result = prune_before(str(tmp_path), datetime(2020, 1, 1, tzinfo=UTC))
    assert result.removed == 0
    after_stat = path.stat()
    assert before_stat.st_mtime_ns == after_stat.st_mtime_ns
    assert before_stat.st_ino == after_stat.st_ino


def test_a_replace_failure_leaves_the_original_intact_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_raw(tmp_path, _entry("2020-01-01T00:00:00+00:00"))
    path = audit_path(str(tmp_path))
    original = path.read_text(encoding="utf-8")

    def _boom(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("app.core.audit.os.replace", _boom)

    with pytest.raises(OSError, match="disk full"):
        prune_before(str(tmp_path), datetime(2026, 1, 1, tzinfo=UTC))

    assert path.read_text(encoding="utf-8") == original
    assert not path.with_name(path.name + ".prune.tmp").exists()


def test_file_mode_survives_a_prune(tmp_path: Path) -> None:
    _write_raw(
        tmp_path,
        _entry("2020-01-01T00:00:00+00:00", target="old"),
        _entry("2026-06-01T00:00:00+00:00", target="new"),
    )
    path = audit_path(str(tmp_path))
    os.chmod(path, 0o640)
    before_mode = stat.S_IMODE(path.stat().st_mode)

    prune_before(str(tmp_path), datetime(2026, 1, 1, tzinfo=UTC))

    assert stat.S_IMODE(path.stat().st_mode) == before_mode


def test_concurrent_writes_during_a_prune_lose_no_entry(tmp_path: Path) -> None:
    _write_raw(tmp_path, _entry("2020-01-01T00:00:00+00:00", target="old"))
    errors: list[BaseException] = []

    def _writer(i: int) -> None:
        try:
            write_audit_entry(str(tmp_path), user=f"u{i}", action="install", target="n8n")
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    writers = [threading.Thread(target=_writer, args=(i,)) for i in range(20)]
    pruner = threading.Thread(
        target=lambda: prune_before(str(tmp_path), datetime(2021, 1, 1, tzinfo=UTC))
    )

    for t in writers:
        t.start()
    pruner.start()
    for t in writers:
        t.join()
    pruner.join()

    assert not errors
    page = query_entries(str(tmp_path), AuditFilter(), limit=100, offset=0)
    # The old entry predates the cutoff and is always removed; every writer's
    # entry is stamped "now" (2026+) and is never a candidate, whichever side
    # of the prune it lands on -- the shared lock rules out landing inside it.
    assert page.total == 20
    assert page.corrupt_lines == 0
    assert "old" not in {e.target for e in page.entries}
