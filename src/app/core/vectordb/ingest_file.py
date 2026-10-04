"""The ingester's connection store: `ai/rag/catalog/connections.yaml`.

The ingester reads this file as `/config/catalog/connections.yaml` and keeps writing it
from its own web interface, so the manager is a second writer of a file it does not own.
That decides almost everything here:

* **The format is the ingester's, exactly.** `{version: 1, connections: [{name, url,
  api_key?}]}`, where `api_key` is an `enc:1:` token. The ingester rejects an entry with
  any other key, and a rejected entry makes it refuse the whole file at runtime, so
  nothing but those three keys is ever written. `ConnectionEntry` mirrors its schema.
* **A change is a surgery on the document, not a rebuild.** The file is parsed to plain
  data, one entry is touched, and the document is dumped with the ingester's own options.
  Entries the manager does not understand, and top-level keys it does not know, stay.
* **A write is compare-and-swap.** The ingester serialises its writes with a lock of its
  own process only. The change is staged in a file of the manager's own name (the
  ingester stages in `.connections.yaml.tmp`, which would be clobbered), and replaced in
  only if the file still has the content the change was computed from. Otherwise it is
  computed again from the new content, a few times.
* **Existing problems do not lock the file.** The ingester refuses any write while the
  file has an error; the manager refuses only a write that introduces a new one. After a
  rotation of the secret every key is unreadable, and the repair must still be possible.
  Structural damage (not a mapping, unsupported version, `connections` not a list) does
  refuse, because there is nothing safe to change.
* **The catalog directory is never created.** If it is missing or does not accept a
  write, the store is read-only and says so. An `OSError` from the write itself is
  authoritative; a permission probe is only a hint for the page.
"""
from __future__ import annotations

import contextlib
import copy
import hashlib
import json
import os
import re
import stat
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from app.core.vectordb.crypto import ENC_PREFIX
from app.core.vectordb.errors import (
    ConnectionFileError,
    ConnectionsReadOnlyError,
    InvalidConnectionError,
)

CONNECTIONS_RELPATH = Path("ai") / "rag" / "catalog" / "connections.yaml"

# The ingester's name rule, the same as a job id.
NAME_PATTERN = r"^[a-z0-9][a-z0-9_-]{0,63}$"
_NAME_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")

_TMP_NAME = ".connections.yaml.manager.tmp"
_BACKUP_TMP_NAME = ".connections.yaml.bak.manager.tmp"
_MAX_ATTEMPTS = 3
_DEFAULT_MODE = 0o644

# Process-wide, for the manager's own writers. It says nothing about the ingester.
_LOCK = threading.Lock()


def is_valid_name(name: str) -> bool:
    return _NAME_RE.fullmatch(name) is not None


class ConnectionEntry(BaseModel):
    """One entry as the ingester's schema accepts it (`connections/schema.py`)."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=NAME_PATTERN)
    url: str = Field(min_length=1)
    api_key: str | None = None

    @field_validator("url")
    @classmethod
    def _http_scheme(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("url must start with http:// or https://")
        return value

    @field_validator("api_key")
    @classmethod
    def _encrypted_only(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        if not value.startswith(ENC_PREFIX):
            raise ValueError("api_key must be an enc:1: token; a literal value is not accepted")
        return value


@dataclass(frozen=True)
class FileIssue:
    """A problem with one entry (or with the file, when `name` is None)."""

    name: str | None
    field: str
    message: str

    @property
    def key(self) -> tuple[str | None, str]:
        return (self.name, self.field)

    def __str__(self) -> str:
        scope = f"connection {self.name!r}: " if self.name else ""
        return f"{scope}{self.field}: {self.message}"


@dataclass(frozen=True)
class FileSnapshot:
    """The file as read once: bytes, parsed document, and what is wrong with it."""

    path: Path
    exists: bool
    # A hint for the page: the directory exists and is not obviously read-only.
    writable: bool
    raw: bytes
    # SHA-256 of the bytes, "" when there is no file. Compare-and-swap compares this.
    revision: str
    document: dict[str, Any]
    issues: tuple[FileIssue, ...] = ()
    structural_error: str | None = None
    entries: tuple[dict[str, Any], ...] = field(default=())


def entry_etag(entry: Mapping[str, Any]) -> str:
    """A short fingerprint of one entry as authored.

    Per entry rather than per file: a fingerprint of the file would answer every
    unrelated write of the ingester's interface, or of another administrator, with a
    conflict.
    """
    text = json.dumps(entry, sort_keys=True, default=str)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def dump_document(document: Mapping[str, Any]) -> str:
    """The ingester's own dump options, so a diff of the file shows only the change."""
    return yaml.safe_dump(
        dict(document), default_flow_style=False, sort_keys=False, allow_unicode=True
    )


def find_entry(document: Mapping[str, Any], name: str) -> dict[str, Any] | None:
    for entry in document.get("connections") or []:
        if isinstance(entry, dict) and entry.get("name") == name:
            return entry
    return None


def remove_entry(document: dict[str, Any], name: str) -> None:
    document["connections"] = [
        entry
        for entry in document.get("connections") or []
        if not (isinstance(entry, dict) and entry.get("name") == name)
    ]


def _issues_from(name: str | None, exc: ValidationError) -> list[FileIssue]:
    return [
        FileIssue(name, ".".join(str(part) for part in error["loc"]) or "<root>", error["msg"])
        for error in exc.errors()
    ]


def validate_entries(raw_entries: list[Any]) -> list[FileIssue]:
    """The ingester's per-entry validation, without the decryption of the keys."""
    issues: list[FileIssue] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_entries):
        if not isinstance(raw, dict):
            issues.append(FileIssue(None, f"connections[{index}]", "connection must be a mapping"))
            continue
        name = raw.get("name") if isinstance(raw.get("name"), str) else None
        try:
            entry = ConnectionEntry.model_validate(raw)
        except ValidationError as exc:
            issues.extend(_issues_from(name or f"connections[{index}]", exc))
            continue
        if entry.name in seen:
            issues.append(FileIssue(entry.name, "name", "duplicate connection name"))
        seen.add(entry.name)
    return issues


def _starter() -> dict[str, Any]:
    return {"version": 1, "connections": []}


def _parse(raw: bytes) -> tuple[dict[str, Any], str | None]:
    """The document, or the reason it cannot be changed."""
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else "invalid YAML"
        return _starter(), f"invalid YAML: {first_line}"
    if document is None:
        document = {}
    if not isinstance(document, dict):
        return _starter(), "the top level must be a mapping"
    # The ingester's interface sets a missing version on its first save; so does this.
    document.setdefault("version", 1)
    if document["version"] != 1:
        return _starter(), f"unsupported connections version {document['version']!r}; expected 1"
    connections = document.get("connections")
    if connections is None:
        document["connections"] = []
    elif not isinstance(connections, list):
        return _starter(), "connections must be a list"
    return document, None


def _revision(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest() if raw else ""


class IngestFileRepository:
    """`connections.yaml` of the RAG module, read and changed on behalf of the manager."""

    def __init__(self, config_dir: str | Path) -> None:
        self._path = Path(config_dir) / CONNECTIONS_RELPATH

    @property
    def path(self) -> Path:
        return self._path

    # ── reading ─────────────────────────────────────────────────────────────

    def snapshot(self) -> FileSnapshot:
        """The file as it is now. Never raises."""
        directory = self._path.parent
        writable = directory.is_dir() and os.access(directory, os.W_OK)
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return FileSnapshot(
                path=self._path,
                exists=False,
                writable=writable,
                raw=b"",
                revision="",
                document=_starter(),
            )
        except OSError as exc:
            return FileSnapshot(
                path=self._path,
                exists=True,
                writable=writable,
                raw=b"",
                revision="",
                document=_starter(),
                structural_error=f"the file cannot be read: {exc.strerror or exc}",
            )

        document, problem = _parse(raw)
        entries = [entry for entry in document["connections"] if isinstance(entry, dict)]
        issues = tuple(validate_entries(document["connections"])) if problem is None else ()
        return FileSnapshot(
            path=self._path,
            exists=True,
            writable=writable,
            raw=raw,
            revision=_revision(raw),
            document=document,
            issues=issues,
            structural_error=problem,
            entries=tuple(copy.deepcopy(entries)),
        )

    # ── writing ─────────────────────────────────────────────────────────────

    def update(self, mutator: Callable[[dict[str, Any]], None]) -> FileSnapshot:
        """Apply `mutator` to the current document and replace the file with the result.

        `mutator` edits the document in place and may raise a `ConnectionStoreError` to
        refuse. It can run more than once, each time on the then-current content, so it
        must decide from the document alone. Raises `ConnectionFileError` for a file
        that cannot be changed or keeps changing, `InvalidConnectionError` if the result
        would contain a new problem, and `ConnectionsReadOnlyError` if the write fails.
        """
        with _LOCK:
            for _ in range(_MAX_ATTEMPTS):
                snapshot = self.snapshot()
                if snapshot.structural_error is not None:
                    raise ConnectionFileError(
                        f"connections.yaml cannot be changed: {snapshot.structural_error}"
                    )
                document = copy.deepcopy(snapshot.document)
                mutator(document)

                known = {issue.key for issue in snapshot.issues}
                fresh = [
                    issue for issue in validate_entries(document["connections"])
                    if issue.key not in known
                ]
                if fresh:
                    raise InvalidConnectionError("; ".join(str(issue) for issue in fresh))

                result = self._replace(snapshot, dump_document(document).encode("utf-8"))
                if result is not None:
                    return result
            raise ConnectionFileError(
                "connections.yaml keeps changing while the manager writes it; try again"
            )

    def _replace(self, snapshot: FileSnapshot, data: bytes) -> FileSnapshot | None:
        """Swap `data` in, or return None if the file is no longer what it was read as."""
        directory = self._path.parent
        if not directory.is_dir():
            raise ConnectionsReadOnlyError(
                f"{directory} does not exist; the manager does not create it"
            )
        tmp = directory / _TMP_NAME
        try:
            mode = (
                stat.S_IMODE(self._path.stat().st_mode) if snapshot.exists else _DEFAULT_MODE
            )
            self._stage(tmp, data, mode)
            try:
                current = _revision(self._path.read_bytes())
            except FileNotFoundError:
                current = ""
            if current != snapshot.revision:
                return None
            if snapshot.exists:
                self._backup(directory, snapshot.raw)
            os.replace(tmp, self._path)
        except OSError as exc:
            raise ConnectionsReadOnlyError(
                f"{directory} does not accept the change ({exc.strerror or type(exc).__name__}); "
                "connections are read-only here"
            ) from exc
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)
        return self.snapshot()

    @staticmethod
    def _stage(tmp: Path, data: bytes, mode: int) -> None:
        # O_BINARY: on Windows a descriptor is otherwise opened in text mode and
        # would rewrite every newline.
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
        tmp.unlink(missing_ok=True)
        fd = os.open(tmp, flags, mode)
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # The umask may have narrowed the mode; the file must stay as readable to the
        # host operator as the one it replaces. Some mounts reject chmod.
        with contextlib.suppress(OSError):
            os.chmod(tmp, mode)

    @staticmethod
    def _backup(directory: Path, previous: bytes) -> None:
        """Keep the previous content as `connections.yaml.bak`, as the ingester does.

        Best effort, and by rename: the ingester copies over the old backup and fails
        when that file belongs to somebody else, which must not stop this change.
        """
        staged = directory / _BACKUP_TMP_NAME
        try:
            staged.write_bytes(previous)
            os.replace(staged, directory / "connections.yaml.bak")
        except OSError:
            with contextlib.suppress(OSError):
                staged.unlink(missing_ok=True)
