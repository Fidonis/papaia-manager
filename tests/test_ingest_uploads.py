"""The upload area: staging folders that live only until their files are embedded.

The rules worth pinning are the ones that keep a confidential file from being read by the
wrong person or left behind: one folder per upload and owner, names that cannot leave it,
limits enforced as the bytes arrive, removal that goes folder-first, and the time limit.
"""
from __future__ import annotations

import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from app.core.ingest import uploads
from app.core.ingest.errors import Conflict, InvalidRequest, NotFound, TooLarge
from app.core.ingest.uploads import (
    STATE_EMBEDDING,
    STATE_KEPT,
    STATE_STAGED,
    UploadStore,
    clean_upload_path,
    owner_slug,
)


class Bytes:
    """What an uploaded file offers: an async `read`, in whatever chunks are asked for."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._position = 0

    async def read(self, size: int = -1, /) -> bytes:
        end = len(self._data) if size < 0 else self._position + size
        chunk = self._data[self._position : end]
        self._position += len(chunk)
        return chunk


@pytest.fixture
def docs(tmp_path: Path) -> Path:
    directory = tmp_path / "config" / "ai" / "rag" / "documents"
    directory.mkdir(parents=True)
    return directory


@pytest.fixture
def store(tmp_path: Path, docs: Path) -> UploadStore:
    return UploadStore(
        str(tmp_path / "config"), docs, max_file_bytes=1024, max_batch_bytes=4096
    )


def _new(store: UploadStore, owner: str = "alice", name: str = "") -> uploads.Batch:
    return store.create(owner_sub=f"sub-{owner}", owner_name=owner, name=name)


def _files(root: Path) -> list[Path]:
    return [p for p in root.rglob("*") if p.is_file()]


def _age(store: UploadStore, batch: uploads.Batch, hours: float) -> None:
    """Make an upload look as if it was last touched `hours` ago."""
    old = (datetime.now(UTC) - timedelta(hours=hours)).isoformat(timespec="seconds")
    store._write(replace(batch, updated=old))  # noqa: SLF001 - the clock is the point


# ---------------------------------------------------------------------------
# Creating
# ---------------------------------------------------------------------------


def test_every_upload_gets_a_folder_of_its_own_under_its_owner(
    store: UploadStore, docs: Path
) -> None:
    first = _new(store, "alice")
    second = _new(store, "alice")
    other = _new(store, "bob")

    assert len({first.id, second.id, other.id}) == 3
    for batch in (first, second, other):
        assert (docs / "uploads" / batch.owner / batch.id).is_dir()
    assert first.container_path == f"/data/local/uploads/alice/{first.id}"
    assert other.owner == "bob"


def test_the_record_of_an_upload_is_outside_the_documents_folder(
    store: UploadStore, docs: Path, tmp_path: Path
) -> None:
    batch = _new(store)

    manifest = tmp_path / "config" / "manager" / "ingest" / "batches" / f"{batch.id}.yaml"
    assert manifest.is_file()
    assert not list(docs.rglob("*.yaml")), "the ingester would embed a manifest in its folder"
    assert store.get(batch.id) == batch


@pytest.mark.parametrize(
    ("name", "slug"),
    [("alice", "alice"), ("Alice Smith", "alice-smith"), ("../etc", "etc"), ("", "user")],
)
def test_the_owner_folder_name_has_no_path_characters(name: str, slug: str) -> None:
    assert owner_slug(name) == slug


def test_a_name_with_a_control_character_is_refused(store: UploadStore) -> None:
    with pytest.raises(InvalidRequest):
        _new(store, name="bad\nname")


def test_an_upload_is_private_to_its_owner(store: UploadStore, docs: Path) -> None:
    if os.name == "nt":
        pytest.skip("POSIX modes only")
    batch = _new(store)

    assert (docs / "uploads" / batch.owner / batch.id).stat().st_mode & 0o777 == 0o700
    assert (docs / "uploads").stat().st_mode & 0o777 == 0o700


def test_the_manager_refuses_to_create_uploads_where_it_cannot_write(
    store: UploadStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(Path, "mkdir", refuse)

    from app.core.ingest.errors import IngestUnavailable

    with pytest.raises(IngestUnavailable, match="does not accept uploads"):
        _new(store)


# ---------------------------------------------------------------------------
# Storing files
# ---------------------------------------------------------------------------


async def test_a_file_is_stored_under_its_relative_path(store: UploadStore, docs: Path) -> None:
    batch = _new(store)

    saved = await store.save_file(batch.id, "handbook/chapters/one.md", Bytes(b"# one"))

    target = docs / "uploads" / batch.owner / batch.id / "handbook" / "chapters" / "one.md"
    assert target.read_bytes() == b"# one"
    assert (saved.path, saved.bytes, saved.replaced) == ("handbook/chapters/one.md", 5, False)
    fresh = store.get(batch.id)
    assert (fresh.files, fresh.bytes) == (1, 5)


async def test_the_same_path_again_replaces_the_file_and_corrects_the_totals(
    store: UploadStore,
) -> None:
    batch = _new(store)
    await store.save_file(batch.id, "a.md", Bytes(b"12345"))
    await store.save_file(batch.id, "b.md", Bytes(b"xy"))

    again = await store.save_file(batch.id, "a.md", Bytes(b"123"))

    assert again.replaced
    fresh = store.get(batch.id)
    assert (fresh.files, fresh.bytes) == (2, 5)
    assert store.documents(fresh).resolve("a.md").read_bytes() == b"123"


async def test_a_large_file_is_read_in_pieces(store: UploadStore) -> None:
    big = UploadStore(
        store._manifests.parent.parent.parent,  # noqa: SLF001
        store._docs_root,  # noqa: SLF001
        max_file_bytes=3 * uploads.CHUNK,
        max_batch_bytes=10 * uploads.CHUNK,
    )
    batch = big.create(owner_sub="s", owner_name="alice")
    data = os.urandom(uploads.CHUNK * 2 + 17)

    saved = await big.save_file(batch.id, "big.bin", Bytes(data))

    assert saved.bytes == len(data)
    assert big.documents(big.get(batch.id)).resolve("big.bin").read_bytes() == data


@pytest.mark.parametrize(
    "path",
    [
        "../escape.md",
        "a/../../escape.md",
        "/etc/passwd",
        "a\\b.md",
        "dir/ctl\x07.md",
        "",
        "   ",
        "dir/",
        "what?.md",
        "star*.md",
        "dot.",
        " lead.md",
        "trail.md ",
    ],
)
async def test_a_name_that_cannot_be_stored_safely_is_refused(
    store: UploadStore, docs: Path, path: str
) -> None:
    batch = _new(store)

    with pytest.raises(InvalidRequest):
        await store.save_file(batch.id, path, Bytes(b"x"))

    assert not [p for p in _files(docs) if p.suffix != ".yaml"]


def test_clean_upload_path_keeps_ordinary_names() -> None:
    assert clean_upload_path("Docs/Über uns (final).pdf") == "Docs/Über uns (final).pdf"


async def test_a_file_over_the_limit_is_refused_and_leaves_nothing(
    store: UploadStore, docs: Path
) -> None:
    batch = _new(store)

    with pytest.raises(TooLarge, match="per file"):
        await store.save_file(batch.id, "big.bin", Bytes(b"x" * 2000))

    root = docs / "uploads" / batch.owner / batch.id
    assert list(root.iterdir()) == [], "not even a temporary file"
    assert store.get(batch.id).files == 0


async def test_an_upload_over_its_total_is_refused(store: UploadStore) -> None:
    batch = _new(store)
    for index in range(4):
        await store.save_file(batch.id, f"f{index}.bin", Bytes(b"x" * 1000))

    with pytest.raises(TooLarge, match="upload would be larger"):
        await store.save_file(batch.id, "one-more.bin", Bytes(b"x" * 500))

    assert store.get(batch.id).files == 4


async def test_a_replacement_is_judged_by_the_growth_not_the_whole_file(
    store: UploadStore,
) -> None:
    batch = _new(store)
    for index in range(4):
        await store.save_file(batch.id, f"f{index}.bin", Bytes(b"x" * 1000))

    await store.save_file(batch.id, "f0.bin", Bytes(b"y" * 1024))

    assert store.get(batch.id).bytes == 4024


async def test_a_file_cannot_be_stored_through_a_folder_that_is_a_file(
    store: UploadStore,
) -> None:
    batch = _new(store)
    await store.save_file(batch.id, "a", Bytes(b"x"))

    with pytest.raises(InvalidRequest, match="is a file"):
        await store.save_file(batch.id, "a/b.md", Bytes(b"x"))


async def test_a_folder_is_not_replaced_by_a_file(store: UploadStore) -> None:
    batch = _new(store)
    await store.save_file(batch.id, "a/b.md", Bytes(b"x"))

    with pytest.raises(InvalidRequest, match="folder or a link"):
        await store.save_file(batch.id, "a", Bytes(b"x"))


async def test_a_link_inside_an_upload_is_not_followed(
    store: UploadStore, docs: Path, tmp_path: Path
) -> None:
    batch = _new(store)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = docs / "uploads" / batch.owner / batch.id / "out"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("this platform does not allow creating symbolic links")

    with pytest.raises(InvalidRequest, match="Symbolic links"):
        await store.save_file(batch.id, "out/planted.md", Bytes(b"x"))

    assert not (outside / "planted.md").exists()


async def test_an_unknown_upload_and_a_malformed_id_are_not_found(store: UploadStore) -> None:
    with pytest.raises(NotFound):
        await store.save_file("20261005-100000-deadbeef", "a.md", Bytes(b"x"))
    with pytest.raises(NotFound):
        store.get("../../etc")


async def test_files_cannot_be_added_while_a_run_reads_the_upload(store: UploadStore) -> None:
    batch = _new(store)
    await store.save_file(batch.id, "a.md", Bytes(b"x"))
    store.mark_embedding(
        batch.id, run_id="r1", job_id="j", collection="c", connection="default", mode="add"
    )

    with pytest.raises(Conflict, match="being embedded"):
        await store.save_file(batch.id, "b.md", Bytes(b"x"))


async def test_adding_a_file_to_a_kept_upload_makes_it_ready_again(store: UploadStore) -> None:
    batch = _new(store)
    await store.save_file(batch.id, "a.md", Bytes(b"x"))
    store.mark_kept(batch.id, "1 document(s) failed")

    await store.save_file(batch.id, "b.md", Bytes(b"x"))

    fresh = store.get(batch.id)
    assert (fresh.state, fresh.note) == (STATE_STAGED, None)


# ---------------------------------------------------------------------------
# The life of an upload
# ---------------------------------------------------------------------------


async def test_a_run_is_recorded_on_the_upload(store: UploadStore) -> None:
    batch = _new(store)

    marked = store.mark_embedding(
        batch.id, run_id="r1", job_id="mgr-x", collection="kb", connection="default", mode="add"
    )

    assert (marked.state, marked.run_id, marked.collection) == (STATE_EMBEDDING, "r1", "kb")
    assert store.get(batch.id).job_id == "mgr-x"


async def test_removing_an_upload_deletes_the_files_then_the_record(
    store: UploadStore, docs: Path, tmp_path: Path
) -> None:
    batch = _new(store)
    await store.save_file(batch.id, "a/b.md", Bytes(b"secret"))

    store.remove(batch.id)

    assert not (docs / "uploads" / batch.owner).exists(), "the empty owner folder goes too"
    assert not list((tmp_path / "config" / "manager" / "ingest" / "batches").glob("*.yaml"))
    with pytest.raises(NotFound):
        store.get(batch.id)


async def test_removing_one_upload_leaves_another_of_the_same_owner(
    store: UploadStore, docs: Path
) -> None:
    first = _new(store)
    second = _new(store)
    await store.save_file(second.id, "keep.md", Bytes(b"x"))

    store.remove(first.id)

    assert (docs / "uploads" / second.owner / second.id / "keep.md").is_file()
    assert store.get(second.id)


async def test_a_failed_removal_keeps_the_record_so_it_is_found_again(
    store: UploadStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.core.ingest.errors import IngestUnavailable

    batch = _new(store)
    await store.save_file(batch.id, "a.md", Bytes(b"x"))

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(uploads.shutil, "rmtree", refuse)

    with pytest.raises(IngestUnavailable, match="could not be removed"):
        store.remove(batch.id)

    assert store.get(batch.id).id == batch.id


async def test_an_upload_cannot_be_discarded_while_a_run_reads_it(store: UploadStore) -> None:
    batch = _new(store)
    store.mark_embedding(
        batch.id, run_id="r1", job_id="j", collection="c", connection="default", mode="add"
    )

    with pytest.raises(Conflict, match="abort the run"):
        store.discard(batch.id)

    store.mark_kept(batch.id, "stopped")
    store.discard(batch.id)
    with pytest.raises(NotFound):
        store.get(batch.id)


def test_the_uploads_are_listed_newest_first_and_a_broken_record_is_skipped(
    store: UploadStore, tmp_path: Path
) -> None:
    first = _new(store)
    second = _new(store)
    batches = tmp_path / "config" / "manager" / "ingest" / "batches"
    broken = batches / "20260101-000000-00000000.yaml"
    broken.write_text("not: [valid", encoding="utf-8")

    listed = store.batches()

    assert {batch.id for batch in listed} == {first.id, second.id}
    assert listed == sorted(listed, key=lambda b: b.created, reverse=True)


# ---------------------------------------------------------------------------
# The time limit
# ---------------------------------------------------------------------------


def test_an_upload_expires_after_the_limit_whatever_its_state(store: UploadStore) -> None:
    staged = _new(store, "alice")
    kept = _new(store, "bob")
    store.mark_kept(kept.id, "failed")
    fresh = _new(store, "carol")
    _age(store, store.get(staged.id), 25)
    _age(store, store.get(kept.id), 30)

    expired = {batch.id for batch in store.expired(24)}

    assert expired == {staged.id, kept.id}
    assert fresh.id not in expired


def test_a_run_that_is_still_working_is_not_expired(store: UploadStore) -> None:
    batch = _new(store)
    store.mark_embedding(
        batch.id, run_id="r1", job_id="j", collection="c", connection="default", mode="add"
    )
    _age(store, store.get(batch.id), 100)

    assert store.expired(24, keep=frozenset({batch.id})) == []
    assert [b.id for b in store.expired(24)] == [batch.id]


def test_a_folder_that_no_record_knows_is_found_once_it_is_old(
    store: UploadStore, docs: Path
) -> None:
    lost = docs / "uploads" / "alice" / "20260101-000000-aaaaaaaa"
    lost.mkdir(parents=True)
    (lost / "orphan.pdf").write_bytes(b"x")
    recent = docs / "uploads" / "alice" / "20261005-100000-bbbbbbbb"
    recent.mkdir()
    old = datetime.now(UTC).timestamp() - 48 * 3600
    os.utime(lost, (old, old))
    not_ours = docs / "uploads" / "alice" / "my-notes"
    not_ours.mkdir()
    os.utime(not_ours, (old, old))

    assert store.orphans(24) == [lost]
    store.remove_orphan(lost)
    assert not lost.exists()
    assert recent.is_dir() and not_ours.is_dir(), "only folders of the manager's own shape"


async def test_a_record_whose_folder_is_gone_is_dropped(store: UploadStore) -> None:
    batch = _new(store)
    await store.save_file(batch.id, "a.md", Bytes(b"x"))
    uploads.shutil.rmtree(store.directory(batch))

    gone = store.dropped_manifests()
    assert [b.id for b in gone] == [batch.id]
    store.drop_manifest(batch.id)
    assert store.batches() == []


def test_a_kept_upload_remembers_why(store: UploadStore) -> None:
    batch = _new(store)

    kept = store.mark_kept(batch.id, "x" * 1000)

    assert kept.state == STATE_KEPT
    assert len(kept.note or "") == 300
