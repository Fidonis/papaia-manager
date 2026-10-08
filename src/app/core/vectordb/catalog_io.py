"""Replacing one of the ingester's catalog files without clobbering a concurrent change.

`connections.yaml` and `jobs.yaml` are written by the ingester's web interface as well as
by the manager, and the ingester serialises its own writes with a lock of its own process
only. A manager write is therefore compare-and-swap: the change is staged in a file of the
manager's own name (the ingester stages in `.<name>.tmp`, which would be clobbered), and
swapped in only if the file still has the content the change was computed from.

This module is the part both stores share. What a file *means* (its schema, which
problems refuse a write) stays with each store.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import stat
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

DEFAULT_MODE = 0o644


def revision(raw: bytes) -> str:
    """SHA-256 of the bytes, or "" when there is no file. Compare-and-swap compares this."""
    return hashlib.sha256(raw).hexdigest() if raw else ""


def dump_document(document: Mapping[str, Any]) -> str:
    """The ingester's own dump options, so a diff of the file shows only the change."""
    return yaml.safe_dump(
        dict(document), default_flow_style=False, sort_keys=False, allow_unicode=True
    )


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


def _keep_backup(directory: Path, name: str, staged_name: str, previous: bytes) -> None:
    """Keep the previous content as `<name>.bak`, as the ingester does.

    Best effort, and by rename: the ingester copies over the old backup and fails
    when that file belongs to somebody else, which must not stop this change.
    """
    staged = directory / staged_name
    try:
        staged.write_bytes(previous)
        os.replace(staged, directory / f"{name}.bak")
    except OSError:
        with contextlib.suppress(OSError):
            staged.unlink(missing_ok=True)


def swap(
    path: Path,
    *,
    tmp_name: str,
    backup_tmp_name: str,
    expected_revision: str,
    existed: bool,
    previous: bytes,
    data: bytes,
    backup: bool = True,
) -> bool:
    """Replace `path` with `data` if it still has the content `expected_revision` names.

    Returns False, writing nothing, when the file changed in the meantime. An `OSError`
    from the write is authoritative and propagates: the caller decides what it means.
    `backup=False` skips the `.bak` copy, for a file whose previous content must not
    linger next to it (the encrypted secrets).
    """
    directory = path.parent
    tmp = directory / tmp_name
    try:
        mode = stat.S_IMODE(path.stat().st_mode) if existed else DEFAULT_MODE
        _stage(tmp, data, mode)
        try:
            current = revision(path.read_bytes())
        except FileNotFoundError:
            current = ""
        if current != expected_revision:
            return False
        if existed and backup:
            _keep_backup(directory, path.name, backup_tmp_name, previous)
        os.replace(tmp, path)
        return True
    finally:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
