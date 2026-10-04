"""The ingester's connection store, as the manager reads and writes it.

`connections.yaml` belongs to the ingester, which keeps writing it from its own web
interface. These tests pin what the manager must not break: the entry schema (the
ingester refuses the whole file for an unknown key), the dump format, and the way a
second writer behaves next to it.
"""
from __future__ import annotations

import os
import stat
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.vectordb.errors import (
    ConnectionFileError,
    ConnectionsReadOnlyError,
    InvalidConnectionError,
)
from app.core.vectordb.ingest_file import (
    CONNECTIONS_RELPATH,
    IngestFileRepository,
    dump_document,
    entry_etag,
    find_entry,
    validate_entries,
)

_TOKEN = "enc:1:gAAAAAB-a-token"


@pytest.fixture
def catalog(tmp_path: Path) -> Path:
    directory = tmp_path / CONNECTIONS_RELPATH.parent
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
def repo(tmp_path: Path, catalog: Path) -> IngestFileRepository:
    return IngestFileRepository(tmp_path)


def _write(catalog: Path, text: str) -> Path:
    path = catalog / "connections.yaml"
    path.write_bytes(text.encode("utf-8"))
    return path


def _append(entry: dict[str, Any]) -> Any:
    def mutate(document: dict[str, Any]) -> None:
        document["connections"].append(entry)

    return mutate


def _names(repo: IngestFileRepository) -> list[str]:
    return [entry["name"] for entry in repo.snapshot().entries]


# ---------------------------------------------------------------------------
# The entry schema of the ingester
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        {"name": "a", "url": "http://x"},
        {"name": "a" * 64, "url": "https://qdrant.example:6333/prefix"},
        {"name": "a-b_c1", "url": "http://x", "api_key": _TOKEN},
        {"name": "a", "url": "http://x", "api_key": ""},
        {"name": "a", "url": "http://x", "api_key": None},
        {"name": "0abc", "url": "http://x"},
    ],
)
def test_what_the_ingester_accepts_is_accepted(entry: dict[str, Any]) -> None:
    assert validate_entries([entry]) == []


@pytest.mark.parametrize(
    ("entry", "field"),
    [
        ({"name": "A", "url": "http://x"}, "name"),
        ({"name": "-a", "url": "http://x"}, "name"),
        ({"name": "a" * 65, "url": "http://x"}, "name"),
        ({"name": "", "url": "http://x"}, "name"),
        ({"name": "a b", "url": "http://x"}, "name"),
        ({"name": "a", "url": "ftp://x"}, "url"),
        ({"name": "a", "url": "qdrant:6333"}, "url"),
        ({"name": "a", "url": ""}, "url"),
        ({"name": "a", "url": "http://x", "api_key": "a-literal-key"}, "api_key"),
        # The ingester forbids unknown keys; these are why nothing but three keys is written.
        ({"name": "a", "url": "http://x", "type": "qdrant"}, "type"),
        ({"name": "a", "url": "http://x", "default": True}, "default"),
        ({"url": "http://x"}, "name"),
    ],
)
def test_what_the_ingester_refuses_is_reported(entry: dict[str, Any], field: str) -> None:
    issues = validate_entries([entry])

    assert [issue.field for issue in issues] == [field]


def test_a_duplicate_name_and_a_non_mapping_are_reported() -> None:
    issues = validate_entries(
        [{"name": "a", "url": "http://x"}, {"name": "a", "url": "http://y"}, "text"]
    )

    assert [(issue.name, issue.field) for issue in issues] == [
        ("a", "name"),
        (None, "connections[2]"),
    ]


# ---------------------------------------------------------------------------
# Reading
# ---------------------------------------------------------------------------


def test_a_missing_file_is_an_empty_store_not_an_error(repo: IngestFileRepository) -> None:
    snapshot = repo.snapshot()

    assert not snapshot.exists
    assert snapshot.structural_error is None
    assert snapshot.entries == ()
    assert snapshot.revision == ""
    assert snapshot.document == {"version": 1, "connections": []}


def test_a_file_the_ingester_wrote_is_read(repo: IngestFileRepository, catalog: Path) -> None:
    _write(
        catalog,
        "version: 1\nconnections:\n- name: primary\n  url: http://qdrant:6333\n"
        f"  api_key: {_TOKEN}\n",
    )

    snapshot = repo.snapshot()

    assert snapshot.exists and snapshot.issues == ()
    assert snapshot.entries == (
        {"name": "primary", "url": "http://qdrant:6333", "api_key": _TOKEN},
    )
    assert len(snapshot.revision) == 64


def test_an_empty_file_is_a_store_without_connections(
    repo: IngestFileRepository, catalog: Path
) -> None:
    _write(catalog, "")

    snapshot = repo.snapshot()

    assert snapshot.structural_error is None
    assert snapshot.entries == ()


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("connections: [", "invalid YAML"),
        ("- a\n- b\n", "top level must be a mapping"),
        ("version: 2\nconnections: []\n", "unsupported connections version 2"),
        ("version: 1\nconnections: nope\n", "connections must be a list"),
    ],
)
def test_structural_damage_is_reported_and_blocks_writes(
    repo: IngestFileRepository, catalog: Path, text: str, expected: str
) -> None:
    path = _write(catalog, text)

    assert expected in (repo.snapshot().structural_error or "")
    with pytest.raises(ConnectionFileError, match="cannot be changed"):
        repo.update(_append({"name": "a", "url": "http://x"}))
    assert path.read_bytes() == text.encode("utf-8")


def test_an_entry_the_ingester_would_reject_is_an_issue_not_a_crash(
    repo: IngestFileRepository, catalog: Path
) -> None:
    _write(
        catalog,
        "version: 1\nconnections:\n- name: ok\n  url: http://x\n"
        "- name: bad\n  url: ftp://y\n  api_key: literal\n",
    )

    snapshot = repo.snapshot()

    assert snapshot.structural_error is None
    assert {(issue.name, issue.field) for issue in snapshot.issues} == {
        ("bad", "url"),
        ("bad", "api_key"),
    }


# ---------------------------------------------------------------------------
# Writing: the format
# ---------------------------------------------------------------------------


def test_the_first_write_creates_the_file_in_the_ingesters_format(
    repo: IngestFileRepository, catalog: Path
) -> None:
    repo.update(_append({"name": "default", "url": "http://qdrant:6333", "api_key": _TOKEN}))

    raw = (catalog / "connections.yaml").read_bytes()
    # Exactly what the ingester's own dump options give, with plain LF line ends.
    assert raw == (
        b"version: 1\nconnections:\n- name: default\n  url: http://qdrant:6333\n"
        b"  api_key: " + _TOKEN.encode() + b"\n"
    )
    assert b"\r" not in raw


def test_the_dump_options_are_the_ingesters() -> None:
    document = {"version": 1, "connections": [{"name": "yes", "url": "http://x"}]}

    text = dump_document(document)

    # `yes` is a boolean in YAML 1.1: the dump has to quote it, or the ingester would
    # read a bool and refuse the name.
    assert yaml.safe_load(text)["connections"][0]["name"] == "yes"
    assert text.startswith("version: 1\n")
    assert "{" not in text  # block style, not flow style


def test_a_write_leaves_only_the_file_and_a_backup(
    repo: IngestFileRepository, catalog: Path
) -> None:
    repo.update(_append({"name": "a", "url": "http://x"}))
    previous = (catalog / "connections.yaml").read_bytes()
    repo.update(_append({"name": "b", "url": "http://y"}))

    assert sorted(path.name for path in catalog.iterdir()) == [
        "connections.yaml",
        "connections.yaml.bak",
    ]
    assert (catalog / "connections.yaml.bak").read_bytes() == previous


def test_a_change_touches_one_entry_and_keeps_everything_else(
    repo: IngestFileRepository, catalog: Path
) -> None:
    # Top-level keys the manager does not know, an entry the ingester would refuse, and
    # the order of the list all survive a change to another entry.
    _write(
        catalog,
        "version: 1\nnotes: keep me\nconnections:\n"
        "- name: first\n  url: http://one\n"
        "- name: broken\n  url: ftp://two\n"
        "- name: last\n  url: http://three\n",
    )

    def mutate(document: dict[str, Any]) -> None:
        find_entry(document, "last")["url"] = "http://changed"  # type: ignore[index]

    repo.update(mutate)

    document = yaml.safe_load((catalog / "connections.yaml").read_text(encoding="utf-8"))
    assert document["notes"] == "keep me"
    assert [entry["name"] for entry in document["connections"]] == ["first", "broken", "last"]
    assert document["connections"][1]["url"] == "ftp://two"
    assert document["connections"][2]["url"] == "http://changed"


def test_the_snapshot_is_a_copy(repo: IngestFileRepository) -> None:
    repo.update(_append({"name": "a", "url": "http://x"}))

    snapshot = repo.snapshot()
    snapshot.entries[0]["url"] = "http://tampered"

    assert repo.snapshot().entries[0]["url"] == "http://x"


# ---------------------------------------------------------------------------
# Writing: next to the ingester
# ---------------------------------------------------------------------------


def test_a_change_that_would_make_the_ingester_refuse_the_file_is_not_written(
    repo: IngestFileRepository, catalog: Path
) -> None:
    path = _write(catalog, "version: 1\nconnections: []\n")
    before = path.read_bytes()

    for entry in (
        {"name": "a", "url": "http://x", "type": "qdrant"},
        {"name": "a", "url": "http://x", "api_key": "plain"},
        {"name": "Bad Name", "url": "http://x"},
    ):
        with pytest.raises(InvalidConnectionError):
            repo.update(_append(entry))

    assert path.read_bytes() == before
    assert sorted(p.name for p in catalog.iterdir()) == ["connections.yaml"]


def test_a_problem_that_was_already_there_does_not_lock_the_file(
    repo: IngestFileRepository, catalog: Path
) -> None:
    # After a rotation of the secret every key is unreadable; the repair has to be
    # possible. The ingester itself would refuse any write here.
    _write(
        catalog,
        "version: 1\nconnections:\n- name: old\n  url: ftp://y\n  api_key: literal\n",
    )

    repo.update(_append({"name": "new", "url": "http://x"}))

    assert _names(repo) == ["old", "new"]


def test_a_write_that_lands_in_between_is_not_overwritten(
    repo: IngestFileRepository, catalog: Path
) -> None:
    path = _write(catalog, "version: 1\nconnections:\n- name: mine-first\n  url: http://a\n")
    calls: list[int] = []

    def mutate(document: dict[str, Any]) -> None:
        calls.append(1)
        if len(calls) == 1:
            # The ingester's interface saves while the change is being computed.
            path.write_bytes(
                dump_document(
                    {
                        "version": 1,
                        "connections": [
                            {"name": "mine-first", "url": "http://a"},
                            {"name": "theirs", "url": "http://b"},
                        ],
                    }
                ).encode()
            )
        document["connections"].append({"name": "mine", "url": "http://c"})

    repo.update(mutate)

    assert len(calls) == 2, "the change is computed again from what is on disk"
    assert _names(repo) == ["mine-first", "theirs", "mine"]


def test_a_file_created_in_between_is_not_overwritten_either(
    repo: IngestFileRepository, catalog: Path
) -> None:
    calls: list[int] = []

    def mutate(document: dict[str, Any]) -> None:
        calls.append(1)
        if len(calls) == 1:
            _write(catalog, "version: 1\nconnections:\n- name: theirs\n  url: http://b\n")
        document["connections"].append({"name": "mine", "url": "http://c"})

    repo.update(mutate)

    assert _names(repo) == ["theirs", "mine"]


def test_a_file_that_keeps_changing_is_given_up_on(
    repo: IngestFileRepository, catalog: Path
) -> None:
    path = _write(catalog, "version: 1\nconnections: []\n")
    counter = iter(range(100))

    def mutate(document: dict[str, Any]) -> None:
        path.write_bytes(f"version: 1\nconnections: []\n# {next(counter)}\n".encode())

    with pytest.raises(ConnectionFileError, match="keeps changing"):
        repo.update(mutate)


def test_the_ingesters_staging_file_is_never_touched(
    repo: IngestFileRepository, catalog: Path
) -> None:
    # The ingester stages in `.connections.yaml.tmp`. If the manager used that name, a
    # write of each at the same moment would swap one another's content.
    theirs = catalog / ".connections.yaml.tmp"
    theirs.write_text("the ingester's half-written change", encoding="utf-8")

    repo.update(_append({"name": "a", "url": "http://x"}))

    assert theirs.read_text(encoding="utf-8") == "the ingester's half-written change"
    assert sorted(path.name for path in catalog.iterdir()) == [
        ".connections.yaml.tmp",
        "connections.yaml",
    ]


def test_a_backup_that_cannot_be_written_does_not_stop_the_change(
    repo: IngestFileRepository, catalog: Path
) -> None:
    repo.update(_append({"name": "a", "url": "http://x"}))
    # Whatever is in the way of the backup (here a directory; in a deployment a file
    # that belongs to another user), the change goes through.
    (catalog / "connections.yaml.bak").mkdir()

    repo.update(_append({"name": "b", "url": "http://y"}))

    assert _names(repo) == ["a", "b"]
    assert not (catalog / ".connections.yaml.bak.manager.tmp").exists()


def test_a_directory_that_refuses_the_write_is_read_only_and_loses_nothing(
    repo: IngestFileRepository, catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _write(catalog, "version: 1\nconnections:\n- name: a\n  url: http://x\n")
    before = path.read_bytes()

    def refuse(*_: Any, **__: Any) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", refuse)

    with pytest.raises(ConnectionsReadOnlyError, match="read-only"):
        repo.update(_append({"name": "b", "url": "http://y"}))

    monkeypatch.undo()
    assert path.read_bytes() == before
    assert sorted(p.name for p in catalog.iterdir()) == ["connections.yaml"]


def test_a_missing_catalog_directory_is_never_created(tmp_path: Path) -> None:
    repo = IngestFileRepository(tmp_path)  # no ai/rag/catalog under it

    snapshot = repo.snapshot()
    with pytest.raises(ConnectionsReadOnlyError, match="does not create"):
        repo.update(_append({"name": "a", "url": "http://x"}))

    assert not snapshot.writable
    assert not (tmp_path / "ai").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_the_mode_of_the_file_is_kept(repo: IngestFileRepository, catalog: Path) -> None:
    path = _write(catalog, "version: 1\nconnections: []\n")
    path.chmod(0o664)

    repo.update(_append({"name": "a", "url": "http://x"}))

    assert stat.S_IMODE(path.stat().st_mode) == 0o664


def test_an_etag_follows_the_entry_and_nothing_else() -> None:
    entry = {"name": "a", "url": "http://x", "api_key": _TOKEN}

    assert entry_etag(entry) == entry_etag(dict(reversed(list(entry.items()))))
    assert entry_etag(entry) != entry_etag({**entry, "url": "http://y"})
    assert entry_etag(entry) != entry_etag({"name": "a", "url": "http://x"})
