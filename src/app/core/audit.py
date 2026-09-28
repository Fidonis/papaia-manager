"""Append-only JSONL audit log, and the read/filter/export/prune side of it."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SECRET_KEY_RE = re.compile(
    r"(secret|password|passwd|token|api[_-]?key|authorization|credential)", re.IGNORECASE
)
# Userinfo of a URL that starts the string, e.g. `https://user:pw@host/...`. Only
# the scheme-anchored form is handled -- catalog URLs are the one place this
# feature puts a credential-bearing URL in front of the log, and they are never
# embedded inside a longer string.
_URL_USERINFO_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/@]+@")

# Guards every append and the prune rewrite against each other. A single
# process writes this file (one uvicorn worker, no `--workers`), so a plain
# lock is enough -- a second process would need `fcntl.flock` instead.
_LOCK = threading.Lock()

# Cells starting with any of these are treated as a formula by Excel/Sheets
# when the export is opened there.
_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")


def audit_path(config_dir: str) -> Path:
    return Path(config_dir) / "manager" / "audit.log"


def write_audit_entry(
    config_dir: str,
    *,
    user: str,
    action: str,
    target: str,
    params: dict[str, Any] | None = None,
    job_id: str | None = None,
    result: str = "ok",
) -> None:
    """Append one entry to the audit log.

    The log lives at ``$PAPAIA_CONFIG_DIR/manager/audit.log`` and is
    append-only. Sensitive values (tokens, secrets) must be redacted by
    the caller before passing them in ``params``.
    """
    path = audit_path(config_dir)
    path.parent.mkdir(parents=True, exist_ok=True)

    entry: dict[str, Any] = {
        "ts": datetime.now(tz=UTC).isoformat(),
        "user": user,
        "action": action,
        "target": target,
        "result": result,
    }
    if params is not None:
        entry["params"] = params
    if job_id is not None:
        entry["job_id"] = job_id

    with _LOCK, path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")


def redact_params(value: Any) -> Any:
    """Recursively mask secret-looking values before they reach the log or a viewer.

    A dict value is replaced with ``***`` when its key looks like a credential
    (token, password, ...); a string that starts with a URL carrying userinfo
    (``https://user:pw@host/...``) has that userinfo masked. Callers pass their
    params through this before ``write_audit_entry`` -- and a future viewer
    applies it again on read, since a caller predating this feature may not have.
    """
    if isinstance(value, dict):
        return {
            k: "***" if _SECRET_KEY_RE.search(k) else redact_params(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_params(v) for v in value]
    if isinstance(value, str):
        return _URL_USERINFO_RE.sub(lambda m: f"{m.group(1)}***@", value)
    return value


@dataclass
class AuditEntry:
    ts: str
    user: str
    action: str
    target: str
    result: str
    params: dict[str, Any] | None = None
    job_id: str | None = None


@dataclass
class AuditFilter:
    """Substring, case-insensitive filters plus a `[since, before)` window.

    `since` is inclusive, `before` is exclusive -- so a viewer paging by day can
    hand the next page's `since` straight back as this page's `before` without
    either double-counting or skipping the boundary entry.
    """

    user: str | None = None
    action: str | None = None
    result: str | None = None
    target: str | None = None
    since: datetime | None = None
    before: datetime | None = None


# Filter strings are operator input rendered back into the viewer; capped the
# same way a free-text filter would be anywhere else in this app.
_MAX_FIELD_LENGTH = 128


def build_filter(
    *,
    user: str | None = None,
    action: str | None = None,
    result: str | None = None,
    target: str | None = None,
    since: str | None = None,
    before: str | None = None,
) -> AuditFilter:
    """Build an `AuditFilter` from raw query-string values.

    The one place the API route and the UI partial agree on how `since`/
    `before` are parsed and how long a substring filter may be. Raises
    `ValueError` -- translated to a 422 by both callers -- on an unparsable
    date.
    """

    def _clip(value: str | None) -> str | None:
        return value[:_MAX_FIELD_LENGTH] if value else value

    return AuditFilter(
        user=_clip(user),
        action=_clip(action),
        result=_clip(result),
        target=_clip(target),
        since=parse_cutoff(since) if since else None,
        before=parse_cutoff(before) if before else None,
    )


@dataclass
class AuditPage:
    entries: list[AuditEntry]
    total: int
    corrupt_lines: int
    facets: dict[str, list[str]]


@dataclass
class PruneResult:
    removed: int
    kept: int
    oldest_removed: str | None
    newest_removed: str | None


def parse_cutoff(value: str) -> datetime:
    """Parse a prune/filter cutoff into a UTC datetime.

    A bare calendar date (``2026-01-01``) becomes that day's start in UTC --
    entries written that day are kept, only strictly older ones are candidates.
    A full ISO datetime is accepted too; a naive one is assumed to already be
    UTC, matching what `write_audit_entry` stamps for a naive caller clock.
    """
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{value!r} is not a valid date or datetime") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _entry_from_line(line: str) -> AuditEntry | None:
    """Parse one JSONL line into an `AuditEntry`, or `None` if it is corrupt.

    `params` is redacted here too, on top of whatever the writer already did --
    a caller that predates `redact_params` (or a hand-edited line) must not put
    a raw credential in front of the viewer.
    """
    try:
        raw = json.loads(line)
        return AuditEntry(
            ts=str(raw["ts"]),
            user=str(raw["user"]),
            action=str(raw["action"]),
            target=str(raw["target"]),
            result=str(raw.get("result", "ok")),
            params=redact_params(raw["params"]) if raw.get("params") is not None else None,
            job_id=str(raw["job_id"]) if raw.get("job_id") is not None else None,
        )
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def _matches(entry: AuditEntry, flt: AuditFilter) -> bool:
    if flt.user and flt.user.lower() not in entry.user.lower():
        return False
    if flt.action and flt.action.lower() not in entry.action.lower():
        return False
    if flt.result and flt.result.lower() not in entry.result.lower():
        return False
    if flt.target and flt.target.lower() not in entry.target.lower():
        return False
    if flt.since is not None or flt.before is not None:
        ts = _parse_ts(entry.ts)
        if ts is None:
            return False
        if flt.since is not None and ts < flt.since:
            return False
        if flt.before is not None and ts >= flt.before:
            return False
    return True


def _read_all(config_dir: str) -> tuple[list[AuditEntry], int]:
    """Every parsable entry in the log, plus a count of the lines that were not."""
    path = audit_path(config_dir)
    if not path.exists():
        return [], 0
    entries: list[AuditEntry] = []
    corrupt = 0
    with path.open("r", encoding="utf-8") as f:
        for raw_line in f:
            stripped = raw_line.strip()
            if not stripped:
                continue
            entry = _entry_from_line(stripped)
            if entry is None:
                corrupt += 1
            else:
                entries.append(entry)
    return entries, corrupt


def query_entries(
    config_dir: str, flt: AuditFilter, *, limit: int, offset: int
) -> AuditPage:
    """One pass over the log: filtered, paginated, newest first.

    Facets (the distinct `user`/`action`/`result` values offered as filter
    choices) are computed over the *unfiltered* set, so picking one value never
    hides the others from the dropdown.
    """
    all_entries, corrupt = _read_all(config_dir)

    facets: dict[str, list[str]] = {
        "user": sorted({e.user for e in all_entries}),
        "action": sorted({e.action for e in all_entries}),
        "result": sorted({e.result for e in all_entries}),
    }

    matched = [e for e in all_entries if _matches(e, flt)]
    matched.sort(key=lambda e: e.ts, reverse=True)

    return AuditPage(
        entries=matched[offset : offset + limit],
        total=len(matched),
        corrupt_lines=corrupt,
        facets=facets,
    )


def iter_entries(config_dir: str, flt: AuditFilter) -> Iterator[AuditEntry]:
    """Every matching entry, newest first -- the export's source, unpaginated."""
    all_entries, _ = _read_all(config_dir)
    matched = [e for e in all_entries if _matches(e, flt)]
    matched.sort(key=lambda e: e.ts, reverse=True)
    yield from matched


def csv_safe(cell: str) -> str:
    """Prefix a cell that Excel/Sheets would read as a formula with `'`.

    Guards the CSV export against formula injection: a username or catalog
    name is written verbatim into the log and could otherwise execute when the
    file is opened in a spreadsheet application.
    """
    if cell.startswith(_CSV_FORMULA_PREFIXES):
        return "'" + cell
    return cell


def prune_before(config_dir: str, cutoff: datetime, *, dry_run: bool = False) -> PruneResult:
    """Delete every entry with a valid `ts` strictly older than *cutoff*.

    Rewrites the log into a sibling temp file under `_LOCK`, then atomically
    replaces the original with `os.replace` -- a writer racing this either lands
    before the swap (and is included) or after it (and starts the next file
    fresh), never in between. A corrupt line or one with no parsable `ts` is
    always kept, never guessed at. When nothing would be removed, or on a dry
    run, the original file is left untouched -- not even its mtime changes.
    """
    path = audit_path(config_dir)
    if not path.exists():
        return PruneResult(removed=0, kept=0, oldest_removed=None, newest_removed=None)

    with _LOCK:
        kept_lines: list[str] = []
        removed_ts: list[str] = []
        kept = 0

        with path.open("r", encoding="utf-8") as f:
            for raw_line in f:
                stripped = raw_line.rstrip("\n")
                if not stripped.strip():
                    continue
                ts: datetime | None = None
                try:
                    raw = json.loads(stripped)
                    ts = _parse_ts(str(raw["ts"]))
                except (json.JSONDecodeError, KeyError, TypeError):
                    ts = None
                if ts is not None and ts < cutoff:
                    removed_ts.append(str(raw["ts"]))
                    continue
                kept_lines.append(stripped)
                kept += 1

        removed = len(removed_ts)
        if removed == 0 or dry_run:
            return PruneResult(
                removed=removed,
                kept=kept,
                oldest_removed=min(removed_ts) if removed_ts else None,
                newest_removed=max(removed_ts) if removed_ts else None,
            )

        tmp_path = path.with_name(path.name + ".prune.tmp")
        try:
            with tmp_path.open("w", encoding="utf-8") as f:
                for line in kept_lines:
                    f.write(line + "\n")
                f.flush()
                os.fsync(f.fileno())
            shutil.copymode(path, tmp_path)
            os.replace(tmp_path, path)
        except Exception:
            tmp_path.unlink(missing_ok=True)
            raise

        return PruneResult(
            removed=removed,
            kept=kept,
            oldest_removed=min(removed_ts),
            newest_removed=max(removed_ts),
        )
