"""A folder the ingester reads, as the Embedding page browses it and selects from it.

The ingester scans a directory and narrows it with `filters.include` globs; it cannot be
handed a file. The page therefore selects paths *relative to a root*, and this module turns
that selection into the root's path inside the ingester and the globs, after checking every
path against the one thing that must hold: nothing outside the root is ever listed, counted
or selected.

* **Paths are jailed by construction.** A path is split into plain segments (no `..`, `.`,
  backslash or control character), and every segment is looked at with `lstat` on the way
  down from a root that was resolved once. A symbolic link anywhere on the path refuses it,
  so there is no `resolve()` to race and no link to follow out of the root. The manager's own
  files (`.env` files, the Keycloak secrets) live in the same configuration directory as the
  documents folder, which is why this matters.
* **The ingester's glob dialect has no escape.** `*` and `?` in a file name would match
  other files than the one named, so such a name is refused instead of selected.
* **The mount is read-only on the ingester's side**, so nothing here can make it write.

The same class serves the documents folder (`/data/local`, with the staging area hidden) and
one upload's staging folder (`/data/local/uploads/<owner>/<batch>`).
"""
from __future__ import annotations

import os
import stat
import unicodedata
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from app.core.ingest.errors import InvalidRequest, NotFound

MAX_LISTING = 2000
MAX_INCLUDES = 200
MAX_NAME_BYTES = 255
MAX_PATH_BYTES = 1024

_GLOB_CHARS = ("*", "?")


def _has_control_character(value: str) -> bool:
    return any(unicodedata.category(char).startswith("C") for char in value)


def clean_rel(rel: str | None) -> str:
    """A relative posix path as plain segments joined by `/`; "" is the root.

    Empty segments (a doubled or trailing slash) are dropped; everything that could leave
    the root is refused instead of normalised away.
    """
    if not rel:
        return ""
    if rel.startswith("/") or "\\" in rel:
        raise InvalidRequest("A path is relative to the folder and uses '/'.")
    if len(rel.encode("utf-8", "surrogatepass")) > MAX_PATH_BYTES:
        raise InvalidRequest("That path is too long.")
    parts = [part for part in rel.split("/") if part]
    for part in parts:
        if part in (".", ".."):
            raise InvalidRequest("A path must not contain '.' or '..'.")
        if _has_control_character(part):
            raise InvalidRequest("A path must not contain control characters.")
        if len(part.encode("utf-8", "surrogatepass")) > MAX_NAME_BYTES:
            raise InvalidRequest("A name in that path is too long.")
    return "/".join(parts)


def refuse_glob(rel: str) -> None:
    """Refuse a name the ingester's include globs cannot match literally."""
    if any(char in rel for char in _GLOB_CHARS):
        raise InvalidRequest(
            f"{rel!r} contains '*' or '?', which the ingester would read as a wildcard. "
            "Rename the file or select its folder."
        )


@dataclass(frozen=True)
class Entry:
    name: str
    rel: str
    is_dir: bool
    size: int | None
    mtime: float


@dataclass(frozen=True)
class Listing:
    path: str
    entries: tuple[Entry, ...]
    truncated: bool


@dataclass(frozen=True)
class Counts:
    files: int
    bytes: int


class Documents:
    """One root, its path in the ingester, and the names hidden at its top level."""

    def __init__(
        self, root: Path, container_root: str, *, hidden_top: frozenset[str] = frozenset()
    ) -> None:
        self._root = root.resolve()
        self._container_root = container_root.rstrip("/")
        self._hidden_top = hidden_top

    @property
    def root(self) -> Path:
        return self._root

    # ── paths ───────────────────────────────────────────────────────────────

    def resolve(self, rel: str | None) -> Path:
        """The path on disk, after the checks above. Raises `NotFound` for a missing one."""
        cleaned = clean_rel(rel)
        parts = cleaned.split("/") if cleaned else []
        if parts and parts[0] in self._hidden_top:
            raise InvalidRequest(f"{parts[0]!r} is not available here.")
        current = self._root
        for part in parts:
            current = current / part
            try:
                mode = current.lstat().st_mode
            except FileNotFoundError as exc:
                raise NotFound(cleaned) from exc
            if stat.S_ISLNK(mode):
                raise InvalidRequest("Symbolic links are not followed.")
        return current

    def container_path(self, rel: str = "") -> str:
        """Where the ingester sees `rel`."""
        cleaned = clean_rel(rel)
        return f"{self._container_root}/{cleaned}" if cleaned else self._container_root

    # ── reading ─────────────────────────────────────────────────────────────

    def list_dir(self, rel: str | None = "") -> Listing:
        """The entries of one directory, folders first, without links and hidden names."""
        cleaned = clean_rel(rel)
        target = self.resolve(cleaned)
        if not target.is_dir():
            raise InvalidRequest("That is not a folder.")
        entries: list[Entry] = []
        truncated = False
        with os.scandir(target) as scan:
            for item in scan:
                if not cleaned and item.name in self._hidden_top:
                    continue
                try:
                    if item.is_symlink():
                        continue
                    info = item.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISDIR(info.st_mode):
                    is_dir = True
                elif stat.S_ISREG(info.st_mode):
                    is_dir = False
                else:
                    continue
                if len(entries) >= MAX_LISTING:
                    truncated = True
                    break
                rel_path = f"{cleaned}/{item.name}" if cleaned else item.name
                size = None if is_dir else info.st_size
                entries.append(Entry(item.name, rel_path, is_dir, size, info.st_mtime))
        entries.sort(key=lambda entry: (not entry.is_dir, entry.name.casefold()))
        return Listing(cleaned, tuple(entries), truncated)

    def files_under(self, rel: str | None = "") -> Iterator[tuple[str, int]]:
        """Every regular file at or below `rel`, as (relative path, size); links are skipped."""
        cleaned = clean_rel(rel)
        target = self.resolve(cleaned)
        if not target.is_dir():
            if target.is_file():
                yield cleaned, target.stat().st_size
            return
        for dirpath, dirnames, filenames in os.walk(target, followlinks=False):
            here = Path(dirpath)
            if here == target and not cleaned:
                dirnames[:] = [name for name in dirnames if name not in self._hidden_top]
            for name in filenames:
                path = here / name
                try:
                    info = path.lstat()
                except OSError:
                    continue
                if not stat.S_ISREG(info.st_mode):
                    continue
                yield path.relative_to(self._root).as_posix(), info.st_size

    def count(self, rels: Iterable[str]) -> Counts:
        """Files and bytes in a selection, each file once however it was selected."""
        seen: dict[str, int] = {}
        for rel in rels:
            for path, size in self.files_under(rel):
                seen[path] = size
        return Counts(len(seen), sum(seen.values()))

    # ── selecting ───────────────────────────────────────────────────────────

    def include_patterns(self, rels: Iterable[str]) -> list[str]:
        """`filters.include` globs for a selection; empty when the whole root is selected.

        A folder becomes `<folder>/**` and a file its own path. A path under a selected
        folder adds nothing. Every path must exist and be free of wildcards.
        """
        cleaned = sorted({clean_rel(rel) for rel in rels})
        if not cleaned:
            raise InvalidRequest("Select at least one file or folder.")
        if "" in cleaned:
            return []
        folders: list[str] = []
        patterns: list[str] = []
        for rel in cleaned:
            if any(rel == folder or rel.startswith(folder + "/") for folder in folders):
                continue
            refuse_glob(rel)
            if self.resolve(rel).is_dir():
                folders.append(rel)
                patterns.append(f"{rel}/**")
            else:
                patterns.append(rel)
        if len(patterns) > MAX_INCLUDES:
            raise InvalidRequest(
                f"That is {len(patterns)} separate selections; select their folder instead "
                f"(at most {MAX_INCLUDES})."
            )
        return patterns
