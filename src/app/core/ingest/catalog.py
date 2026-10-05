"""The managed jobs in the ingester's job catalog, `ai/rag/catalog/jobs.yaml`.

The ingester can only run what `jobs.yaml` declares and has no API to create a job, so the
manager keeps two jobs per (connection, collection) there, one for each kind of source:

* `upload`: the staging folder of one upload. The job's path is changed for every run.
* `folder`: the documents folder itself, narrowed by `filters.include` to what was selected.

Their ids start with `MANAGED_PREFIX`, which belongs to the manager: every other job is left
exactly as the operator or the ingester's own interface wrote it, and an entry with a managed
id is replaced as a whole. The job is manual-only (no schedule) and its stored mode is
`append`, the one that cannot delete or update anything, so a run started from the ingester's
own interface can do no harm. The manager chooses the real mode for each run in the run
request.

Two things decide the rest of this module:

* **A document's identity is the job, the source label and the path inside the source.** The
  ingester derives every point id from them, so the job id and the label are stable for a
  collection, and a file that is uploaded again under the same relative path replaces its
  chunks instead of adding a second copy. That is the "update by file name" of the feature,
  with the directory part of the name included.
* **The ingester refuses a catalog with one invalid job as a whole**, and keeps serving the
  previous one. The manager therefore checks the rules the ingester checks across jobs
  (one connection and one embedding model per collection, distinct labels) before it writes,
  and the service rolls its write back if the ingester still does not take it.

Writing is compare-and-swap on the bytes, like `connections.yaml` (see
`app.core.vectordb.catalog_io`). The document is parsed and dumped again, so comments and
blank lines are lost on a write, as they are when the ingester's own form saves a job; the
previous content is kept as `jobs.yaml.bak`, and nothing is written when the entry is
already as it should be.
"""
from __future__ import annotations

import copy
import hashlib
import os
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from app.core.ingest.errors import CatalogRejected
from app.core.vectordb import catalog_io
from app.core.vectordb.jobs_usage import JOBS_RELPATH

MANAGED_PREFIX = "mgr-"

KIND_UPLOAD = "upload"
KIND_FOLDER = "folder"
_KINDS = {KIND_UPLOAD: "u", KIND_FOLDER: "f"}

# The label is the authority of the `source` URI of every point (`local://<label>/<path>`),
# so it is what search results show. Prefixed so it cannot clash with a label an operator
# chose for a job of their own on the same collection.
LABELS = {KIND_UPLOAD: "manager-upload", KIND_FOLDER: "manager-folder"}

# Where the ingester sees the documents folder.
LOCAL_ROOT = "/data/local"

DESCRIPTION = (
    "Managed by the papaia manager (Embedding page). The next run overwrites changes."
)

# The ingester's rules, as far as the manager has to know them to refuse early.
COLLECTION_PATTERN = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}")
_ID_SLUG = re.compile(r"[^a-z0-9]+")

_TMP_NAME = ".jobs.yaml.manager.tmp"
_BACKUP_TMP_NAME = ".jobs.yaml.bak.manager.tmp"
_MAX_ATTEMPTS = 3

# Process-wide, for the manager's own writers. It says nothing about the ingester.
_LOCK = threading.Lock()


def managed_job_id(connection: str, collection: str, kind: str) -> str:
    """The id of the job for one (connection, collection, kind), within the ingester's id rule.

    A readable slug of the collection, a hash of the pair so two collections whose names
    differ only in case or punctuation do not meet, and the kind. At most 47 characters.
    """
    if kind not in _KINDS:
        raise ValueError(f"unknown source kind {kind!r}")
    slug = _ID_SLUG.sub("-", collection.lower()).strip("-")[:32] or "c"
    digest = hashlib.sha256(f"{connection}|{collection}".encode()).hexdigest()[:8]
    return f"{MANAGED_PREFIX}{slug}-{digest}-{_KINDS[kind]}"


def is_managed(job_id: object) -> bool:
    return isinstance(job_id, str) and job_id.startswith(MANAGED_PREFIX)


def kind_of(job_id: str) -> str | None:
    """The kind of source a managed job reads (`upload` or `folder`), or None for any other.

    The kind is the last part of the id (see `managed_job_id`), which is how a run, which
    knows only its job, tells the page what it read.
    """
    if not is_managed(job_id):
        return None
    for kind, suffix in _KINDS.items():
        if job_id.endswith(f"-{suffix}"):
            return kind
    return None


def build_job(
    *,
    kind: str,
    connection: str,
    collection: str,
    model: str,
    source_path: str,
    include: list[str] | None,
    exclude: list[str] | None = None,
) -> dict[str, Any]:
    """The catalog entry for one run, in the key order the ingester's own writer produces."""
    job: dict[str, Any] = {
        "id": managed_job_id(connection, collection, kind),
        "description": DESCRIPTION,
        "source": {"type": "local", "label": LABELS[kind], "path": source_path},
    }
    filters: dict[str, Any] = {}
    if include:
        filters["include"] = list(include)
    if exclude:
        filters["exclude"] = list(exclude)
    if filters:
        job["filters"] = filters
    job["target"] = {"collection": collection, "connection": connection}
    job["mode"] = "append"
    job["embedding"] = {"model": model}
    return job


# ---------------------------------------------------------------------------
# Reading the catalog
# ---------------------------------------------------------------------------


def _enabled(job: dict[str, Any]) -> bool:
    return job.get("enabled", True) is not False


def _collection_of(job: dict[str, Any]) -> str | None:
    target = job.get("target")
    value = target.get("collection") if isinstance(target, dict) else None
    return value if isinstance(value, str) else None


def _connection_of(job: dict[str, Any]) -> str | None:
    target = job.get("target")
    value = target.get("connection") if isinstance(target, dict) else None
    return value if isinstance(value, str) else None


def _label_of(job: dict[str, Any]) -> str | None:
    source = job.get("source")
    value = source.get("label") if isinstance(source, dict) else None
    return value if isinstance(value, str) else None


def _model_of(document: dict[str, Any], job: dict[str, Any]) -> str | None:
    embedding = job.get("embedding")
    model = embedding.get("model") if isinstance(embedding, dict) else None
    if isinstance(model, str) and model:
        return model
    defaults = document.get("defaults")
    inherited = defaults.get("embedding") if isinstance(defaults, dict) else None
    fallback = inherited.get("model") if isinstance(inherited, dict) else None
    return fallback if isinstance(fallback, str) and fallback else None


def _jobs_of(document: dict[str, Any]) -> list[dict[str, Any]]:
    return [job for job in document.get("jobs") or [] if isinstance(job, dict)]


def other_jobs_for(document: dict[str, Any], collection: str, exclude: set[str]) -> list[str]:
    """Ids of the enabled jobs, other than `exclude`, that write to a collection of that name."""
    return [
        str(job.get("id"))
        for job in _jobs_of(document)
        if _enabled(job) and _collection_of(job) == collection and job.get("id") not in exclude
    ]


def catalog_problems(
    document: dict[str, Any], job_id: str, connections: set[str] | None
) -> list[str]:
    """What the ingester would refuse about `job_id` once the document is loaded.

    The cross-job rules of its loader: every job serving one collection uses one connection
    and one embedding model, and two enabled jobs on one collection need different labels.
    `connections` is the set of names in `connections.yaml`, or None when it cannot be read
    (then the connection is not checked here and the ingester has the last word).
    """
    mine = next((job for job in _jobs_of(document) if job.get("id") == job_id), None)
    if mine is None:
        return []
    collection = _collection_of(mine)
    connection = _connection_of(mine)
    problems: list[str] = []
    if connections is not None and connection not in connections:
        problems.append(f"the connection {connection!r} is not in connections.yaml")
    label = _label_of(mine)
    model = _model_of(document, mine)
    for other in _jobs_of(document):
        if other is mine or not _enabled(other) or _collection_of(other) != collection:
            continue
        who = f"the job {other.get('id')!r}"
        if _connection_of(other) != connection:
            problems.append(
                f"{who} writes to the collection {collection!r} on the connection "
                f"{_connection_of(other)!r}; a collection lives in exactly one database"
            )
        other_model = _model_of(document, other)
        if model and other_model and other_model != model:
            problems.append(
                f"{who} embeds {collection!r} with {other_model!r}; a collection records "
                f"exactly one model, and this run uses {model!r}"
            )
        if label is not None and _label_of(other) == label:
            problems.append(f"{who} already uses the source label {label!r} on {collection!r}")
    return problems


# ---------------------------------------------------------------------------
# The file
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class JobsSnapshot:
    """The file as read once."""

    path: Path
    exists: bool
    writable: bool
    raw: bytes
    revision: str
    document: dict[str, Any]
    structural_error: str | None = None


@dataclass(frozen=True)
class Written:
    """What a write changed, so the caller can undo it."""

    before: bytes
    existed: bool
    after: JobsSnapshot
    changed: bool


def _starter() -> dict[str, Any]:
    return {"version": 1, "jobs": []}


def _parse(raw: bytes) -> tuple[dict[str, Any], str | None]:
    """The document, or the reason it cannot be changed."""
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        first = str(exc).strip().splitlines()[0] if str(exc).strip() else "invalid YAML"
        return _starter(), f"invalid YAML: {first}"
    if document is None:
        document = {}
    if not isinstance(document, dict):
        return _starter(), "the top level must be a mapping"
    document.setdefault("version", 1)
    if document["version"] != 1:
        return _starter(), f"unsupported jobs version {document['version']!r}; expected 1"
    jobs = document.get("jobs")
    if jobs is None:
        document["jobs"] = []
    elif not isinstance(jobs, list):
        return _starter(), "jobs must be a list"
    return document, None


class JobsFileRepository:
    """`jobs.yaml` of the RAG module, read and changed on behalf of the manager."""

    def __init__(self, config_dir: str | Path) -> None:
        self._path = Path(config_dir) / JOBS_RELPATH

    @property
    def path(self) -> Path:
        return self._path

    def snapshot(self) -> JobsSnapshot:
        """The file as it is now. Never raises."""
        directory = self._path.parent
        writable = directory.is_dir() and os.access(directory, os.W_OK)
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return JobsSnapshot(self._path, False, writable, b"", "", _starter())
        except OSError as exc:
            return JobsSnapshot(
                self._path,
                True,
                writable,
                b"",
                "",
                _starter(),
                f"the file cannot be read: {exc.strerror or exc}",
            )
        document, problem = _parse(raw)
        return JobsSnapshot(
            self._path, True, writable, raw, catalog_io.revision(raw), document, problem
        )

    def upsert(
        self,
        job: dict[str, Any],
        rules: Callable[[dict[str, Any]], list[str]] | None = None,
    ) -> Written:
        """Make `job` the entry with its id, leaving every other entry as it is.

        `rules` receives the document that would be written and returns what the ingester
        would refuse about it. The change can run more than once, each time on the
        then-current content. Raises `CatalogRejected` for a file that cannot be changed,
        a change the rules refuse and a write that fails or keeps losing the race.
        """
        job_id = str(job["id"])
        with _LOCK:
            for _ in range(_MAX_ATTEMPTS):
                snapshot = self.snapshot()
                if snapshot.structural_error is not None:
                    raise CatalogRejected(
                        f"jobs.yaml cannot be changed: {snapshot.structural_error}"
                    )
                document = copy.deepcopy(snapshot.document)
                jobs = document["jobs"]
                for index, existing in enumerate(jobs):
                    if isinstance(existing, dict) and existing.get("id") == job_id:
                        jobs[index] = copy.deepcopy(job)
                        break
                else:
                    jobs.append(copy.deepcopy(job))

                if snapshot.exists and document == snapshot.document:
                    return Written(snapshot.raw, True, snapshot, changed=False)
                if rules is not None:
                    problems = rules(document)
                    if problems:
                        raise CatalogRejected(
                            "jobs.yaml would not be valid for the ingester: "
                            + "; ".join(problems),
                            tuple(problems),
                        )
                data = catalog_io.dump_document(document).encode("utf-8")
                if self._swap(snapshot, data):
                    return Written(snapshot.raw, snapshot.exists, self.snapshot(), changed=True)
            raise CatalogRejected(
                "jobs.yaml keeps changing while the manager writes it; try again"
            )

    def restore(self, written: Written) -> bool:
        """Put the previous content back, if the file is still the one `written` produced.

        False when somebody else changed the file since (then it is left alone) or when it
        cannot be written.
        """
        if not written.changed:
            return True
        after = written.after
        with _LOCK:
            try:
                if not written.existed:
                    if catalog_io.revision(self._path.read_bytes()) != after.revision:
                        return False
                    self._path.unlink()
                    return True
                return catalog_io.swap(
                    self._path,
                    tmp_name=_TMP_NAME,
                    backup_tmp_name=_BACKUP_TMP_NAME,
                    expected_revision=after.revision,
                    existed=True,
                    previous=after.raw,
                    data=written.before,
                )
            except OSError:
                return False

    def _swap(self, snapshot: JobsSnapshot, data: bytes) -> bool:
        directory = self._path.parent
        if not directory.is_dir():
            raise CatalogRejected(f"{directory} does not exist; the manager does not create it")
        try:
            return catalog_io.swap(
                self._path,
                tmp_name=_TMP_NAME,
                backup_tmp_name=_BACKUP_TMP_NAME,
                expected_revision=snapshot.revision,
                existed=snapshot.exists,
                previous=snapshot.raw,
                data=data,
            )
        except OSError as exc:
            raise CatalogRejected(
                f"{directory} does not accept the change ({exc.strerror or type(exc).__name__}); "
                "the job catalog is read-only here"
            ) from exc
