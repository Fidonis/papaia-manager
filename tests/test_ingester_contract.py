"""The manager against the ingester's real code: what it writes loads, what it refuses it refuses.

The manager mirrors the ingester's job schema and writes its catalog files, so a drift between
the two is a job that validates here and is dropped there. These tests load the ingester's own
modules (`catalog.loader`, `catalog.schema`, `catalog.secret_store`) and check the two against
each other.

They need the ingester's source tree and are skipped without it. Point `QDRANT_INGEST_SRC` at
the `src` directory of a checkout of `Fidonis/qdrant-ingest` that has the secret store, for
example `C:\\Projects\\fidonis\\qdrant-ingest\\src`. Repeat them when either side's schema, its
defaults, its cipher or its loader rules change.
"""

from __future__ import annotations

import copy
import os
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

_SRC = os.environ.get("QDRANT_INGEST_SRC", "")

pytestmark = pytest.mark.skipif(
    not _SRC or not (Path(_SRC) / "catalog" / "loader.py").is_file(),
    reason="QDRANT_INGEST_SRC does not point at the ingester's src directory",
)

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-contract-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-contract-workspace-")

for _key, _value in {
    "OIDC_ISSUER_KC_AUTH": "https://kc.test/auth",
    "OIDC_ISSUER_KC_TOKEN": "https://kc.test/token",
    "OIDC_ISSUER_KC_CERTS": "https://kc.test/certs",
    "MANAGER_ADMIN_ROLE": "admin",
    "MANAGER_USER_ROLE": "user",
    "MANAGER_HOST": "http://localhost:8120",
    "MANAGER_OIDC_CLIENT_SECRET": "client-secret",
    "MANAGER_SESSION_SECRET": "test-session-secret-value",
    "PAPAIA_CONFIG_DIR": _CONFIG_DIR,
    "PAPAIA_WORKSPACE_DIR": _WORKSPACE_DIR,
}.items():
    os.environ.setdefault(_key, _value)

from app.core.ingest import job_forms, jobspec, schedules  # noqa: E402
from app.core.ingest import secrets as manager_secrets  # noqa: E402
from app.core.vectordb import catalog_io  # noqa: E402

KEY = "contract-test-connections-secret"
ENV = {
    "QI_SECRET_DAV": "davpass",
    "QI_SECRET_S3_KEY": "AKIA",
    "QI_SECRET_S3_SECRET": "s3secret",
    "QI_SECRET_SFTP_KEY": "-----BEGIN-----\nx\n-----END-----",
    "QI_SECRET_GD": "{}",
    "QI_SECRET_AZ": "k",
}


@pytest.fixture(scope="module")
def ingester() -> Iterator[Any]:
    """The ingester's modules, imported from its source tree."""
    sys.path.insert(0, _SRC)
    try:
        import catalog.loader as loader
        import catalog.schema as schema
        from config import Settings

        yield type("Ingester", (), {"loader": loader, "schema": schema, "Settings": Settings})
    finally:
        sys.path.remove(_SRC)
        for name in [m for m in sys.modules if m.split(".")[0] in _INGESTER_PACKAGES]:
            del sys.modules[name]


_INGESTER_PACKAGES = {
    "catalog",
    "config",
    "connections",
    "engine",
    "scheduler",
    "state",
    "store",
    "sources",
    "extract",
    "chunk",
    "embed",
    "api",
    "ui",
    "mcp_app",
}


def _secret_store_api() -> Any:
    """The ingester's secret store, imported without its Qdrant client.

    `connections/__init__` pulls in the registry and with it `qdrant_client`, which the
    manager does not have. The store only needs `connections.crypto`, so `connections` is
    stood in for by an empty package that points at the same directory.
    """
    import types

    sys.path.insert(0, _SRC)
    try:
        if "connections" not in sys.modules:
            package = types.ModuleType("connections")
            package.__path__ = [str(Path(_SRC) / "connections")]
            sys.modules["connections"] = package
        import catalog.secret_store as store

        return store
    finally:
        sys.path.remove(_SRC)


def _load(
    ingester: Any,
    tmp: Path,
    *jobs: dict[str, Any],
    defaults: dict[str, Any] | None = None,
    connections: set[str] | None = None,
    environ: dict[str, str] | None = None,
) -> Any:
    document: dict[str, Any] = {"version": 1}
    if defaults:
        document["defaults"] = defaults
    document["jobs"] = list(jobs)
    path = tmp / "jobs.yaml"
    path.write_bytes(catalog_io.dump_document(document).encode("utf-8"))
    return ingester.loader.load_catalog(
        path,
        ingester.Settings(local_dir="/data/local"),
        ENV if environ is None else environ,
        known_connections={"default", "research"} if connections is None else connections,
    )


def _job(**overrides: Any) -> dict[str, Any]:
    job: dict[str, Any] = {
        "id": "handbook",
        "source": {"type": "local", "label": "handbook", "path": "/data/local/handbook"},
        "target": {"collection": "kb", "connection": "default"},
        "mode": "upsert",
        "embedding": {"model": "nomic-embed-text"},
    }
    job.update(overrides)
    return job


# ---------------------------------------------------------------------------
# The mirror has the shape of the original
# ---------------------------------------------------------------------------


def test_the_job_has_the_same_fields_and_the_same_defaults(ingester: Any) -> None:
    real = ingester.schema.JobConfig
    ours = jobspec.JobSpec

    assert set(real.model_fields) == set(ours.model_fields)
    for name, info in real.model_fields.items():
        mine = ours.model_fields[name]
        if name in ("source", "filters", "target", "schedule", "chunking", "embedding", "safety"):
            continue
        assert mine.get_default(call_default_factory=True) == info.get_default(
            call_default_factory=True
        ), name


@pytest.mark.parametrize("section", jobspec.DEFAULT_SECTIONS)
def test_each_section_has_the_same_fields_and_defaults(ingester: Any, section: str) -> None:
    real = {
        "embedding": ingester.schema.EmbeddingConfig,
        "chunking": ingester.schema.ChunkingConfig,
        "filters": ingester.schema.FiltersConfig,
        "schedule": ingester.schema.ScheduleConfig,
        "safety": ingester.schema.SafetyConfig,
    }[section]()

    assert jobspec.section_defaults(section) == real.model_dump()


def test_the_target_has_the_same_fields(ingester: Any) -> None:
    assert set(ingester.schema.TargetConfig.model_fields) == set(jobspec.TargetSpec.model_fields)


def test_the_nine_source_types_have_the_same_fields_and_defaults(ingester: Any) -> None:
    real = {
        "local": ingester.schema.LocalSource,
        "s3": ingester.schema.S3Source,
        "webdav": ingester.schema.WebdavSource,
        "sftp": ingester.schema.SftpSource,
        "smb": ingester.schema.SmbSource,
        "ftp": ingester.schema.FtpSource,
        "gdrive": ingester.schema.GdriveSource,
        "azureblob": ingester.schema.AzureBlobSource,
        "http": ingester.schema.HttpSource,
    }

    assert set(real) == set(jobspec.SOURCE_TYPES)
    for source_type, model in real.items():
        theirs = {(i.alias or n): i for n, i in model.model_fields.items() if n != "type"}
        assert set(theirs) == jobspec.model_keys(source_type) | {"label"} - set(), source_type
        for key, info in theirs.items():
            if key in ("label",) or info.is_required():
                continue
            mine = jobspec._source_defaults(source_type)
            assert mine[key] == info.get_default(call_default_factory=True), (source_type, key)
        assert set(model.secret_fields) == {
            "password" if k == "pass" else k for k in jobspec.secret_keys_of(source_type)
        }, source_type


def test_the_reserved_payload_keys_and_the_patterns_are_the_same(ingester: Any) -> None:
    assert jobspec.RESERVED_PAYLOAD_KEYS == ingester.schema.RESERVED_PAYLOAD_KEYS
    assert jobspec.SLUG_PATTERN == ingester.schema.SLUG_PATTERN
    assert jobspec.COLLECTION_PATTERN == ingester.schema.COLLECTION_PATTERN


# ---------------------------------------------------------------------------
# What the manager writes, the ingester loads
# ---------------------------------------------------------------------------

_STATES: list[dict[str, Any]] = [
    # (the editor's state of each kind of job)
    {
        "id": "local-job",
        "source": {"type": "local", "label": "l", "path": "/data/local/a"},
        "target": {"collection": "c1", "connection": "default", "acl_tags": "dept:a\ndept:b"},
        "mode": "upsert",
        "schedule": {"mode": "daily", "time": "03:30"},
        "embedding": {"model": "m"},
        "filters": {"include": "**/*.md\n**/*.pdf", "exclude": "**/tmp/**"},
    },
    {
        "id": "s3-job",
        "source": {
            "type": "s3",
            "label": "s",
            "bucket": "b",
            "prefix": "p/",
            "region": "eu-central-1",
            "access_key_id": "QI_SECRET_S3_KEY",
            "secret_access_key": "QI_SECRET_S3_SECRET",
            "rclone_flags": "--s3-no-check-bucket",
        },
        "target": {"collection": "c2", "connection": "default"},
        "mode": "append",
        "schedule": {"mode": "interval", "every_n": 6, "every_unit": "h"},
        "embedding": {"model": "m"},
    },
    {
        "id": "dav-job",
        "source": {
            "type": "webdav",
            "label": "d",
            "url": "https://d.test/dav",
            "vendor": "nextcloud",
            "user": "me",
            "pass": "QI_SECRET_DAV",
        },
        "target": {"collection": "c3", "connection": "research"},
        "mode": "full",
        "full_scope": "job",
        "schedule": {
            "mode": "weekly",
            "time": "22:00",
            "weekdays": ["mon", "fri"],
            "timezone": "Europe/Berlin",
            "run_on_startup": "never",
            "jitter_seconds": 5,
        },
        "embedding": {"model": "m", "batch_size": 8},
    },
    {
        "id": "sftp-job",
        "source": {
            "type": "sftp",
            "label": "f",
            "host": "h",
            "port": 2222,
            "user": "u",
            "pass": "QI_SECRET_DAV",
            "key_file": "QI_SECRET_SFTP_KEY",
            "path": "/x",
        },
        "target": {"collection": "c4", "connection": "default"},
        "mode": "upsert",
        "schedule": {"mode": "monthly", "time": "01:00", "day_of_month": 15},
        "embedding": {"model": "m"},
        "chunking": {"strategy": "markdown", "words": 300, "overlap": 30},
        "safety": {"max_delete_ratio": 0.5, "empty_source_guard": False},
    },
    {
        "id": "smb-job",
        "source": {"type": "smb", "label": "m", "host": "h", "share": "s", "pass": "QI_SECRET_DAV"},
        "target": {"collection": "c5", "connection": "default"},
        "mode": "upsert",
        "schedule": {"mode": "cron", "cron": "0 4 * * 1-3"},
        "embedding": {"model": "m"},
    },
    {
        "id": "ftp-job",
        "source": {"type": "ftp", "label": "t", "host": "h", "tls": True},
        "target": {"collection": "c6", "connection": "default"},
        "mode": "upsert",
        "schedule": {"mode": "hourly", "minute": 15},
        "embedding": {"model": "m"},
        "target_extra": 1,
    },
    {
        "id": "gd-job",
        "source": {
            "type": "gdrive",
            "label": "g",
            "service_account_json": "QI_SECRET_GD",
            "root_folder_id": "abc",
        },
        "target": {
            "collection": "c7",
            "connection": "default",
            "extra_payload": '{"origin": "drive"}',
        },
        "mode": "append",
        "schedule": {"mode": "manual"},
        "embedding": {"model": "m"},
        "mcp_allow_full": True,
        "expand_embedded": True,
    },
    {
        "id": "az-job",
        "source": {
            "type": "azureblob",
            "label": "z",
            "account": "a",
            "container": "c",
            "key": "QI_SECRET_AZ",
        },
        "target": {"collection": "c8", "connection": "default"},
        "mode": "upsert",
        "schedule": {"mode": "manual"},
        "embedding": {"model": "m"},
    },
    {
        "id": "http-job",
        "source": {"type": "http", "label": "h", "url": "https://files.test/"},
        "target": {"collection": "c9", "connection": "default"},
        "mode": "upsert",
        "schedule": {"mode": "manual"},
        "embedding": {"model": "m"},
        "enabled": False,
    },
]


@pytest.mark.parametrize("state", _STATES, ids=lambda s: s["id"])
def test_a_job_the_editor_produces_loads_in_the_ingester_as_intended(
    ingester: Any, tmp_path: Path, state: dict[str, Any]
) -> None:
    state = copy.deepcopy(state)
    state.pop("target_extra", None)
    entry, issues = job_forms.authored(state, None)
    assert issues == []
    spec, schema_issues = jobspec.parse_job(entry)
    assert schema_issues == [], schema_issues

    result = _load(ingester, tmp_path, entry)

    assert result.errors == [], [str(e) for e in result.errors]
    [job] = result.jobs
    assert job.id == state["id"] and job.enabled is (state.get("enabled", True))
    assert job.target.collection == state["target"]["collection"]
    assert job.mode == state["mode"]
    # What the editor compiled is what the ingester schedules.
    plan = schedules.read_plan(entry.get("schedule"))
    assert (job.schedule.cron, job.schedule.every) == (
        schedules.compile_plan(plan).cron,
        schedules.compile_plan(plan).every,
    )
    # The mirror's view of the job and the ingester's agree on the fields that carry meaning.
    assert job.model_dump(by_alias=True)["source"]["type"] == state["source"]["type"]
    assert job.embedding.model == "m"


def test_the_catalog_defaults_are_applied_the_same_way(ingester: Any, tmp_path: Path) -> None:
    defaults = {
        "embedding": {"model": "from-defaults", "batch_size": 4},
        "chunking": {"words": 512, "overlap": 64},
        "filters": {"exclude": ["**/.git/**"]},
        "schedule": {"timezone": "Europe/Berlin", "jitter_seconds": 7},
    }
    raw = _job()
    del raw["embedding"]
    raw["chunking"] = {"overlap": 50}

    result = _load(ingester, tmp_path, raw, defaults=defaults)

    assert result.errors == []
    [job] = result.jobs
    assert job.embedding.model == "from-defaults" and job.embedding.batch_size == 4
    assert (job.chunking.words, job.chunking.overlap) == (512, 50)
    assert job.filters.exclude == ["**/.git/**"] and job.schedule.jitter_seconds == 7
    merged = jobspec.effective(raw, defaults)
    assert merged["chunking"] == {"words": 512, "overlap": 50}
    spec, issues = jobspec.parse_job(raw, defaults)
    assert issues == [] and spec is not None
    assert (spec.chunking.words, spec.chunking.overlap) == (512, 50)


# ---------------------------------------------------------------------------
# What the ingester refuses, the manager refuses
# ---------------------------------------------------------------------------

_BAD: list[tuple[str, dict[str, Any]]] = [
    ("unknown key", _job(colour="blue")),
    ("bad id", _job(id="Not A Slug")),
    ("bad mode", _job(mode="sync")),
    ("bad collection", _job(target={"collection": "-x", "connection": "default"})),
    (
        "reserved payload",
        _job(target={"collection": "c", "connection": "default", "extra_payload": {"text": 1}}),
    ),
    ("overlap", _job(chunking={"words": 10, "overlap": 10})),
    ("cron and every", _job(schedule={"cron": "0 3 * * *", "every": "1h"})),
    ("bad every", _job(schedule={"every": "soon"})),
    ("bad cron", _job(schedule={"cron": "not a cron"})),
    (
        "literal secret",
        _job(source={"type": "webdav", "label": "w", "url": "https://x", "pass": "hunter2"}),
    ),
    ("missing bucket", _job(source={"type": "s3", "label": "s"})),
    ("bad port", _job(source={"type": "sftp", "label": "s", "host": "h", "port": 70000})),
    ("source template", _job(source_template="{label}")),
    ("ratio", _job(safety={"max_delete_ratio": 2})),
    ("path outside", _job(source={"type": "local", "label": "l", "path": "/etc"})),
    ("unknown connection", _job(target={"collection": "c", "connection": "nowhere"})),
    ("no model", {k: v for k, v in _job().items() if k != "embedding"}),
    ("system collection", _job(target={"collection": "_rbac_acl", "connection": "default"})),
]


@pytest.mark.parametrize(("name", "raw"), _BAD, ids=[n for n, _ in _BAD])
def test_a_job_the_ingester_refuses_is_refused_here_too(
    ingester: Any, tmp_path: Path, name: str, raw: dict[str, Any]
) -> None:
    theirs = _load(ingester, tmp_path, raw)
    spec, schema_issues = jobspec.parse_job(raw)
    document = {"version": 1, "jobs": [raw]}
    ours = schema_issues + jobspec.check_catalog(
        document,
        jobspec.RuleContext(
            connections=frozenset({"default", "research"}),
            secrets=frozenset(ENV),
            system_collections=frozenset({"_collection_meta", "_rbac_acl"}),
        ),
    )

    assert theirs.errors, f"{name}: the ingester accepted it"
    assert ours, f"{name}: the manager accepted what the ingester refuses"


def test_the_cross_job_rules_agree(ingester: Any, tmp_path: Path) -> None:
    first = _job()
    twin = _job(id="twin")  # same collection and label
    other_model = _job(
        id="m",
        source={"type": "local", "label": "m", "path": "/data/local/m"},
        embedding={"model": "other"},
    )
    other_db = _job(
        id="d",
        source={"type": "local", "label": "d", "path": "/data/local/d"},
        target={"collection": "kb", "connection": "research"},
    )
    document: dict[str, Any] = {
        "version": 1,
        "jobs": [first, twin, other_model, other_db, first],
    }
    theirs = _load(ingester, tmp_path, *document["jobs"])
    ours = jobspec.check_catalog(
        document, jobspec.RuleContext(connections=frozenset({"default", "research"}))
    )

    their_pairs = {(e.job_id, e.field) for e in theirs.errors}
    our_pairs = {(i.job_id, i.field) for i in ours}
    assert their_pairs == our_pairs, their_pairs ^ our_pairs


# ---------------------------------------------------------------------------
# The secret store, written here and read there
# ---------------------------------------------------------------------------


def test_a_credential_stored_by_the_manager_is_read_by_the_ingester(
    ingester: Any, tmp_path: Path
) -> None:
    secret_store = _secret_store_api()
    catalog_dir = tmp_path / "ai" / "rag" / "catalog"
    catalog_dir.mkdir(parents=True)
    repo = manager_secrets.SecretsRepository(tmp_path, KEY)
    repo.set("QI_SECRET_STORED", "from-the-manager")
    repo.set("QI_SECRET_PEM", "-----BEGIN-----\nabc\n-----END-----\n")

    store = secret_store.SecretStore(catalog_dir / "secrets.yaml", KEY)
    environ = secret_store.LayeredEnviron({"QI_SECRET_ENV": "e"}, store)

    assert store.problem() is None
    assert environ["QI_SECRET_STORED"] == "from-the-manager"
    assert environ["QI_SECRET_PEM"].startswith("-----BEGIN-----\nabc")
    # A job that refers to it is valid there, with nothing in the process environment.
    raw = _job(
        source={
            "type": "webdav",
            "label": "w",
            "url": "https://x",
            "pass": "${env:QI_SECRET_STORED}",
        }
    )
    result = _load(ingester, tmp_path, raw, environ=environ)
    assert result.errors == [], [str(e) for e in result.errors]
    # And the other way round: a name that is not stored is reported.
    missing = _job(
        source={"type": "webdav", "label": "w", "url": "https://x", "pass": "${env:QI_SECRET_GONE}"}
    )
    assert _load(ingester, tmp_path, missing, environ=environ).errors
    # The manager's own check of the file agrees with the ingester's reader.
    assert repo.readable() == {"QI_SECRET_STORED": True, "QI_SECRET_PEM": True}
    wrong = secret_store.SecretStore(catalog_dir / "secrets.yaml", "another-secret")
    assert wrong.get("QI_SECRET_STORED") is None and wrong.problem() is not None
    assert manager_secrets.SecretsRepository(tmp_path, "another-secret").readable() == {
        "QI_SECRET_STORED": False,
        "QI_SECRET_PEM": False,
    }


def test_the_file_the_manager_writes_is_valid_for_the_ingesters_schema(
    ingester: Any, tmp_path: Path
) -> None:
    secret_store = _secret_store_api()
    (tmp_path / "ai" / "rag" / "catalog").mkdir(parents=True)
    manager_secrets.SecretsRepository(tmp_path, KEY).set("QI_SECRET_ONE", "x")
    text = (tmp_path / manager_secrets.SECRETS_RELPATH).read_text(encoding="utf-8")

    document = secret_store.SecretsDocument.model_validate(yaml.safe_load(text))

    assert [s.name for s in document.secrets] == ["QI_SECRET_ONE"]
