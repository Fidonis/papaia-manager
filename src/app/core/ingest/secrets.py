"""The credentials of remote sources: `ai/rag/catalog/secrets.yaml`, encrypted, write-only.

A job refers to a credential as `${env:QI_SECRET_<NAME>}`. The reference has two homes: the
ingester's process environment (`ai/rag/.env` is its `env_file`, so a variable there is part
of it) and this file, which the ingester reads when it needs the value. The file exists so
that a credential can be added from a page: the environment is read when the container is
created, so a variable in `.env` needs a restart, and `.env` is also the environment file of
other services.

Format, shared with the ingester (`catalog/secret_store.py`)::

    version: 1
    secrets:
      - name: QI_SECRET_S3_KEY
        value: enc:1:gAAAA...

Values are Fernet tokens keyed from `QI_CONNECTIONS_SECRET`, like the api-keys in
`connections.yaml`. What that protects, and what it does not: the key is in the same `.env`
next to the file and a backup carries both, so this keeps a credential out of casual sight
(a copy of the file, a diff, a screenshot, a log) and not out of the reach of someone who can
read both files.

Consequences worth knowing when touching this:

* **A value is never an output.** The page lists names, where each is used and whether it can
  be read. Responses, the audit log and every exception text are free of values and tokens.
* **Names that exist in the environment are not shadowed.** The ingester answers from the
  environment first, so a stored value of the same name would be ignored while looking set.
  Setting such a name is refused.
* **No backup copy.** `catalog_io.swap` keeps the previous content as `<name>.bak` for the
  other catalog files; here that would keep a deleted credential on disk, so it is off.
* **A change is compare-and-swap**, like the other two files, and keeps going over problems
  that were already there: after a rotation of `QI_CONNECTIONS_SECRET` every value is
  unreadable, and storing the credential again has to stay possible.
"""
from __future__ import annotations

import os
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from app.core.ingest.errors import CatalogRejected
from app.core.vectordb import catalog_io
from app.core.vectordb.crypto import ENC_PREFIX, SecretError, decrypt, encrypt

SECRETS_RELPATH = Path("ai") / "rag" / "catalog" / "secrets.yaml"
NAME_PREFIX = "QI_SECRET_"
_NAME_RE = re.compile(r"^QI_SECRET_[A-Z0-9_]+$")
_SUFFIX_RE = re.compile(r"^[A-Z0-9_]{1,80}$")
_REF_RE = re.compile(r"^\$\{env:(QI_SECRET_[A-Z0-9_]+)\}$")

_TMP_NAME = ".secrets.yaml.manager.tmp"
_BACKUP_TMP_NAME = ".secrets.yaml.bak.manager.tmp"
_MAX_ATTEMPTS = 3
MAX_VALUE_BYTES = 64 * 1024  # a PEM key or a service-account JSON, with room to spare

# Process-wide, for the manager's own writers.
_LOCK = threading.Lock()


class SecretProblem(ValueError):  # noqa: N818 - reads as the answer it becomes: a 422
    """The name or the value cannot be stored."""


class SecretConflict(Exception):  # noqa: N818
    """The change clashes with what exists: a name in the environment, a credential in use."""

    def __init__(self, message: str, used_by: tuple[str, ...] = ()) -> None:
        super().__init__(message)
        self.used_by = used_by


def full_name(name: str) -> str:
    """`S3_KEY` or `QI_SECRET_S3_KEY` as the stored name `QI_SECRET_S3_KEY`."""
    text = name.strip().upper().replace("-", "_").replace(" ", "_")
    if not text.startswith(NAME_PREFIX):
        text = NAME_PREFIX + text
    if not _NAME_RE.match(text) or not _SUFFIX_RE.match(text[len(NAME_PREFIX) :]):
        raise SecretProblem(
            "Use capital letters, digits and '_' for the name (for example S3_KEY or "
            "NEXTCLOUD_PASSWORD)."
        )
    return text


def is_credential_name(name: str) -> bool:
    return bool(_NAME_RE.match(name))


def _first_line(exc: Exception) -> str:
    text = str(exc).strip()
    return text.splitlines()[0] if text else "invalid YAML"


@dataclass(frozen=True)
class SecretsSnapshot:
    """The file as read once. Holds names, never values."""

    path: Path
    exists: bool
    writable: bool
    raw: bytes
    revision: str
    entries: tuple[dict[str, Any], ...] = field(default=())
    structural_error: str | None = None

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(str(entry.get("name")) for entry in self.entries)


def _parse(raw: bytes) -> tuple[dict[str, Any], str | None]:
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        return {"version": 1, "secrets": []}, f"invalid YAML: {_first_line(exc)}"
    if document is None:
        document = {}
    if not isinstance(document, dict):
        return {"version": 1, "secrets": []}, "the top level must be a mapping"
    document.setdefault("version", 1)
    if document["version"] != 1:
        return (
            {"version": 1, "secrets": []},
            f"unsupported secrets version {document['version']!r}; expected 1",
        )
    secrets = document.get("secrets")
    if secrets is None:
        document["secrets"] = []
    elif not isinstance(secrets, list):
        return {"version": 1, "secrets": []}, "secrets must be a list"
    return document, None


def _entry_problems(document: Mapping[str, Any]) -> list[str]:
    """What the ingester would refuse about the file as a whole (it refuses all of it)."""
    problems: list[str] = []
    seen: set[str] = set()
    for index, entry in enumerate(document.get("secrets") or []):
        if not isinstance(entry, dict) or set(entry) != {"name", "value"}:
            problems.append(f"secrets[{index}] must hold exactly a name and a value")
            continue
        name, value = entry.get("name"), entry.get("value")
        if not isinstance(name, str) or not _NAME_RE.match(name):
            problems.append(f"secrets[{index}]: the name must look like QI_SECRET_<NAME>")
        elif name in seen:
            problems.append(f"duplicate secret name {name!r}")
        else:
            seen.add(name)
        if not isinstance(value, str) or not value.startswith(ENC_PREFIX):
            problems.append(f"secrets[{index}]: the value must be an {ENC_PREFIX} token")
    return problems


class SecretsRepository:
    """`secrets.yaml` of the RAG module, read and changed on behalf of the manager."""

    def __init__(self, config_dir: str | Path, key: str) -> None:
        self._path = Path(config_dir) / SECRETS_RELPATH
        self._key = key

    @property
    def path(self) -> Path:
        return self._path

    @property
    def has_key(self) -> bool:
        return bool(self._key)

    def snapshot(self) -> SecretsSnapshot:
        """The file as it is now. Never raises."""
        directory = self._path.parent
        writable = directory.is_dir() and _writable(directory)
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return SecretsSnapshot(self._path, False, writable, b"", "")
        except OSError as exc:
            return SecretsSnapshot(
                self._path, True, writable, b"", "",
                structural_error=f"the file cannot be read: {exc.strerror or exc}",
            )
        document, problem = _parse(raw)
        if problem is None:
            broken = _entry_problems(document)
            problem = broken[0] if broken else None
        entries = tuple(e for e in document["secrets"] if isinstance(e, dict))
        return SecretsSnapshot(
            self._path, True, writable, raw, catalog_io.revision(raw), entries, problem
        )

    def readable(self, snapshot: SecretsSnapshot | None = None) -> dict[str, bool]:
        """Per stored name, whether it decrypts with the current key."""
        snap = snapshot or self.snapshot()
        out: dict[str, bool] = {}
        for entry in snap.entries:
            name, value = str(entry.get("name")), entry.get("value")
            try:
                decrypt(str(value), self._key)
                out[name] = True
            except SecretError:
                out[name] = False
        return out

    # ── writing ─────────────────────────────────────────────────────────────

    def set(self, name: str, value: str) -> bool:
        """Store (or replace) a credential. Returns whether the name was new."""
        if not self._key:
            raise CatalogRejected(
                "QI_CONNECTIONS_SECRET is not set in ai/rag/.env, so credentials cannot be "
                "stored encrypted."
            )
        if not value or not value.strip():
            raise SecretProblem("The value is empty.")
        if len(value.encode("utf-8")) > MAX_VALUE_BYTES:
            raise SecretProblem("The value is larger than 64 KB.")
        token = encrypt(value, self._key)
        created = False

        def mutate(document: dict[str, Any]) -> None:
            nonlocal created
            entries = document["secrets"]
            for entry in entries:
                if isinstance(entry, dict) and entry.get("name") == name:
                    entry["value"] = token
                    created = False
                    return
            entries.append({"name": name, "value": token})
            created = True

        self._update(mutate)
        return created

    def delete(self, name: str) -> bool:
        """Remove a credential. False when it was not stored."""
        removed = False

        def mutate(document: dict[str, Any]) -> None:
            nonlocal removed
            before = len(document["secrets"])
            document["secrets"] = [
                entry for entry in document["secrets"]
                if not (isinstance(entry, dict) and entry.get("name") == name)
            ]
            removed = len(document["secrets"]) != before

        self._update(mutate)
        return removed

    def _update(self, mutate: Callable[[dict[str, Any]], None]) -> None:
        with _LOCK:
            for _ in range(_MAX_ATTEMPTS):
                snapshot = self.snapshot()
                if snapshot.structural_error is not None:
                    raise CatalogRejected(
                        f"secrets.yaml cannot be changed: {snapshot.structural_error}"
                    )
                document, _ = _parse(snapshot.raw) if snapshot.exists else (
                    {"version": 1, "secrets": []}, None
                )
                mutate(document)
                data = catalog_io.dump_document(document).encode("utf-8")
                if snapshot.exists and data == snapshot.raw:
                    return
                if self._swap(snapshot, data):
                    return
            raise CatalogRejected(
                "secrets.yaml keeps changing while the manager writes it; try again"
            )

    def _swap(self, snapshot: SecretsSnapshot, data: bytes) -> bool:
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
                backup=False,
            )
        except OSError as exc:
            raise CatalogRejected(
                f"{directory} does not accept the change ({exc.strerror or type(exc).__name__}); "
                "the catalog folder is read-only here"
            ) from exc


def _writable(directory: Path) -> bool:
    return os.access(directory, os.W_OK)


def references(document: Mapping[str, Any]) -> dict[str, list[str]]:
    """Which jobs refer to which credential: name -> job ids, in file order.

    Every job counts, disabled ones and ones the ingester would reject included: they would
    use the credential again once fixed, and deleting a credential on a guess is the one
    mistake that cannot be undone from the page.
    """
    used: dict[str, list[str]] = {}
    jobs = document.get("jobs")
    if not isinstance(jobs, list):
        return used
    for index, job in enumerate(jobs):
        if not isinstance(job, dict):
            continue
        source = job.get("source")
        if not isinstance(source, dict):
            continue
        job_id = job.get("id")
        label = job_id if isinstance(job_id, str) and job_id else f"jobs[{index}]"
        for value in source.values():
            if isinstance(value, str):
                match = _REF_RE.match(value.strip())
                if match:
                    used.setdefault(match.group(1), [])
                    if label not in used[match.group(1)]:
                        used[match.group(1)].append(label)
    return used
