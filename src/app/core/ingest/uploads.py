"""The upload area of the Embedding page: files that live only until they are embedded.

An upload may be confidential, so it is treated as a loan to the ingester rather than as
storage. Each upload gets a staging folder of its own, `uploads/<owner>/<batch>/` inside the
documents folder (`/data/local/uploads/...` for the ingester). That keeps the uploads of
different administrators and of different occasions apart, and gives a run exactly one folder
as its source. A small manifest per upload, outside the documents folder, records who made it
and where it stands:

    staged    files are being added, or are ready to be embedded
    embedding a run is working on it
    kept      the run failed, or some documents failed: the files stay for a retry

What happens to the files afterwards is decided in `app.core.ingest.runs.reconcile`: a run
that succeeded without a failed document removes the whole staging folder, and the time limit
(`INGEST_UPLOAD_TTL_HOURS`) removes whatever is left, whatever its state. Nothing else ever
deletes a file the manager did not create.

Things worth knowing when touching this:

* **A staged file is private**: folders `0700`, files `0600`. The manager and the ingester run
  as the same user in the stack's compose, so that is enough for the ingester to read them.
* **A manifest is not in the staging folder**, because the ingester would embed it (`.yaml` is
  a supported type) and because a deleted folder must not take its own record with it before
  the removal is known to have worked.
* **Names are checked, not repaired.** An upload path with `..`, a backslash, a control
  character or a wildcard is refused; the ingester's include globs cannot express such a name.
* **The same relative path is the same document.** Uploading `a/b.pdf` again, in another upload
  or later, replaces its chunks in the collection (see `catalog`), which is the update.
* **The size limits are enforced while the file is written**, not trusted from a header.
* The store assumes one manager process, like the audit log and the scheduler.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import re
import secrets
import shutil
import threading
import unicodedata
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import yaml

from app.core.ingest.catalog import LOCAL_ROOT
from app.core.ingest.documents import (
    MAX_NAME_BYTES,
    MAX_PATH_BYTES,
    Documents,
    clean_rel,
    refuse_glob,
)
from app.core.ingest.errors import (
    Conflict,
    IngestUnavailable,
    InvalidRequest,
    NotFound,
    TooLarge,
)

UPLOADS_DIR = "uploads"
BATCHES_RELPATH = Path("manager") / "ingest" / "batches"

STATE_STAGED = "staged"
STATE_EMBEDDING = "embedding"
STATE_KEPT = "kept"
_STATES = {STATE_STAGED, STATE_EMBEDDING, STATE_KEPT}

BATCH_ID_RE = re.compile(r"\d{8}-\d{6}-[0-9a-f]{8}")
_OWNER_RE = re.compile(r"[^a-z0-9._-]+")

DIR_MODE = 0o700
FILE_MODE = 0o600
CHUNK = 1024 * 1024
MAX_NAME_LENGTH = 120

_LOCK = threading.RLock()


class Reader(Protocol):
    """What an uploaded file offers: `UploadFile` does, and so does a test double."""

    async def read(self, size: int = -1, /) -> bytes: ...


def owner_slug(name: str) -> str:
    """A folder name for the owner of an upload: lowercase, no path characters."""
    slug = _OWNER_RE.sub("-", name.strip().lower()).strip("-.")[:48]
    return slug or "user"


def _now() -> datetime:
    return datetime.now(UTC)


def _stamp(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def _parse_stamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return datetime.fromtimestamp(0, UTC)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


@dataclass(frozen=True)
class Batch:
    id: str
    owner: str
    owner_name: str
    name: str
    created: str
    updated: str
    state: str = STATE_STAGED
    files: int = 0
    bytes: int = 0
    run_id: str | None = None
    job_id: str | None = None
    collection: str | None = None
    connection: str | None = None
    mode: str | None = None
    note: str | None = None

    @property
    def relpath(self) -> str:
        return f"{UPLOADS_DIR}/{self.owner}/{self.id}"

    @property
    def container_path(self) -> str:
        return f"{LOCAL_ROOT}/{self.relpath}"

    @property
    def age(self) -> timedelta:
        return _now() - _parse_stamp(self.updated)


@dataclass(frozen=True)
class SavedFile:
    path: str
    bytes: int
    replaced: bool


def clean_upload_path(raw: str) -> str:
    """The relative path an uploaded file is stored under, or an `InvalidRequest`."""
    if not raw or not raw.strip():
        raise InvalidRequest("A file needs a name.")
    if raw.endswith("/"):
        raise InvalidRequest("A file name does not end with '/'.")
    cleaned = clean_rel(raw)
    if not cleaned:
        raise InvalidRequest("A file needs a name.")
    for part in cleaned.split("/"):
        if part != part.strip() or part.endswith("."):
            raise InvalidRequest(f"{part!r} is not a usable file name.")
        if any(unicodedata.category(char) == "Zs" and char != " " for char in part):
            raise InvalidRequest(f"{part!r} is not a usable file name.")
    refuse_glob(cleaned)
    if len(cleaned.encode("utf-8", "surrogatepass")) > MAX_PATH_BYTES or any(
        len(part.encode("utf-8", "surrogatepass")) > MAX_NAME_BYTES for part in cleaned.split("/")
    ):
        raise InvalidRequest("That path is too long.")
    return cleaned


def _private_dir(path: Path) -> None:
    if not path.is_dir():
        path.mkdir(mode=DIR_MODE)
    with contextlib.suppress(OSError):
        # The umask may have narrowed or widened the mode of a folder this created.
        os.chmod(path, DIR_MODE)


class UploadStore:
    """The staging folders under the documents folder, and the manifests that describe them."""

    def __init__(
        self,
        config_dir: str,
        documents_root: Path,
        *,
        max_file_bytes: int,
        max_batch_bytes: int,
    ) -> None:
        self._docs_root = documents_root.resolve()
        self._uploads = self._docs_root / UPLOADS_DIR
        self._manifests = Path(config_dir) / BATCHES_RELPATH
        self._max_file = max_file_bytes
        self._max_batch = max_batch_bytes

    # ── manifests ───────────────────────────────────────────────────────────

    def _manifest_path(self, batch_id: str) -> Path:
        return self._manifests / f"{batch_id}.yaml"

    def _write(self, batch: Batch) -> None:
        self._manifests.mkdir(parents=True, exist_ok=True)
        target = self._manifest_path(batch.id)
        tmp = target.with_suffix(".yaml.tmp")
        tmp.write_text(
            yaml.safe_dump(asdict(batch), sort_keys=False, allow_unicode=True), encoding="utf-8"
        )
        os.replace(tmp, target)

    def _read(self, path: Path) -> Batch | None:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            return None
        if not isinstance(raw, dict):
            return None
        try:
            batch = Batch(**{key: raw[key] for key in Batch.__dataclass_fields__ if key in raw})
        except TypeError:
            return None
        if not BATCH_ID_RE.fullmatch(batch.id) or batch.state not in _STATES:
            return None
        return batch

    def batches(self) -> list[Batch]:
        """Every upload, newest first. A manifest that cannot be read is left out."""
        if not self._manifests.is_dir():
            return []
        found = [
            batch
            for path in self._manifests.glob("*.yaml")
            if (batch := self._read(path)) is not None
        ]
        return sorted(found, key=lambda batch: batch.created, reverse=True)

    def get(self, batch_id: str) -> Batch:
        if not BATCH_ID_RE.fullmatch(batch_id):
            raise NotFound(batch_id)
        batch = self._read(self._manifest_path(batch_id))
        if batch is None:
            raise NotFound(batch_id)
        return batch

    def directory(self, batch: Batch) -> Path:
        return self._uploads / batch.owner / batch.id

    def documents(self, batch: Batch) -> Documents:
        """The staging folder as a root to browse and select from."""
        directory = self.directory(batch)
        if not directory.is_dir():
            raise NotFound(batch.id)
        return Documents(directory, batch.container_path)

    # ── creating and filling ────────────────────────────────────────────────

    def create(self, *, owner_sub: str, owner_name: str, name: str = "") -> Batch:
        """A new, empty upload owned by `owner_name`."""
        label = name.strip()
        if len(label) > MAX_NAME_LENGTH or any(
            unicodedata.category(char).startswith("C") for char in label
        ):
            raise InvalidRequest("Not a valid name for an upload.")
        owner = owner_slug(owner_name or owner_sub)
        moment = _now()
        batch_id = f"{moment:%Y%m%d-%H%M%S}-{secrets.token_hex(4)}"
        try:
            _private_dir(self._uploads)
            _private_dir(self._uploads / owner)
            _private_dir(self._uploads / owner / batch_id)
        except OSError as exc:
            raise IngestUnavailable(
                "The documents folder does not accept uploads "
                f"({exc.strerror or type(exc).__name__})."
            ) from exc
        batch = Batch(
            id=batch_id,
            owner=owner,
            owner_name=owner_name or owner_sub,
            name=label,
            created=_stamp(moment),
            updated=_stamp(moment),
        )
        with _LOCK:
            self._write(batch)
        return batch

    async def save_file(self, batch_id: str, raw_path: str, reader: Reader) -> SavedFile:
        """Store one file under `raw_path` in an upload, replacing a file of the same path.

        The size limits are checked as the bytes arrive. The file is written to a temporary
        name in its folder and renamed into place, so a refused or interrupted upload leaves
        nothing behind and never a half-written file under the real name.
        """
        batch = self.get(batch_id)
        if batch.state == STATE_EMBEDDING:
            raise Conflict("This upload is being embedded; wait for the run to finish.")
        rel = clean_upload_path(raw_path)
        directory = self.directory(batch)
        if not directory.is_dir():
            raise NotFound(batch_id)

        parts = rel.split("/")
        try:
            current = directory
            for part in parts[:-1]:
                current = current / part
                if current.is_symlink():
                    raise InvalidRequest("Symbolic links are not followed.")
                if current.exists() and not current.is_dir():
                    raise InvalidRequest(f"{part!r} is a file, so it cannot hold other files.")
                _private_dir(current)
        except OSError as exc:
            raise IngestUnavailable(
                "The documents folder does not accept uploads "
                f"({exc.strerror or type(exc).__name__})."
            ) from exc
        target = current / parts[-1]
        if target.is_symlink() or target.is_dir():
            raise InvalidRequest(f"{rel!r} is a folder or a link, not a file.")
        previous = target.stat().st_size if target.is_file() else None

        tmp = current / f".{secrets.token_hex(6)}.part"
        written = 0
        try:
            descriptor = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, FILE_MODE)
            with os.fdopen(descriptor, "wb") as handle:
                while True:
                    chunk = await reader.read(CHUNK)
                    if not chunk:
                        break
                    written += len(chunk)
                    if written > self._max_file:
                        raise TooLarge(
                            f"{rel!r} is larger than the limit of {self._max_file // 2**20} MiB "
                            "per file (INGEST_MAX_UPLOAD_MB)."
                        )
                    if batch.bytes - (previous or 0) + written > self._max_batch:
                        raise TooLarge(
                            "This upload would be larger than the limit of "
                            f"{self._max_batch // 2**20} MiB (INGEST_MAX_BATCH_MB)."
                        )
                    await asyncio.to_thread(handle.write, chunk)
                handle.flush()
                await asyncio.to_thread(os.fsync, handle.fileno())
            os.replace(tmp, target)
            with contextlib.suppress(OSError):
                os.chmod(target, FILE_MODE)
        except OSError as exc:
            raise IngestUnavailable(
                f"The file could not be stored ({exc.strerror or type(exc).__name__})."
            ) from exc
        finally:
            with contextlib.suppress(OSError):
                tmp.unlink(missing_ok=True)

        with _LOCK:
            fresh = self._read(self._manifest_path(batch_id)) or batch
            self._write(
                replace(
                    fresh,
                    files=fresh.files + (0 if previous is not None else 1),
                    bytes=max(0, fresh.bytes - (previous or 0) + written),
                    updated=_stamp(_now()),
                    state=STATE_STAGED if fresh.state == STATE_KEPT else fresh.state,
                    note=None if fresh.state == STATE_KEPT else fresh.note,
                )
            )
        return SavedFile(rel, written, previous is not None)

    # ── the life of an upload ───────────────────────────────────────────────

    def mark_embedding(
        self,
        batch_id: str,
        *,
        run_id: str,
        job_id: str,
        collection: str,
        connection: str,
        mode: str,
    ) -> Batch:
        with _LOCK:
            batch = replace(
                self.get(batch_id),
                state=STATE_EMBEDDING,
                run_id=run_id,
                job_id=job_id,
                collection=collection,
                connection=connection,
                mode=mode,
                note=None,
                updated=_stamp(_now()),
            )
            self._write(batch)
            return batch

    def mark_kept(self, batch_id: str, note: str) -> Batch:
        with _LOCK:
            batch = replace(
                self.get(batch_id), state=STATE_KEPT, note=note[:300], updated=_stamp(_now())
            )
            self._write(batch)
            return batch

    def remove(self, batch_id: str) -> Batch:
        """Delete an upload's files and then its record. The files go first on purpose.

        A removal that fails half-way keeps the record, so the next sweep finds the upload
        again instead of leaving files that nothing knows about.
        """
        with _LOCK:
            batch = self.get(batch_id)
            directory = self.directory(batch)
            try:
                if directory.is_dir() and not directory.is_symlink():
                    shutil.rmtree(directory)
            except OSError as exc:
                raise IngestUnavailable(
                    f"The staged files of {batch.id} could not be removed "
                    f"({exc.strerror or type(exc).__name__})."
                ) from exc
            with contextlib.suppress(OSError):
                # Only when empty: another upload of the same owner may be in it.
                directory.parent.rmdir()
            self._manifest_path(batch.id).unlink(missing_ok=True)
            return batch

    def discard(self, batch_id: str) -> Batch:
        """Remove an upload on request. Refused while a run is reading it."""
        if self.get(batch_id).state == STATE_EMBEDDING:
            raise Conflict("This upload is being embedded; abort the run first.")
        return self.remove(batch_id)

    def expired(self, ttl_hours: int, *, keep: frozenset[str] = frozenset()) -> list[Batch]:
        """The uploads whose last activity is older than the limit, except those in `keep`."""
        limit = timedelta(hours=ttl_hours)
        return [
            batch for batch in self.batches() if batch.id not in keep and batch.age > limit
        ]

    def orphans(self, ttl_hours: int) -> list[Path]:
        """Staging folders that no manifest knows and that nobody touched for the limit."""
        if not self._uploads.is_dir():
            return []
        known = {batch.id for batch in self.batches()}
        cutoff = _now().timestamp() - ttl_hours * 3600
        found: list[Path] = []
        for owner in self._uploads.iterdir():
            if not owner.is_dir() or owner.is_symlink():
                continue
            for entry in owner.iterdir():
                if (
                    entry.is_dir()
                    and not entry.is_symlink()
                    and BATCH_ID_RE.fullmatch(entry.name)
                    and entry.name not in known
                    and entry.stat().st_mtime < cutoff
                ):
                    found.append(entry)
        return found

    def remove_orphan(self, path: Path) -> None:
        with _LOCK:
            shutil.rmtree(path, ignore_errors=True)
            with contextlib.suppress(OSError):
                path.parent.rmdir()

    def dropped_manifests(self) -> list[Batch]:
        """Records of uploads whose folder is gone, which is nothing left to protect."""
        return [
            batch
            for batch in self.batches()
            if batch.state != STATE_EMBEDDING and not self.directory(batch).is_dir()
        ]

    def drop_manifest(self, batch_id: str) -> None:
        with _LOCK:
            self._manifest_path(batch_id).unlink(missing_ok=True)


def new_store(settings: Any, documents_root: Path) -> UploadStore:
    """The store with the limits of the manager's settings."""
    return UploadStore(
        settings.papaia_config_dir,
        documents_root,
        max_file_bytes=settings.ingest_max_upload_mb * 2**20,
        max_batch_bytes=settings.ingest_max_batch_mb * 2**20,
    )
