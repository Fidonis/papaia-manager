"""The managed jobs in `jobs.yaml`, and the way the manager writes that file.

The file is shared with the ingester's own interface and with whatever an operator wrote by
hand, and the ingester refuses a catalog with one invalid job as a whole. So the tests pin that
the manager touches only its own entries, never writes what the ingester would refuse, and can
take its change back.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.core.ingest import catalog
from app.core.ingest.catalog import (
    KIND_FOLDER,
    KIND_UPLOAD,
    JobsFileRepository,
    build_job,
    catalog_problems,
    managed_job_id,
    other_jobs_for,
)
from app.core.ingest.errors import CatalogRejected
from app.core.vectordb import catalog_io

# The ingester's rules (`catalog/schema.py`), copied here as the contract of an id and a name.
_SLUG = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_COLLECTION = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]{0,127}$")


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    (directory / "ai" / "rag" / "catalog").mkdir(parents=True)
    return directory


@pytest.fixture
def repo(config_dir: Path) -> JobsFileRepository:
    return JobsFileRepository(config_dir)


def _job(collection: str = "kb", **changes: Any) -> dict[str, Any]:
    job = build_job(
        kind=KIND_UPLOAD,
        connection="default",
        collection=collection,
        model="nomic-embed-text",
        source_path="/data/local/uploads/alice/20261005-100000-aaaaaaaa",
        include=None,
    )
    job.update(changes)
    return job


def _read(repo: JobsFileRepository) -> dict[str, Any]:
    document = yaml.safe_load(repo.path.read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _foreign(job_id: str = "nightly", collection: str = "other", **changes: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "id": job_id,
        "source": {"type": "s3", "label": "bucket", "bucket": "b"},
        "target": {"collection": collection, "connection": "default"},
        "mode": "upsert",
        "schedule": {"cron": "0 2 * * *"},
    }
    job.update(changes)
    return job


# ---------------------------------------------------------------------------
# Ids and entries
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("collection", ["kb", "Finance.2026", "a_b-c", "UPPER", "x" * 128, "..."])
def test_a_managed_id_obeys_the_ingesters_id_rule(collection: str) -> None:
    for kind in (KIND_UPLOAD, KIND_FOLDER):
        job_id = managed_job_id("default", collection, kind)

        assert _SLUG.fullmatch(job_id), job_id
        assert job_id.startswith(catalog.MANAGED_PREFIX)
        assert len(job_id) <= 64


def test_the_ids_of_names_that_differ_only_in_case_or_punctuation_do_not_meet() -> None:
    ids = {
        managed_job_id("default", name, KIND_UPLOAD)
        for name in ("Kb", "kb", "k-b", "k.b", "k_b")
    }

    assert len(ids) == 5


def test_a_connection_is_part_of_the_id_and_the_kind_tells_the_jobs_apart() -> None:
    assert managed_job_id("a", "kb", KIND_UPLOAD) != managed_job_id("b", "kb", KIND_UPLOAD)
    assert managed_job_id("a", "kb", KIND_UPLOAD) != managed_job_id("a", "kb", KIND_FOLDER)
    assert managed_job_id("a", "kb", KIND_UPLOAD) == managed_job_id("a", "kb", KIND_UPLOAD)


@pytest.mark.parametrize("kind", [KIND_UPLOAD, KIND_FOLDER])
def test_the_kind_of_a_managed_job_is_read_back_from_its_id(kind: str) -> None:
    assert catalog.kind_of(managed_job_id("default", "kb", kind)) == kind


@pytest.mark.parametrize("job_id", ["nightly", "mgr-kb-2e141ff7", "mgr-kb-2e141ff7-x", ""])
def test_a_job_of_somebody_else_has_no_kind(job_id: str) -> None:
    assert catalog.kind_of(job_id) is None


def test_an_unknown_kind_is_a_programming_error() -> None:
    with pytest.raises(ValueError):
        managed_job_id("default", "kb", "ftp")


def test_a_managed_job_is_manual_and_cannot_delete_when_started_from_elsewhere() -> None:
    job = build_job(
        kind=KIND_FOLDER,
        connection="default",
        collection="kb",
        model="m",
        source_path="/data/local",
        include=["a/**", "b.md"],
        exclude=["uploads/**"],
    )

    assert job["mode"] == "append"
    assert "schedule" not in job and "enabled" not in job
    assert job["source"] == {"type": "local", "label": "manager-folder", "path": "/data/local"}
    assert job["filters"] == {"include": ["a/**", "b.md"], "exclude": ["uploads/**"]}
    assert job["target"] == {"collection": "kb", "connection": "default"}
    assert job["embedding"] == {"model": "m"}
    assert list(job)[:3] == ["id", "description", "source"]


def test_no_filters_are_written_when_nothing_is_selected_or_excluded() -> None:
    assert "filters" not in _job()


def test_the_label_of_each_kind_cannot_clash_with_an_ordinary_one() -> None:
    assert catalog.LABELS == {"upload": "manager-upload", "folder": "manager-folder"}


def test_a_collection_name_the_ingester_would_refuse_is_recognised() -> None:
    assert catalog.COLLECTION_PATTERN.fullmatch("kb.2026_a-b")
    assert not catalog.COLLECTION_PATTERN.fullmatch("x" * 129)
    assert not catalog.COLLECTION_PATTERN.fullmatch("_hidden")
    assert not catalog.COLLECTION_PATTERN.fullmatch("a b")
    assert _COLLECTION.pattern.rstrip("$") == "^" + catalog.COLLECTION_PATTERN.pattern


# ---------------------------------------------------------------------------
# What the ingester would refuse
# ---------------------------------------------------------------------------


def _doc(*jobs: dict[str, Any], **top: Any) -> dict[str, Any]:
    return {"version": 1, "jobs": list(jobs), **top}


def test_a_lone_job_has_no_problems() -> None:
    job = _job()

    assert catalog_problems(_doc(job), job["id"], {"default"}) == []


def test_a_connection_that_does_not_exist_is_a_problem_but_an_unreadable_file_is_not() -> None:
    job = _job()

    assert catalog_problems(_doc(job), job["id"], {"other"}) == [
        "the connection 'default' is not in connections.yaml"
    ]
    assert catalog_problems(_doc(job), job["id"], None) == []


def test_a_collection_lives_in_one_database() -> None:
    mine = _job("kb")
    theirs = _foreign("hr", "kb", target={"collection": "kb", "connection": "archive"})

    problems = catalog_problems(_doc(mine, theirs), mine["id"], None)

    assert len(problems) == 1 and "exactly one database" in problems[0]


def test_a_collection_records_one_model() -> None:
    mine = _job("kb")
    theirs = _foreign("hr", "kb", embedding={"model": "bge-m3"})

    problems = catalog_problems(_doc(mine, theirs), mine["id"], None)

    assert len(problems) == 1 and "'bge-m3'" in problems[0] and "'nomic-embed-text'" in problems[0]


def test_the_model_of_another_job_may_come_from_the_defaults() -> None:
    mine = _job("kb")
    theirs = _foreign("hr", "kb")

    clash = catalog_problems(
        _doc(mine, theirs, defaults={"embedding": {"model": "bge-m3"}}), mine["id"], None
    )
    same = catalog_problems(
        _doc(mine, theirs, defaults={"embedding": {"model": "nomic-embed-text"}}), mine["id"], None
    )

    assert len(clash) == 1
    assert same == []


def test_two_enabled_jobs_on_a_collection_need_different_labels() -> None:
    mine = _job("kb")
    theirs = _foreign("hr", "kb", source={"type": "local", "label": "manager-upload", "path": "/x"})

    problems = catalog_problems(_doc(mine, theirs), mine["id"], None)

    assert len(problems) == 1 and "source label 'manager-upload'" in problems[0]


def test_a_disabled_job_and_a_job_on_another_collection_are_not_checked() -> None:
    mine = _job("kb")
    disabled = _foreign("old", "kb", enabled=False, embedding={"model": "bge-m3"})
    elsewhere = _foreign("hr", "other", embedding={"model": "bge-m3"})

    assert catalog_problems(_doc(mine, disabled, elsewhere), mine["id"], None) == []


def test_other_jobs_for_a_collection_leave_out_the_given_ids_and_disabled_ones() -> None:
    document = _doc(
        _job("kb"),
        _foreign("a", "kb"),
        _foreign("b", "kb", enabled=False),
        _foreign("c", "other"),
    )

    assert other_jobs_for(document, "kb", {managed_job_id("default", "kb", KIND_UPLOAD)}) == ["a"]


# ---------------------------------------------------------------------------
# Writing the file
# ---------------------------------------------------------------------------


def test_the_first_write_creates_the_file(repo: JobsFileRepository) -> None:
    written = repo.upsert(_job())

    assert written.changed and not written.existed
    document = _read(repo)
    assert document["version"] == 1
    assert [job["id"] for job in document["jobs"]] == [_job()["id"]]


def test_everything_else_in_the_file_stays_as_it_is(repo: JobsFileRepository) -> None:
    repo.path.write_text(
        yaml.safe_dump(
            {
                "version": 1,
                "defaults": {
                    "embedding": {"model": "nomic-embed-text"},
                    "chunking": {"words": 300},
                },
                "future_key": {"kept": True},
                "jobs": [_foreign("nightly"), _foreign("weekly", "third", mode="full")],
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    repo.upsert(_job())

    document = _read(repo)
    assert document["defaults"] == {
        "embedding": {"model": "nomic-embed-text"},
        "chunking": {"words": 300},
    }
    assert document["future_key"] == {"kept": True}
    assert [job["id"] for job in document["jobs"]] == ["nightly", "weekly", _job()["id"]]
    assert document["jobs"][0] == _foreign("nightly")
    assert document["jobs"][1] == _foreign("weekly", "third", mode="full")


def test_an_entry_with_a_managed_id_is_replaced_in_place(repo: JobsFileRepository) -> None:
    repo.upsert(_foreign("first"))
    repo.upsert(_job(source={"type": "local", "label": "manager-upload", "path": "/data/local/a"}))
    repo.upsert(_foreign("last"))

    repo.upsert(_job(source={"type": "local", "label": "manager-upload", "path": "/data/local/b"}))

    jobs = _read(repo)["jobs"]
    assert [job["id"] for job in jobs] == ["first", _job()["id"], "last"]
    assert jobs[1]["source"]["path"] == "/data/local/b"


def test_a_job_that_is_already_as_it_should_be_is_not_written_again(
    repo: JobsFileRepository,
) -> None:
    repo.upsert(_job())
    before = repo.path.read_bytes()

    written = repo.upsert(_job())

    assert not written.changed
    assert repo.path.read_bytes() == before
    assert not (repo.path.parent / "jobs.yaml.bak").exists()


def test_a_change_keeps_the_previous_content_as_a_backup(repo: JobsFileRepository) -> None:
    repo.upsert(_foreign("nightly"))
    first = repo.path.read_bytes()

    repo.upsert(_job())

    assert (repo.path.parent / "jobs.yaml.bak").read_bytes() == first


def test_a_change_the_ingester_would_refuse_writes_nothing(repo: JobsFileRepository) -> None:
    repo.upsert(_foreign("nightly"))
    before = repo.path.read_bytes()

    with pytest.raises(CatalogRejected, match="would not be valid") as caught:
        repo.upsert(_job(), lambda document: ["the model clashes"])

    assert repo.path.read_bytes() == before
    assert caught.value.problems == ("the model clashes",)


def test_the_rules_see_the_document_that_would_be_written(repo: JobsFileRepository) -> None:
    repo.upsert(_foreign("nightly"))
    seen: list[list[str]] = []

    repo.upsert(_job(), lambda document: seen.append([j["id"] for j in document["jobs"]]) or [])

    assert seen == [["nightly", _job()["id"]]]


@pytest.mark.parametrize(
    ("content", "reason"),
    [
        ("jobs: [unclosed", "invalid YAML"),
        ("- just\n- a list\n", "top level must be a mapping"),
        ("version: 2\njobs: []\n", "unsupported jobs version"),
        ("version: 1\njobs: nope\n", "jobs must be a list"),
    ],
)
def test_a_file_that_cannot_be_read_safely_is_not_overwritten(
    repo: JobsFileRepository, content: str, reason: str
) -> None:
    repo.path.write_text(content, encoding="utf-8")

    with pytest.raises(CatalogRejected, match=reason):
        repo.upsert(_job())

    assert repo.path.read_text(encoding="utf-8") == content


def test_a_change_made_meanwhile_is_not_lost(
    repo: JobsFileRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.upsert(_foreign("nightly"))
    real_swap = catalog_io.swap
    calls = {"count": 0}

    def swap_after_a_concurrent_write(*args: Any, **kwargs: Any) -> bool:
        calls["count"] += 1
        if calls["count"] == 1:
            document = _read(repo)
            document["jobs"].append(_foreign("from-the-ingesters-ui"))
            repo.path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")
        return real_swap(*args, **kwargs)

    monkeypatch.setattr(catalog_io, "swap", swap_after_a_concurrent_write)

    written = repo.upsert(_job())

    assert written.changed
    assert calls["count"] == 2
    assert [job["id"] for job in _read(repo)["jobs"]] == [
        "nightly",
        "from-the-ingesters-ui",
        _job()["id"],
    ]


def test_a_file_that_keeps_changing_is_given_up_on(
    repo: JobsFileRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo.upsert(_foreign("nightly"))
    monkeypatch.setattr(catalog_io, "swap", lambda *a, **k: False)

    with pytest.raises(CatalogRejected, match="keeps changing"):
        repo.upsert(_job())


def test_a_catalog_folder_that_does_not_exist_is_not_created(tmp_path: Path) -> None:
    repo = JobsFileRepository(tmp_path / "no-such-config")

    with pytest.raises(CatalogRejected, match="does not exist"):
        repo.upsert(_job())

    assert not (tmp_path / "no-such-config").exists()


def test_a_read_only_catalog_is_reported_as_such(
    repo: JobsFileRepository, monkeypatch: pytest.MonkeyPatch
) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(catalog_io.os, "replace", refuse)

    with pytest.raises(CatalogRejected, match="read-only"):
        repo.upsert(_job())

    assert not repo.path.exists()
    assert not list(repo.path.parent.glob(".jobs.yaml*")), "no staging file is left behind"


# ---------------------------------------------------------------------------
# Taking a change back
# ---------------------------------------------------------------------------


def test_a_change_can_be_rolled_back(repo: JobsFileRepository) -> None:
    repo.upsert(_foreign("nightly"))
    before = repo.path.read_bytes()
    written = repo.upsert(_job())

    assert repo.restore(written)

    assert repo.path.read_bytes() == before


def test_a_first_write_is_rolled_back_by_removing_the_file(repo: JobsFileRepository) -> None:
    written = repo.upsert(_job())

    assert repo.restore(written)

    assert not repo.path.exists()


def test_a_rollback_never_overwrites_somebody_elses_change(repo: JobsFileRepository) -> None:
    repo.upsert(_foreign("nightly"))
    written = repo.upsert(_job())
    document = _read(repo)
    document["jobs"].append(_foreign("added-later"))
    repo.path.write_text(yaml.safe_dump(document, sort_keys=False), encoding="utf-8")

    assert not repo.restore(written)

    assert "added-later" in repo.path.read_text(encoding="utf-8")


def test_rolling_back_a_write_that_changed_nothing_is_a_no_op(repo: JobsFileRepository) -> None:
    repo.upsert(_job())
    written = repo.upsert(_job())

    assert repo.restore(written)
    assert repo.path.exists()
