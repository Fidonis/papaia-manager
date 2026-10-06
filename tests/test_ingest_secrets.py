"""The credential store: encrypted, write-only, never shadowing the environment.

The repository writes the format the ingester reads, so the checks are on the bytes: what is in
the file, what is not (a value, a backup copy), and what a reader with the right key gets back.
The service tests check the rules around it and, above all, that a value never leaves: not in an
answer, not in the audit log, not in an exception.
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-secrets-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-secrets-workspace-")

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

from app.config import get_settings  # noqa: E402
from app.core.audit import audit_path  # noqa: E402
from app.core.ingest import catalog, runs  # noqa: E402
from app.core.ingest import secrets as store  # noqa: E402
from app.core.ingest.client import IngestClient  # noqa: E402
from app.core.ingest.errors import (  # noqa: E402
    CatalogRejected,
    Conflict,
    IngestTooOld,
    InvalidRequest,
    NotFound,
)
from app.core.ingest.jobs_service import JobsService  # noqa: E402
from app.core.vectordb import catalog_io  # noqa: E402
from app.core.vectordb.crypto import decrypt, encrypt  # noqa: E402
from app.core.vectordb.ingest_file import CONNECTIONS_RELPATH  # noqa: E402
from tests.fake_ingest import TOKEN, FakeIngest  # noqa: E402
from tests.fake_qdrant import Fleet  # noqa: E402

KEY = "a-long-random-connections-secret"
VALUE = "s3cr3t-value-that-must-never-leak"


class Env:
    def __init__(self, config_dir: Path, service: JobsService, ingest: FakeIngest) -> None:
        self.config_dir = config_dir
        self.service = service
        self.ingest = ingest

    @property
    def path(self) -> Path:
        return self.config_dir / store.SECRETS_RELPATH

    def repo(self, key: str = KEY) -> store.SecretsRepository:
        return store.SecretsRepository(self.config_dir, key)

    def document(self) -> dict[str, Any]:
        return yaml.safe_load(self.path.read_text(encoding="utf-8"))

    def audit_text(self) -> str:
        path = audit_path(str(self.config_dir))
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def folder(self) -> list[str]:
        return sorted(p.name for p in self.path.parent.iterdir())


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    config_dir = tmp_path / "config"
    (config_dir / CONNECTIONS_RELPATH.parent).mkdir(parents=True)
    (config_dir / ".env").write_text("COMPOSE_PROFILES=keycloak,rag\n", encoding="utf-8")
    (config_dir / "ai" / "rag" / ".env").write_text(
        f"QI_CONNECTIONS_SECRET={KEY}\nQI_API_TOKEN={TOKEN}\nQI_SECRET_FROM_ENV=hunter2\n",
        encoding="utf-8",
    )
    get_settings.cache_clear()
    settings = get_settings().model_copy(
        update={
            "papaia_config_dir": str(config_dir),
            "qdrant_ingest_url": "http://qdrant-ingest:8300",
        }
    )

    async def idle(_kind: object) -> None:
        return None

    monkeypatch.setattr(runs.runner, "find_runner", idle)
    ingest = FakeIngest(config_dir)
    fleet = Fleet()
    fleet.add("qdrant-ingest", ingest)
    client = IngestClient("http://qdrant-ingest:8300", TOKEN, transport=fleet.transport())
    yield Env(config_dir, JobsService(settings, client=client), ingest)
    get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "stored"),
    [
        ("S3_KEY", "QI_SECRET_S3_KEY"),
        ("QI_SECRET_S3_KEY", "QI_SECRET_S3_KEY"),
        ("s3-key", "QI_SECRET_S3_KEY"),
        (" nextcloud password ", "QI_SECRET_NEXTCLOUD_PASSWORD"),
    ],
)
def test_a_name_is_normalised_to_the_stored_form(typed: str, stored: str) -> None:
    assert store.full_name(typed) == stored


@pytest.mark.parametrize("typed", ["", "  ", "bad/name", "ünïcode", "QI_SECRET_", "A" * 100])
def test_a_name_that_cannot_be_a_credential_is_refused(typed: str) -> None:
    with pytest.raises(store.SecretProblem):
        store.full_name(typed)


# ---------------------------------------------------------------------------
# The repository
# ---------------------------------------------------------------------------


def test_a_stored_value_is_a_token_the_right_key_reads_and_the_file_never_holds_it(
    env: Env,
) -> None:
    created = env.repo().set("QI_SECRET_DAV", VALUE)

    assert created is True
    document = env.document()
    assert document["version"] == 1
    [entry] = document["secrets"]
    assert set(entry) == {"name", "value"} and entry["name"] == "QI_SECRET_DAV"
    assert entry["value"].startswith("enc:1:")
    assert VALUE.encode() not in env.path.read_bytes()
    assert decrypt(entry["value"], KEY) == VALUE


def test_replacing_keeps_one_entry_and_in_its_place(env: Env) -> None:
    repo = env.repo()
    repo.set("QI_SECRET_A", "one")
    repo.set("QI_SECRET_B", "two")

    created = repo.set("QI_SECRET_A", "changed")

    assert created is False
    names = [e["name"] for e in env.document()["secrets"]]
    assert names == ["QI_SECRET_A", "QI_SECRET_B"]
    assert decrypt(env.document()["secrets"][0]["value"], KEY) == "changed"


def test_no_backup_copy_of_the_previous_content_is_left_behind(env: Env) -> None:
    repo = env.repo()
    repo.set("QI_SECRET_A", "first-value")
    repo.set("QI_SECRET_A", "second-value")
    repo.delete("QI_SECRET_A")

    assert env.folder() == ["secrets.yaml"], "no .bak and no staging file"
    assert b"first-value" not in env.path.read_bytes()


def test_deleting_removes_the_entry_and_reports_whether_there_was_one(env: Env) -> None:
    repo = env.repo()
    repo.set("QI_SECRET_A", "x")

    assert repo.delete("QI_SECRET_A") is True
    assert repo.delete("QI_SECRET_A") is False
    assert env.document()["secrets"] == []


@pytest.mark.parametrize("value", ["", "   ", "\n"])
def test_an_empty_value_is_refused(env: Env, value: str) -> None:
    with pytest.raises(store.SecretProblem, match="empty"):
        env.repo().set("QI_SECRET_A", value)

    assert not env.path.exists()


def test_a_value_over_the_limit_is_refused(env: Env) -> None:
    with pytest.raises(store.SecretProblem, match="64 KB"):
        env.repo().set("QI_SECRET_A", "x" * (store.MAX_VALUE_BYTES + 1))


def test_a_pem_key_with_newlines_survives_the_roundtrip(env: Env) -> None:
    pem = "-----BEGIN KEY-----\nabc\ndef\n-----END KEY-----\n"

    env.repo().set("QI_SECRET_SFTP_KEY", pem)

    assert decrypt(env.document()["secrets"][0]["value"], KEY) == pem


def test_without_the_key_nothing_can_be_stored(env: Env) -> None:
    with pytest.raises(CatalogRejected, match="QI_CONNECTIONS_SECRET"):
        env.repo(key="").set("QI_SECRET_A", "x")

    assert not env.path.exists()


def test_a_rotated_key_makes_the_values_unreadable_and_storing_again_still_works(env: Env) -> None:
    env.repo(key="the-old-key").set("QI_SECRET_OLD", "x")
    repo = env.repo()

    assert repo.readable() == {"QI_SECRET_OLD": False}

    repo.set("QI_SECRET_OLD", "again")  # the repair after a rotation must stay possible
    assert repo.readable() == {"QI_SECRET_OLD": True}


def test_the_names_are_listed_without_values(env: Env) -> None:
    env.repo().set("QI_SECRET_A", "x")
    env.repo().set("QI_SECRET_B", "y")

    snapshot = env.repo().snapshot()

    assert snapshot.names == ("QI_SECRET_A", "QI_SECRET_B")
    assert "x" not in repr(snapshot.entries[0]["name"])


@pytest.mark.parametrize(
    "text",
    [
        "jobs: [unterminated",
        "version: 2\nsecrets: []\n",
        "version: 1\nsecrets: nope\n",
        "- just\n- a list\n",
        "version: 1\nsecrets:\n  - name: QI_SECRET_X\n    value: plaintext\n",
        "version: 1\nsecrets:\n  - name: lower\n    value: enc:1:abc\n",
        "version: 1\nsecrets:\n  - {name: QI_SECRET_X, value: 'enc:1:a', extra: 1}\n",
    ],
)
def test_a_file_the_ingester_would_refuse_is_never_written_to(env: Env, text: str) -> None:
    env.path.write_text(text, encoding="utf-8")

    snapshot = env.repo().snapshot()
    assert snapshot.structural_error is not None
    with pytest.raises(CatalogRejected, match="cannot be changed"):
        env.repo().set("QI_SECRET_A", "x")

    assert env.path.read_text(encoding="utf-8") == text


def test_a_change_made_meanwhile_is_not_lost(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    repo = env.repo()
    repo.set("QI_SECRET_A", "a")
    real_swap = catalog_io.swap
    calls = {"count": 0}

    def swap_after_a_concurrent_write(*args: Any, **kwargs: Any) -> bool:
        calls["count"] += 1
        if calls["count"] == 1:
            document = env.document()
            document["secrets"].append(
                {"name": "QI_SECRET_FROM_ELSEWHERE", "value": encrypt("z", KEY)}
            )
            env.path.write_text(yaml.safe_dump(document), encoding="utf-8")
        return real_swap(*args, **kwargs)

    monkeypatch.setattr(catalog_io, "swap", swap_after_a_concurrent_write)

    repo.set("QI_SECRET_B", "b")

    assert calls["count"] == 2
    assert [e["name"] for e in env.document()["secrets"]] == [
        "QI_SECRET_A",
        "QI_SECRET_FROM_ELSEWHERE",
        "QI_SECRET_B",
    ]


def test_a_file_that_keeps_changing_is_given_up_on(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(catalog_io, "swap", lambda *a, **k: False)

    with pytest.raises(CatalogRejected, match="keeps changing"):
        env.repo().set("QI_SECRET_A", "x")


def test_the_catalog_folder_is_never_created(tmp_path: Path) -> None:
    repo = store.SecretsRepository(tmp_path / "no-such-config", KEY)

    with pytest.raises(CatalogRejected, match="does not exist"):
        repo.set("QI_SECRET_A", "x")
    assert not (tmp_path / "no-such-config").exists()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes")
def test_the_mode_of_an_existing_file_is_kept(env: Env) -> None:
    env.repo().set("QI_SECRET_A", "x")
    env.path.chmod(0o640)

    env.repo().set("QI_SECRET_B", "y")

    assert stat.S_IMODE(env.path.stat().st_mode) == 0o640


def test_which_jobs_use_which_credential(env: Env) -> None:
    document = {
        "jobs": [
            {"id": "one", "source": {"type": "webdav", "pass": "${env:QI_SECRET_DAV}"}},
            {
                "id": "two",
                "enabled": False,
                "source": {
                    "type": "s3",
                    "access_key_id": "${env:QI_SECRET_DAV}",
                    "secret_access_key": "${env:QI_SECRET_S3}",
                },
            },
            {"id": "three", "source": {"type": "local", "path": "/data/local/x"}},
            "not a job",
            {"source": {"pass": "${env:QI_SECRET_ORPHAN}"}},
        ]
    }

    assert store.references(document) == {
        "QI_SECRET_DAV": ["one", "two"],
        "QI_SECRET_S3": ["two"],
        "QI_SECRET_ORPHAN": ["jobs[4]"],
    }
    assert store.references({"jobs": "nope"}) == {}


# ---------------------------------------------------------------------------
# The service
# ---------------------------------------------------------------------------


async def test_a_credential_is_stored_audited_and_the_ingester_is_told_to_reload(env: Env) -> None:
    name = await env.service.set_credential("dav", VALUE, user="alice")

    assert name == "QI_SECRET_DAV"
    assert decrypt(env.document()["secrets"][0]["value"], KEY) == VALUE
    assert ("POST", "/v1/config/reload") in env.ingest.calls
    audit = [json.loads(line) for line in env.audit_text().splitlines()]
    assert [(e["user"], e["action"], e["target"]) for e in audit] == [
        ("alice", "rag.ingest.secret.set", "QI_SECRET_DAV")
    ]
    assert VALUE not in env.audit_text()


async def test_a_name_that_exists_in_the_environment_is_not_shadowed(env: Env) -> None:
    with pytest.raises(Conflict, match="ai/rag/.env"):
        await env.service.set_credential("from_env", VALUE, user="alice")

    assert not env.path.exists()


async def test_an_ingester_that_does_not_read_the_store_is_told_so_before_anything_is_written(
    env: Env,
) -> None:
    env.ingest.features = []

    with pytest.raises(IngestTooOld, match="secret store"):
        await env.service.set_credential("dav", VALUE, user="alice")

    assert not env.path.exists()


async def test_a_bad_name_is_a_refusal_not_a_crash(env: Env) -> None:
    with pytest.raises(InvalidRequest):
        await env.service.set_credential("bad/name", VALUE, user="alice")
    with pytest.raises(InvalidRequest, match="empty"):
        await env.service.set_credential("dav", "  ", user="alice")


async def test_the_view_lists_names_where_they_come_from_and_where_they_are_used(env: Env) -> None:
    await env.service.set_credential("dav", VALUE, user="alice")
    await env.service.set_credential("unused", "x", user="alice")
    jobs = {
        "version": 1,
        "jobs": [
            {
                "id": "cloud",
                "source": {
                    "type": "webdav",
                    "label": "c",
                    "url": "https://c.test",
                    "pass": "${env:QI_SECRET_DAV}",
                },
            }
        ],
    }
    (env.config_dir / catalog.JOBS_RELPATH).write_text(yaml.safe_dump(jobs), encoding="utf-8")

    view = await env.service.credentials()

    by_name = {item.name: item for item in view.items}
    assert list(by_name) == ["QI_SECRET_DAV", "QI_SECRET_FROM_ENV", "QI_SECRET_UNUSED"]
    assert by_name["QI_SECRET_DAV"].used_by == ("cloud",)
    assert not by_name["QI_SECRET_DAV"].deletable
    assert by_name["QI_SECRET_UNUSED"].deletable
    assert by_name["QI_SECRET_FROM_ENV"].origin == "environment"
    assert not by_name["QI_SECRET_FROM_ENV"].deletable
    assert view.supported is True and view.has_key
    assert VALUE not in repr(view)


async def test_a_stored_value_with_the_name_of_an_environment_variable_is_flagged_shadowed(
    env: Env,
) -> None:
    env.repo().set("QI_SECRET_FROM_ENV", "ignored")

    [item] = [i for i in (await env.service.credentials()).items if i.name == "QI_SECRET_FROM_ENV"]

    assert item.origin == "environment" and item.shadowed


async def test_the_view_says_when_the_ingester_cannot_be_asked(env: Env) -> None:
    env.ingest.down = True

    view = await env.service.credentials()

    assert view.supported is None and not view.ingester.reachable


async def test_a_credential_in_use_cannot_be_deleted_and_an_unused_one_can(env: Env) -> None:
    await env.service.set_credential("dav", VALUE, user="alice")
    jobs = {
        "version": 1,
        "jobs": [
            {"id": "cloud", "source": {"type": "webdav", "pass": "${env:QI_SECRET_DAV}"}}
        ],
    }
    (env.config_dir / catalog.JOBS_RELPATH).write_text(yaml.safe_dump(jobs), encoding="utf-8")

    with pytest.raises(Conflict, match="cloud"):
        await env.service.delete_credential("dav", user="alice")
    assert len(env.document()["secrets"]) == 1

    (env.config_dir / catalog.JOBS_RELPATH).write_text("version: 1\njobs: []\n", encoding="utf-8")
    await env.service.delete_credential("QI_SECRET_DAV", user="alice")

    assert env.document()["secrets"] == []
    assert env.audit_text().count("rag.ingest.secret.delete") == 1


async def test_what_cannot_be_deleted_or_is_not_there_is_said(env: Env) -> None:
    with pytest.raises(Conflict, match="ai/rag/.env"):
        await env.service.delete_credential("QI_SECRET_FROM_ENV", user="alice")
    with pytest.raises(NotFound):
        await env.service.delete_credential("QI_SECRET_NOPE", user="alice")
    with pytest.raises(InvalidRequest):
        await env.service.delete_credential("bad/name", user="alice")


async def test_a_value_is_in_no_answer_log_or_error(
    env: Env, caplog: pytest.LogCaptureFixture
) -> None:
    seen: list[str] = []
    try:
        await env.service.set_credential("dav", VALUE, user="alice")
        seen.append(repr(await env.service.credentials()))
        seen.append(json.dumps(env.service.credential_names()))
        env.path.write_text("version: 1\nsecrets: nope\n", encoding="utf-8")
        await env.service.set_credential("other", VALUE, user="alice")
    except CatalogRejected as exc:
        seen.append(str(exc))

    assert all(VALUE not in text for text in seen)
    assert VALUE not in caplog.text and VALUE not in env.audit_text()
