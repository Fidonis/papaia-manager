"""The mirror of the ingester's job schema: what is valid, what is written, what is refused."""
from __future__ import annotations

import copy
from typing import Any

import pytest

from app.core.ingest import jobspec
from app.core.ingest.jobspec import Issue, RuleContext

CTX = RuleContext(
    connections=frozenset({"default", "research"}),
    secrets=frozenset({"QI_SECRET_DAV", "QI_SECRET_S3_KEY", "QI_SECRET_S3_SECRET"}),
)


def _local(**overrides: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "id": "handbook",
        "source": {"type": "local", "label": "handbook", "path": "/data/local/handbook"},
        "target": {"collection": "kb", "connection": "default"},
        "mode": "upsert",
        "embedding": {"model": "nomic-embed-text"},
    }
    job.update(overrides)
    return job


def _fields(issues: list[Issue]) -> dict[str, str]:
    return {issue.field: issue.message for issue in issues}


# ---------------------------------------------------------------------------
# The mirror matches what the editor renders
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("source_type", jobspec.SOURCE_TYPES)
def test_every_field_of_a_source_has_an_input_and_the_other_way_round(source_type: str) -> None:
    form = jobspec.form_keys(source_type)
    model = jobspec.model_keys(source_type) - {"rclone_flags"}

    assert form == model, (
        f"{source_type}: only in the form {form - model}, only in the schema {model - form}"
    )


def test_the_nine_source_types_are_all_offered() -> None:
    assert set(jobspec.SOURCE_FORMS) == set(jobspec.SOURCE_TYPES)
    assert len(jobspec.SOURCE_TYPES) == 9
    assert [form["type"] for form in jobspec.source_forms()] == list(jobspec.SOURCE_TYPES)


def test_the_password_key_is_pass_as_in_the_file() -> None:
    assert "pass" in jobspec.model_keys("webdav")
    assert "pass" in jobspec.secret_keys_of("sftp")
    assert jobspec.secret_keys_of("local") == frozenset()


# ---------------------------------------------------------------------------
# Validation, with the field in the ingester's dotted form
# ---------------------------------------------------------------------------


def test_a_minimal_local_job_is_valid() -> None:
    spec, issues = jobspec.parse_job(_local())

    assert issues == []
    assert spec is not None and spec.id == "handbook"


@pytest.mark.parametrize(
    ("source", "valid"),
    [
        ({"type": "s3", "label": "x", "bucket": "b"}, True),
        ({"type": "s3", "label": "x"}, False),
        (
            {
                "type": "webdav",
                "label": "x",
                "url": "https://d.test",
                "pass": "${env:QI_SECRET_DAV}",
            },
            True,
        ),
        ({"type": "sftp", "label": "x", "host": "h", "port": 70000}, False),
        ({"type": "smb", "label": "x", "host": "h", "share": "s"}, True),
        ({"type": "ftp", "label": "x", "host": "h", "tls": True}, True),
        ({"type": "gdrive", "label": "x"}, True),
        ({"type": "azureblob", "label": "x", "account": "a", "container": "c"}, True),
        ({"type": "http", "label": "x", "url": "https://files.test/"}, True),
        ({"type": "carrier-pigeon", "label": "x"}, False),
    ],
)
def test_each_source_type_is_validated(source: dict[str, Any], valid: bool) -> None:
    spec, issues = jobspec.parse_job(_local(source=source))

    assert (spec is not None) is valid
    assert bool(issues) is not valid


def test_problems_name_the_field_without_the_source_type_in_it() -> None:
    _, issues = jobspec.parse_job(_local(source={"type": "s3", "label": "x"}))

    assert _fields(issues) == {"source.bucket": "this is required"}
    assert issues[0].job_id == "handbook"


def test_a_key_the_ingester_does_not_know_is_named() -> None:
    _, issues = jobspec.parse_job(_local(colour="blue"))

    assert _fields(issues) == {"colour": "is not a setting the ingester knows"}


def test_a_credential_typed_into_the_file_is_refused_with_a_way_out() -> None:
    source = {"type": "webdav", "label": "x", "url": "https://d.test", "pass": "hunter2"}

    _, issues = jobspec.parse_job(_local(source=source))

    assert list(_fields(issues)) == ["source.pass"]
    assert "stored credential" in issues[0].message


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"id": "Not A Slug"}, "id"),
        ({"mode": "sync"}, "mode"),
        ({"target": {"collection": "-bad", "connection": "default"}}, "target.collection"),
        ({"target": {"collection": "kb", "connection": "Default"}}, "target.connection"),
        (
            {"target": {"collection": "kb", "connection": "default", "extra_payload": {"text": 1}}},
            "target.extra_payload",
        ),
        ({"chunking": {"words": 100, "overlap": 100}}, "chunking"),
        ({"schedule": {"cron": "0 3 * * *", "every": "1h"}}, "schedule"),
        ({"schedule": {"every": "soon"}}, "schedule"),
        ({"filters": {"max_file_bytes": 0}}, "filters.max_file_bytes"),
        ({"safety": {"max_delete_ratio": 2}}, "safety.max_delete_ratio"),
        ({"source_template": "{label}"}, "source_template"),
    ],
)
def test_the_rules_of_the_schema_are_applied(override: dict[str, Any], field: str) -> None:
    _, issues = jobspec.parse_job(_local(**override))

    assert field in {issue.field for issue in issues}, issues


def test_the_catalog_defaults_are_part_of_the_job() -> None:
    job = _local()
    del job["embedding"]

    spec, issues = jobspec.parse_job(job, {"embedding": {"model": "from-defaults"}})

    assert issues == [] and spec is not None
    assert spec.embedding.model == "from-defaults"


def test_the_ingesters_merge_replaces_lists_and_merges_mappings() -> None:
    merged = jobspec.effective(
        {"filters": {"include": ["*.md"]}, "schedule": {"every": "1h"}},
        {"filters": {"include": ["*.pdf"], "exclude": ["tmp/**"]}, "unknown": {"x": 1}},
    )

    assert merged["filters"] == {"include": ["*.md"], "exclude": ["tmp/**"]}
    assert "unknown" not in merged


# ---------------------------------------------------------------------------
# What is written back
# ---------------------------------------------------------------------------


def test_a_job_that_keeps_every_default_is_written_short() -> None:
    form_state = {
        "id": "handbook",
        "enabled": True,
        "description": "  ",
        "source": {"type": "local", "label": "handbook", "path": "/data/local/handbook"},
        "target": {
            "collection": "kb",
            "connection": "default",
            "acl_tags": [],
            "extra_payload": {},
        },
        "mode": "upsert",
        "full_scope": "job",
        "append_probe": "auto",
        "mcp_allow_full": False,
        "expand_embedded": False,
        "source_template": "{scheme}://{label}/{rel_path}",
        "filters": {"include": [], "exclude": [], "max_file_bytes": None},
        "schedule": {"jitter_seconds": 30, "run_on_startup": "if_missed", "timezone": None},
        "chunking": {"strategy": "auto", "words": 400, "overlap": 50},
        "embedding": {"model": "nomic-embed-text", "batch_size": None},
        "safety": {"max_delete_ratio": 0.25, "empty_source_guard": True},
    }

    assert jobspec.minimise(form_state) == _local()


def test_a_value_equal_to_the_catalog_default_is_left_out_but_an_override_is_kept() -> None:
    defaults = {
        "chunking": {"words": 512, "overlap": 64},
        "schedule": {"timezone": "Europe/Berlin"},
    }
    state = _local(
        chunking={"strategy": "auto", "words": 512, "overlap": 50},
        schedule={"timezone": "Europe/Berlin"},
    )

    out = jobspec.minimise(state, defaults)

    # words equals the catalog default and goes; overlap equals only the schema default but
    # differs from the catalog's 64, so it is an override and stays.
    assert out["chunking"] == {"overlap": 50}
    assert "schedule" not in out


def test_the_embedding_model_is_written_even_when_it_is_the_catalog_default() -> None:
    out = jobspec.minimise(_local(), {"embedding": {"model": "nomic-embed-text"}})

    assert out["embedding"] == {"model": "nomic-embed-text"}


def test_source_defaults_and_empty_values_are_dropped_and_required_ones_kept() -> None:
    source = {
        "type": "sftp",
        "label": "legal",
        "host": "sftp.example.com",
        "port": 22,
        "user": "",
        "path": "/",
        "pass": "QI_SECRET_DAV",
        "key_file": "",
    }

    out = jobspec.minimise(_local(source=source))["source"]

    assert out == {
        "type": "sftp",
        "label": "legal",
        "host": "sftp.example.com",
        "pass": "${env:QI_SECRET_DAV}",
    }


def test_a_reference_stays_a_reference_and_a_literal_is_left_for_the_schema_to_refuse() -> None:
    kept = jobspec.minimise(
        _local(source={"type": "webdav", "label": "x", "url": "https://d.test",
                       "pass": "${env:QI_SECRET_DAV}"})
    )["source"]
    typed = jobspec.minimise(
        _local(source={"type": "webdav", "label": "x", "url": "https://d.test", "pass": "hunter2"})
    )["source"]

    assert kept["pass"] == "${env:QI_SECRET_DAV}"
    assert typed["pass"] == "hunter2"
    _, issues = jobspec.parse_job({**_local(), "source": typed})
    assert [issue.field for issue in issues] == ["source.pass"]


def test_a_disabled_job_and_a_description_are_kept() -> None:
    out = jobspec.minimise(_local(enabled=False, description=" Nightly "))

    assert out["enabled"] is False
    assert out["description"] == "Nightly"
    assert list(out)[:3] == ["id", "enabled", "description"]


def test_minimising_a_valid_job_twice_changes_nothing() -> None:
    worked_example = {
        "id": "acme-reports",
        "description": "Quarterly reports",
        "source": {
            "type": "s3",
            "label": "acme-reports",
            "bucket": "acme-corp-reports",
            "prefix": "published/",
            "region": "eu-central-1",
            "access_key_id": "${env:QI_SECRET_S3_KEY}",
            "secret_access_key": "${env:QI_SECRET_S3_SECRET}",
            "rclone_flags": ["--s3-no-check-bucket"],
        },
        "filters": {"include": ["**/*.pdf"], "exclude": ["**/drafts/**"]},
        "target": {
            "collection": "corporate-knowledge",
            "connection": "default",
            "acl_tags": ["dept:finance"],
            "extra_payload": {"origin": "s3"},
        },
        "mode": "upsert",
        "schedule": {"cron": "0 2 * * *"},
        "chunking": {"words": 512, "overlap": 64},
        "embedding": {"model": "nomic-embed-text"},
    }
    spec, issues = jobspec.parse_job(worked_example)
    assert issues == [] and spec is not None

    once = jobspec.minimise(copy.deepcopy(worked_example))

    assert once == worked_example
    assert jobspec.minimise(copy.deepcopy(once)) == once


# ---------------------------------------------------------------------------
# The rules of the loader that need a context
# ---------------------------------------------------------------------------


def _doc(*jobs: dict[str, Any], defaults: dict[str, Any] | None = None) -> dict[str, Any]:
    return {"version": 1, "defaults": defaults or {}, "jobs": list(jobs)}


def _check(*jobs: dict[str, Any], **kw: Any) -> list[Issue]:
    ctx = kw.pop("ctx", CTX)
    return jobspec.check_catalog(_doc(*jobs, **kw), ctx)


def test_a_clean_catalog_has_no_issues() -> None:
    assert _check(_local()) == []


def test_each_per_job_rule_of_the_loader_is_applied() -> None:
    bad = _local(
        id="bad",
        source={"type": "local", "label": "x", "path": "/etc"},
        target={"collection": "kb2", "connection": "nowhere"},
        schedule={"cron": "not a cron", "timezone": "Nowhere/Land"},
    )
    del bad["embedding"]

    by_field = _fields(_check(bad))

    assert "source.path" in by_field and "/data/local" in by_field["source.path"]
    assert "target.connection" in by_field and "nowhere" in by_field["target.connection"]
    assert "schedule.cron" in by_field
    assert "schedule.timezone" in by_field
    assert "embedding.model" in by_field


def test_a_disabled_job_needs_no_model_but_is_still_checked() -> None:
    quiet = _local(
        id="quiet", enabled=False, source={"type": "local", "label": "q", "path": "/etc"}
    )
    del quiet["embedding"]

    by_field = _fields(_check(quiet))

    assert "embedding.model" not in by_field
    assert "source.path" in by_field


def test_a_missing_credential_is_reported_and_an_unknown_set_is_not_checked() -> None:
    webdav = _local(
        source={"type": "webdav", "label": "x", "url": "https://d.test",
                "pass": "${env:QI_SECRET_NOPE}"}
    )

    assert "source.pass" in _fields(_check(webdav))
    assert _check(webdav, ctx=RuleContext(connections=CTX.connections, secrets=None)) == []


def test_an_unknown_set_of_connections_is_not_checked() -> None:
    job = _local(target={"collection": "kb", "connection": "anything"})

    assert _check(job, ctx=RuleContext(connections=None, secrets=CTX.secrets)) == []


def test_the_cross_job_rules_of_the_loader() -> None:
    first = _local()
    twin = _local(id="twin")  # same collection and label
    other_model = _local(
        id="other-model",
        source={"type": "local", "label": "m", "path": "/data/local/m"},
        embedding={"model": "bge-m3"},
    )
    other_db = _local(
        id="other-db",
        source={"type": "local", "label": "d", "path": "/data/local/d"},
        target={"collection": "kb", "connection": "research"},
    )
    system = _local(
        id="system",
        source={"type": "local", "label": "s", "path": "/data/local/s"},
        target={"collection": "_rbac_acl", "connection": "default"},
    )

    issues = _check(first, twin, other_model, other_db, system, first)
    by_job = {(issue.job_id, issue.field) for issue in issues}

    assert ("twin", "source.label") in by_job
    assert ("other-model", "embedding.model") in by_job
    assert ("other-db", "target.connection") in by_job
    assert ("system", "target.collection") in by_job
    assert ("handbook", "id") in by_job  # listed twice


def test_a_disabled_job_does_not_collide_with_anything() -> None:
    twin = _local(id="twin", enabled=False)

    assert _check(_local(), twin) == []


def test_issues_of_one_job_can_be_picked_out() -> None:
    issues = jobspec.check_one(_doc(_local(), _local(id="twin")), "twin", CTX)

    assert [issue.field for issue in issues] == ["source.label"]
    assert jobspec.check_one(_doc(_local()), "handbook", CTX) == []


def test_a_catalog_that_is_not_one_is_reported_as_the_ingester_would() -> None:
    assert _fields(jobspec.check_catalog({"version": 2}, CTX)) == {
        "version": "unsupported catalog version 2; expected 1"
    }
    assert "defaults" in _fields(
        jobspec.check_catalog({"version": 1, "defaults": {"surprise": {}}}, CTX)
    )
    assert "jobs" in _fields(jobspec.check_catalog({"version": 1, "jobs": "nope"}, CTX))
    assert "jobs[0]" in _fields(jobspec.check_catalog({"version": 1, "jobs": ["x"]}, CTX))


@pytest.mark.parametrize(
    ("value", "problem"),
    [
        ("handbook", None),
        ("Handbook", "lowercase"),
        ("", "lowercase"),
        ("new", "reserved"),
        ("mgr-kb-1234-u", "Embedding page"),
    ],
)
def test_the_id_of_a_new_job(value: str, problem: str | None) -> None:
    result = jobspec.id_problem(value)

    if problem is None:
        assert result is None
    else:
        assert result is not None and problem in result
