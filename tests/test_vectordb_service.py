"""The rules of the connection store: the default connection, keys, names, jobs.

The file handling is pinned in `test_vectordb_file.py` and the cipher in
`test_vectordb_crypto.py`; this is what the manager decides on top of them. The
databases are fakes behind one transport, told apart by host.
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

import pytest
import yaml

_CONFIG_DIR = tempfile.mkdtemp(prefix="papaia-connections-config-")
_WORKSPACE_DIR = tempfile.mkdtemp(prefix="papaia-connections-workspace-")

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

from app.config import Settings, get_settings  # noqa: E402
from app.core.audit import audit_path  # noqa: E402
from app.core.rag import INTEGRATED_URL  # noqa: E402
from app.core.vectordb import (  # noqa: E402
    ConnectionExistsError,
    ConnectionInUseError,
    InvalidConnectionError,
    ProbeEnv,
    ProtectedConnectionError,
    SecretMissingError,
    StaleConnectionError,
    UnknownConnectionError,
    crypto,
)
from app.core.vectordb import service as service_module  # noqa: E402
from app.core.vectordb.ingest_file import CONNECTIONS_RELPATH, IngestFileRepository  # noqa: E402
from app.core.vectordb.service import DEFAULT_NAME, ConnectionService  # noqa: E402
from tests.fake_qdrant import API_KEY, FakeQdrant, Fleet  # noqa: E402

_SECRET = "connections-secret"
_RAG_ENV = "COMPOSE_PROFILES=keycloak,rag\n"
# Where the manager reaches the integrated Qdrant in these tests; the store keeps the
# address the ingester uses.
_REACH = "http://qdrant.test:6333"


def _module_env(config_dir: Path, text: str) -> None:
    directory = config_dir / "ai" / "rag"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / ".env").write_text(text, encoding="utf-8")


@pytest.fixture(autouse=True)
def _forget_failed_seeds() -> None:
    service_module._seed_failed_at.clear()


@pytest.fixture
def config(tmp_path: Path) -> Path:
    directory = tmp_path / "config"
    (directory / "manager").mkdir(parents=True)
    (directory / CONNECTIONS_RELPATH.parent).mkdir(parents=True)
    (directory / ".env").write_text(_RAG_ENV, encoding="utf-8")
    _module_env(directory, f"QDRANT_JWT_SECRET={API_KEY}\nQI_CONNECTIONS_SECRET={_SECRET}\n")
    return directory


def _settings(config: Path) -> Settings:
    return get_settings().model_copy(
        update={"papaia_config_dir": str(config), "qdrant_url": _REACH}
    )


@pytest.fixture
def service(config: Path) -> ConnectionService:
    return ConnectionService(_settings(config))


@pytest.fixture
def fleet() -> Fleet:
    fleet = Fleet()
    fleet.add("qdrant.test", FakeQdrant(API_KEY))
    return fleet


def _env(fleet: Fleet) -> ProbeEnv:
    return ProbeEnv(transport=fleet.transport())


def _file(config: Path) -> dict[str, Any]:
    document = yaml.safe_load((config / CONNECTIONS_RELPATH).read_text(encoding="utf-8"))
    assert isinstance(document, dict)
    return document


def _entry(config: Path, name: str) -> dict[str, Any]:
    return next(e for e in _file(config)["connections"] if e["name"] == name)


def _write_file(config: Path, entries: list[dict[str, Any]]) -> None:
    (config / CONNECTIONS_RELPATH).write_text(
        yaml.safe_dump({"version": 1, "connections": entries}), encoding="utf-8"
    )


def _etag(service: ConnectionService, name: str) -> str:
    return next(c.etag for c in service.state().connections if c.name == name)


def _create(service: ConnectionService, name: str = "archive", **changes: Any) -> Any:
    args: dict[str, Any] = {
        "name": name,
        "type_id": "qdrant",
        "values": {"url": "http://archive.test:6333"},
        "api_key": None,
    }
    args.update(changes)
    return service.create(**args)


def _plain_key(config: Path, name: str) -> str:
    return crypto.decrypt(_entry(config, name)["api_key"], _SECRET)


# ---------------------------------------------------------------------------
# The default connection
# ---------------------------------------------------------------------------


def test_the_default_connection_is_created_for_the_ingester_not_for_the_manager(
    service: ConnectionService, config: Path
) -> None:
    # The manager reaches Qdrant at qdrant.test here; the ingester must get its own
    # address, or a development machine would hand it one it cannot resolve.
    assert service.ensure_default() == "seeded"

    entry = _entry(config, DEFAULT_NAME)
    assert set(entry) == {"name", "url", "api_key"}, "only the keys the ingester allows"
    assert entry["url"] == INTEGRATED_URL == "http://qdrant:6333"
    assert crypto.decrypt(entry["api_key"], _SECRET) == API_KEY


def test_creating_the_default_twice_changes_nothing(
    service: ConnectionService, config: Path
) -> None:
    service.ensure_default()
    before = (config / CONNECTIONS_RELPATH).read_bytes()

    assert service.ensure_default() == "present"

    assert (config / CONNECTIONS_RELPATH).read_bytes() == before


def test_an_existing_default_is_never_overwritten(
    service: ConnectionService, config: Path
) -> None:
    _write_file(config, [{"name": "default", "url": "http://my-own:6333"}])
    before = (config / CONNECTIONS_RELPATH).read_bytes()

    assert service.ensure_default() == "present"

    assert (config / CONNECTIONS_RELPATH).read_bytes() == before


def test_an_unusable_entry_named_default_is_left_alone_too(
    service: ConnectionService, config: Path
) -> None:
    _write_file(config, [{"name": "default", "url": "ftp://nowhere"}])
    before = (config / CONNECTIONS_RELPATH).read_bytes()

    assert service.ensure_default() == "present"

    assert (config / CONNECTIONS_RELPATH).read_bytes() == before


def test_the_creation_is_audited_as_the_manager(
    service: ConnectionService, config: Path
) -> None:
    service.ensure_default()
    service.ensure_default()

    lines = audit_path(str(config)).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert (entry["user"], entry["action"], entry["target"]) == (
        "manager",
        "rag.connection.seed",
        "default",
    )
    assert API_KEY not in lines[0] and _SECRET not in lines[0]


def test_nothing_is_created_without_the_rag_profile(
    service: ConnectionService, config: Path
) -> None:
    (config / ".env").write_text("COMPOSE_PROFILES=keycloak\n", encoding="utf-8")

    assert service.ensure_default() == "skipped"
    assert not (config / CONNECTIONS_RELPATH).exists()


@pytest.mark.parametrize(
    "env",
    ["QI_CONNECTIONS_SECRET=x\n", f"QDRANT_JWT_SECRET={API_KEY}\n", ""],
    ids=["no api-key", "no connections secret", "neither"],
)
def test_nothing_is_created_while_a_secret_is_missing(
    service: ConnectionService, config: Path, env: str
) -> None:
    _module_env(config, env)

    assert service.ensure_default() == "skipped"
    assert not (config / CONNECTIONS_RELPATH).exists()


def test_a_broken_file_is_not_touched_and_the_attempt_does_not_raise(
    service: ConnectionService, config: Path
) -> None:
    path = config / CONNECTIONS_RELPATH
    path.write_text("connections: [", encoding="utf-8")

    assert service.ensure_default() == "skipped"
    assert path.read_text(encoding="utf-8") == "connections: ["


def test_a_failed_attempt_never_raises_and_is_not_repeated_at_once(
    service: ConnectionService, config: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    attempts: list[int] = []

    def refuse(*_: Any, **__: Any) -> None:
        attempts.append(1)
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(os, "replace", refuse)

    assert service.ensure_default() == "skipped"
    assert service.ensure_default() == "skipped"
    assert len(attempts) == 1, "the second call is inside the retry delay"

    # After the delay the next request tries again.
    key = str(config)
    service_module._seed_failed_at[key] = time.monotonic() - 61
    monkeypatch.undo()
    assert service.ensure_default() == "seeded"


# ---------------------------------------------------------------------------
# Resolving a connection
# ---------------------------------------------------------------------------


def test_the_default_works_from_the_stacks_settings_before_it_is_stored(
    service: ConnectionService,
) -> None:
    connection = service.resolve(DEFAULT_NAME)

    assert (connection.url, connection.api_key, connection.source) == (_REACH, API_KEY, "env")
    assert connection.integrated


def test_the_default_without_any_key_is_a_connection_without_a_key(
    service: ConnectionService, config: Path
) -> None:
    _module_env(config, "")

    connection = service.resolve(DEFAULT_NAME)

    assert connection.api_key == "" and connection.key_state == "none"


def test_the_default_works_while_the_file_is_unusable(
    service: ConnectionService, config: Path
) -> None:
    (config / CONNECTIONS_RELPATH).write_text("connections: [", encoding="utf-8")

    assert service.resolve(DEFAULT_NAME).source == "env"
    with pytest.raises(UnknownConnectionError, match="invalid YAML"):
        service.resolve("archive")


def test_a_stored_connection_is_resolved_with_its_key_decrypted(
    service: ConnectionService, config: Path
) -> None:
    _create(service, api_key="archive-key")

    connection = service.resolve("archive")

    assert (connection.url, connection.api_key, connection.key_state) == (
        "http://archive.test:6333",
        "archive-key",
        "ok",
    )
    assert connection.source == "file" and not connection.integrated
    assert "archive-key" not in repr(connection), "the key stays out of every repr"


def test_the_integrated_qdrant_is_reached_where_the_manager_reaches_it(
    service: ConnectionService,
) -> None:
    service.ensure_default()

    connection = service.resolve(DEFAULT_NAME)
    view = next(c for c in service.state().connections if c.name == DEFAULT_NAME)

    assert connection.url == _REACH
    assert view.address == INTEGRATED_URL, "the page shows what the ingester uses"
    assert view.reach_address == _REACH


def test_an_unreadable_key_is_a_state_not_an_error(
    service: ConnectionService, config: Path
) -> None:
    _create(service, api_key="archive-key")
    _module_env(config, f"QDRANT_JWT_SECRET={API_KEY}\nQI_CONNECTIONS_SECRET=rotated\n")

    connection = service.resolve("archive")
    view = next(c for c in service.state().connections if c.name == "archive")

    assert (connection.api_key, connection.key_state) == ("", "unreadable")
    assert view.key_state == "unreadable" and view.has_key


def test_an_unknown_connection_is_unknown(service: ConnectionService) -> None:
    with pytest.raises(UnknownConnectionError, match="'nope'"):
        service.resolve("nope")


# ---------------------------------------------------------------------------
# The listing
# ---------------------------------------------------------------------------


def test_the_default_is_listed_first_and_marked(
    service: ConnectionService,
) -> None:
    _create(service, "aaa")
    service.ensure_default()

    state = service.state()

    assert [c.name for c in state.connections] == ["default", "aaa"]
    assert [c.is_default for c in state.connections] == [True, False]
    assert state.default_stored and state.secret_configured and state.writable


def test_a_view_never_carries_a_key(service: ConnectionService, config: Path) -> None:
    _create(service, api_key="archive-key")
    token = _entry(config, "archive")["api_key"]

    text = repr(service.state()) + json.dumps(
        [c.__dict__ for c in service.state().connections], default=str
    )

    assert "archive-key" not in text and token not in text
    assert next(c for c in service.state().connections if c.name == "archive").has_key


def test_a_key_that_drifted_from_the_stacks_is_flagged_on_the_default_only(
    service: ConnectionService, config: Path
) -> None:
    service.ensure_default()
    _create(service, api_key="archive-key")
    _module_env(config, f"QDRANT_JWT_SECRET=rotated-key\nQI_CONNECTIONS_SECRET={_SECRET}\n")

    drift = {c.name: c.key_drift for c in service.state().connections}

    assert drift == {"default": True, "archive": False}


def test_a_connection_to_the_integrated_qdrant_is_where_roles_are_enforced(
    service: ConnectionService,
) -> None:
    _create(service, "same", values={"url": "http://qdrant:6333/"})
    _create(service, "other")

    integrated = {c.name: c.integrated for c in service.state().connections}

    assert integrated == {"same": True, "other": False}


def test_entries_the_ingester_would_reject_are_reported_not_listed(
    service: ConnectionService, config: Path
) -> None:
    _write_file(
        config,
        [{"name": "fine", "url": "http://x"}, {"name": "bad", "url": "ftp://y"}],
    )

    state = service.state()

    assert [c.name for c in state.connections] == ["fine"]
    assert any("bad" in issue and "url" in issue for issue in state.issues)


def test_the_jobs_that_use_a_connection_are_listed(
    service: ConnectionService, config: Path
) -> None:
    _create(service)
    (config / "ai/rag/catalog/jobs.yaml").write_text(
        "version: 1\njobs:\n- id: nightly\n  target:\n    connection: archive\n"
        "- id: weekly\n  enabled: false\n  target:\n    connection: archive\n"
        "- id: other\n  target:\n    connection: default\n",
        encoding="utf-8",
    )

    view = next(c for c in service.state().connections if c.name == "archive")

    assert view.used_by == ("nightly", "weekly")


# ---------------------------------------------------------------------------
# Creating
# ---------------------------------------------------------------------------


def test_a_connection_is_stored_with_an_encrypted_key(
    service: ConnectionService, config: Path
) -> None:
    view = _create(service, api_key="archive-key")

    entry = _entry(config, "archive")
    assert entry["api_key"].startswith("enc:1:") and "archive-key" not in entry["api_key"]
    assert _plain_key(config, "archive") == "archive-key"
    assert set(entry) == {"name", "url", "api_key"}
    assert view.name == "archive" and view.has_key and not view.is_default


def test_a_connection_without_a_key_has_no_key_entry(
    service: ConnectionService, config: Path
) -> None:
    _create(service, api_key="  ")

    assert _entry(config, "archive") == {"name": "archive", "url": "http://archive.test:6333"}


def test_a_name_that_is_taken_is_refused(service: ConnectionService) -> None:
    _create(service)

    with pytest.raises(ConnectionExistsError):
        _create(service)


@pytest.mark.parametrize("name", ["", "Archive", "-a", "a b", "a" * 65, "ä", "a/b", "a\n"])
def test_a_name_the_ingester_would_refuse_is_refused(
    service: ConnectionService, name: str
) -> None:
    with pytest.raises(InvalidConnectionError, match="lowercase"):
        _create(service, name)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "   ",
        "qdrant:6333",
        "ftp://x",
        "HTTP://x",
        "http://",
        "http://user:pw@host",
        "http://user@host",
        "http://ho st",
        "http://ho\nst",
        "http://host:notaport",
        "http://" + "a" * 2050,
    ],
)
def test_an_address_that_is_not_worth_storing_is_refused(
    service: ConnectionService, config: Path, url: str
) -> None:
    with pytest.raises(InvalidConnectionError):
        _create(service, values={"url": url})

    assert not (config / CONNECTIONS_RELPATH).exists()


def test_an_address_may_carry_a_path_and_is_stored_as_typed(
    service: ConnectionService, config: Path
) -> None:
    _create(service, values={"url": "  https://proxy.test/qdrant/  "})

    assert _entry(config, "archive")["url"] == "https://proxy.test/qdrant/"


def test_a_key_cannot_be_stored_without_the_secret_but_a_connection_without_one_can(
    service: ConnectionService, config: Path
) -> None:
    _module_env(config, f"QDRANT_JWT_SECRET={API_KEY}\n")

    with pytest.raises(SecretMissingError, match="QI_CONNECTIONS_SECRET"):
        _create(service, api_key="archive-key")
    _create(service, "open")

    assert [c.name for c in service.state().connections] == ["open"]
    assert not service.state().secret_configured


def test_an_unknown_type_is_refused(service: ConnectionService) -> None:
    with pytest.raises(InvalidConnectionError, match="Unknown connection type"):
        _create(service, type_id="pinecone")


# ---------------------------------------------------------------------------
# Changing
# ---------------------------------------------------------------------------


def _update(service: ConnectionService, name: str = "archive", **changes: Any) -> Any:
    args: dict[str, Any] = {
        "values": {"url": "http://archive.test:6333"},
        "api_key": None,
        "clear_api_key": False,
        "confirm_jobs": False,
    }
    args.update(changes)
    if "etag" not in args:
        args["etag"] = _etag(service, name)
    return service.update(name, **args)


def test_a_key_that_is_not_touched_keeps_its_token_byte_for_byte(
    service: ConnectionService, config: Path
) -> None:
    _create(service, api_key="archive-key")
    token = _entry(config, "archive")["api_key"]

    _update(service)

    assert _entry(config, "archive")["api_key"] == token, "re-encrypting would rewrite the file"


def test_a_key_is_replaced_or_removed_on_request(
    service: ConnectionService, config: Path
) -> None:
    _create(service, api_key="old-key")

    _update(service, api_key="new-key")
    assert _plain_key(config, "archive") == "new-key"

    _update(service, clear_api_key=True)
    assert "api_key" not in _entry(config, "archive")


def test_a_new_key_and_removing_the_key_together_make_no_sense(
    service: ConnectionService,
) -> None:
    _create(service, api_key="old-key")

    with pytest.raises(InvalidConnectionError, match="not both"):
        _update(service, api_key="new-key", clear_api_key=True)


def test_a_stored_key_is_not_carried_to_a_different_address(
    service: ConnectionService, config: Path
) -> None:
    _create(service, api_key="archive-key")
    before = (config / CONNECTIONS_RELPATH).read_bytes()

    with pytest.raises(InvalidConnectionError, match="never sent to a different one"):
        _update(service, values={"url": "http://attacker.test:6333"})

    assert (config / CONNECTIONS_RELPATH).read_bytes() == before


def test_the_address_may_change_with_a_new_key_or_with_the_key_removed(
    service: ConnectionService, config: Path
) -> None:
    _create(service, api_key="archive-key")

    _update(service, values={"url": "http://moved.test:6333"}, api_key="moved-key")
    assert _plain_key(config, "archive") == "moved-key"

    _update(service, values={"url": "http://again.test:6333"}, clear_api_key=True)
    assert _entry(config, "archive") == {"name": "archive", "url": "http://again.test:6333"}


def test_a_connection_without_a_key_can_move_freely(
    service: ConnectionService, config: Path
) -> None:
    _create(service)

    _update(service, values={"url": "http://moved.test:6333"})

    assert _entry(config, "archive")["url"] == "http://moved.test:6333"


def test_a_change_keeps_the_place_of_the_entry_and_its_neighbours(
    service: ConnectionService, config: Path
) -> None:
    _create(service, "first")
    _create(service, "second")
    _create(service, "third")

    _update(service, "second", values={"url": "http://moved.test:6333"})

    assert [e["name"] for e in _file(config)["connections"]] == ["first", "second", "third"]


def test_a_change_to_an_entry_somebody_else_changed_is_refused(
    service: ConnectionService, config: Path
) -> None:
    _create(service)
    etag = _etag(service, "archive")
    _write_file(config, [{"name": "archive", "url": "http://theirs.test:6333"}])

    with pytest.raises(StaleConnectionError):
        _update(service, etag=etag)

    assert _entry(config, "archive")["url"] == "http://theirs.test:6333"


def test_an_unrelated_write_does_not_make_a_change_stale(
    service: ConnectionService, config: Path
) -> None:
    _create(service)
    etag = _etag(service, "archive")
    _create(service, "other")  # the file changed, this entry did not

    _update(service, etag=etag, values={"url": "http://moved.test:6333"})

    assert _entry(config, "archive")["url"] == "http://moved.test:6333"


def test_changing_something_that_is_gone_is_a_404(service: ConnectionService) -> None:
    _create(service)
    etag = _etag(service, "archive")

    with pytest.raises(UnknownConnectionError):
        _update(service, "gone", etag=etag)


def _jobs(config: Path, text: str) -> None:
    (config / "ai/rag/catalog/jobs.yaml").write_text(text, encoding="utf-8")


_ONE_JOB = "version: 1\njobs:\n- id: nightly\n  target:\n    connection: archive\n"


def test_moving_a_connection_that_jobs_use_needs_a_confirmation_naming_them(
    service: ConnectionService, config: Path
) -> None:
    _create(service)
    _jobs(config, _ONE_JOB)

    with pytest.raises(ConnectionInUseError, match="nightly") as caught:
        _update(service, values={"url": "http://moved.test:6333"})
    assert caught.value.jobs == ("nightly",)
    assert _entry(config, "archive")["url"] == "http://archive.test:6333"

    _update(service, values={"url": "http://moved.test:6333"}, confirm_jobs=True)
    assert _entry(config, "archive")["url"] == "http://moved.test:6333"


def test_the_confirmation_is_not_asked_when_the_address_stays(
    service: ConnectionService, config: Path
) -> None:
    _create(service, api_key="archive-key")
    _jobs(config, _ONE_JOB)

    _update(service, api_key="new-key")

    assert _plain_key(config, "archive") == "new-key"


def test_a_jobs_file_that_cannot_be_read_asks_for_the_confirmation_too(
    service: ConnectionService, config: Path
) -> None:
    _create(service)
    _jobs(config, "jobs: [")

    with pytest.raises(ConnectionInUseError, match="not valid YAML"):
        _update(service, values={"url": "http://moved.test:6333"})


def test_the_default_can_be_edited_but_keeps_its_name(
    service: ConnectionService, config: Path
) -> None:
    service.ensure_default()

    view = _update(service, DEFAULT_NAME, values={"url": INTEGRATED_URL}, api_key="other-key")

    assert view.name == "default" and view.is_default
    assert _plain_key(config, "default") == "other-key"


# ---------------------------------------------------------------------------
# Deleting
# ---------------------------------------------------------------------------


def test_a_connection_is_deleted_and_the_others_stay(
    service: ConnectionService, config: Path
) -> None:
    _create(service, "one")
    _create(service, "two")

    service.delete("one", etag=_etag(service, "one"))

    assert [e["name"] for e in _file(config)["connections"]] == ["two"]


def test_the_default_connection_cannot_be_deleted(
    service: ConnectionService, config: Path
) -> None:
    service.ensure_default()

    with pytest.raises(ProtectedConnectionError, match="reset"):
        service.delete(DEFAULT_NAME, etag=_etag(service, DEFAULT_NAME))

    assert _entry(config, "default")


def test_a_connection_that_jobs_use_cannot_be_deleted(
    service: ConnectionService, config: Path
) -> None:
    _create(service)
    _jobs(config, _ONE_JOB)

    with pytest.raises(ConnectionInUseError, match="nightly"):
        service.delete("archive", etag=_etag(service, "archive"))

    assert _entry(config, "archive")


@pytest.mark.parametrize("text", ["jobs: [", "- a\n- b\n", "jobs: nope\n"])
def test_a_deletion_is_not_guessed_when_the_jobs_cannot_be_read(
    service: ConnectionService, config: Path, text: str
) -> None:
    _create(service)
    _jobs(config, text)

    with pytest.raises(ConnectionInUseError, match="cannot be checked"):
        service.delete("archive", etag=_etag(service, "archive"))

    assert _entry(config, "archive")


def test_without_a_jobs_file_nothing_uses_a_connection(
    service: ConnectionService, config: Path
) -> None:
    _create(service)

    service.delete("archive", etag=_etag(service, "archive"))

    assert _file(config)["connections"] == []


def test_deleting_what_changed_or_is_gone_is_refused(
    service: ConnectionService, config: Path
) -> None:
    _create(service)
    etag = _etag(service, "archive")
    _write_file(config, [{"name": "archive", "url": "http://theirs.test:6333"}])

    with pytest.raises(StaleConnectionError):
        service.delete("archive", etag=etag)
    with pytest.raises(UnknownConnectionError):
        service.delete("gone", etag=etag)


# ---------------------------------------------------------------------------
# Resetting the default
# ---------------------------------------------------------------------------


def test_a_reset_points_the_default_at_the_integrated_qdrant_again(
    service: ConnectionService, config: Path
) -> None:
    service.ensure_default()
    _update(service, DEFAULT_NAME, values={"url": "http://elsewhere:6333"}, api_key="stale")

    view = service.reset_default()

    assert _entry(config, "default")["url"] == INTEGRATED_URL
    assert _plain_key(config, "default") == API_KEY
    assert not view.key_drift


def test_a_reset_after_a_rotation_replaces_the_key(
    service: ConnectionService, config: Path
) -> None:
    service.ensure_default()
    _module_env(config, f"QDRANT_JWT_SECRET=rotated-key\nQI_CONNECTIONS_SECRET={_SECRET}\n")
    assert next(c for c in service.state().connections if c.is_default).key_drift

    service.reset_default()

    assert _plain_key(config, "default") == "rotated-key"
    assert not next(c for c in service.state().connections if c.is_default).key_drift


def test_a_reset_creates_the_default_when_it_is_missing(
    service: ConnectionService, config: Path
) -> None:
    _create(service)

    service.reset_default()

    assert [e["name"] for e in _file(config)["connections"]] == ["archive", "default"]


@pytest.mark.parametrize("env", ["QDRANT_JWT_SECRET=k\n", f"QI_CONNECTIONS_SECRET={_SECRET}\n"])
def test_a_reset_needs_both_secrets(
    service: ConnectionService, config: Path, env: str
) -> None:
    _module_env(config, env)

    with pytest.raises(SecretMissingError):
        service.reset_default()


# ---------------------------------------------------------------------------
# Testing a connection
# ---------------------------------------------------------------------------


async def test_a_stored_connection_is_tested_with_its_own_key(
    service: ConnectionService, fleet: Fleet
) -> None:
    fleet.add("archive.test", FakeQdrant("archive-key")).add("books")
    _create(service, api_key="archive-key")

    result = await service.test(
        name="archive", type_id="qdrant", values={}, api_key=None, env=_env(fleet)
    )

    assert result.ok and result.collections == 1
    assert "1 collection" in result.detail


async def test_the_default_is_tested_where_the_manager_reaches_it(
    service: ConnectionService, fleet: Fleet
) -> None:
    service.ensure_default()

    result = await service.test(
        name=DEFAULT_NAME, type_id="qdrant", values={}, api_key=None, env=_env(fleet)
    )

    assert result.ok


async def test_a_wrong_key_is_reported_with_the_name_of_the_connection_and_not_the_key(
    service: ConnectionService, fleet: Fleet
) -> None:
    fleet.add("archive.test", FakeQdrant("the-real-key"))
    _create(service, api_key="wrong-key")

    result = await service.test(
        name="archive", type_id="qdrant", values={}, api_key=None, env=_env(fleet)
    )

    assert not result.ok
    assert "refused the api-key" in result.detail and "'archive'" in result.detail
    assert "wrong-key" not in result.detail and "the-real-key" not in result.detail


async def test_an_unreachable_database_is_a_result_not_an_error(
    service: ConnectionService, fleet: Fleet
) -> None:
    _create(service)  # nothing answers at archive.test in the fleet

    result = await service.test(
        name="archive", type_id="qdrant", values={}, api_key=None, env=_env(fleet)
    )

    assert not result.ok and "not reachable" in result.detail


async def test_a_stored_key_is_never_sent_to_an_address_that_came_with_the_request(
    service: ConnectionService, fleet: Fleet
) -> None:
    attacker = fleet.add("attacker.test", FakeQdrant("anything"))
    _create(service, api_key="archive-key")

    with pytest.raises(InvalidConnectionError, match="Enter the api-key"):
        await service.test(
            name="archive",
            type_id="qdrant",
            values={"url": "http://attacker.test:6333"},
            api_key=None,
            env=_env(fleet),
        )

    assert attacker.calls == []


async def test_the_stored_address_may_be_sent_along_unchanged(
    service: ConnectionService, fleet: Fleet
) -> None:
    fleet.add("archive.test", FakeQdrant("archive-key"))
    _create(service, api_key="archive-key")

    result = await service.test(
        name="archive",
        type_id="qdrant",
        values={"url": "http://archive.test:6333/"},
        api_key=None,
        env=_env(fleet),
    )

    assert result.ok


async def test_a_typed_key_may_test_a_different_address(
    service: ConnectionService, fleet: Fleet
) -> None:
    other = fleet.add("other.test", FakeQdrant("typed-key"))
    _create(service, api_key="archive-key")

    result = await service.test(
        name="archive",
        type_id="qdrant",
        values={"url": "http://other.test:6333"},
        api_key="typed-key",
        env=_env(fleet),
    )

    assert result.ok and other.calls, "the typed key went to the typed address"


async def test_what_was_typed_for_a_new_connection_is_tested_without_being_stored(
    service: ConnectionService, fleet: Fleet, config: Path
) -> None:
    fleet.add("new.test", FakeQdrant("typed-key"))

    result = await service.test(
        name=None,
        type_id="qdrant",
        values={"url": "http://new.test:6333"},
        api_key="typed-key",
        env=_env(fleet),
    )

    assert result.ok
    assert not (config / CONNECTIONS_RELPATH).exists()


async def test_a_key_that_cannot_be_read_is_explained_without_a_request(
    service: ConnectionService, fleet: Fleet, config: Path
) -> None:
    archive = fleet.add("archive.test", FakeQdrant("archive-key"))
    _create(service, api_key="archive-key")
    _module_env(config, f"QDRANT_JWT_SECRET={API_KEY}\nQI_CONNECTIONS_SECRET=rotated\n")

    result = await service.test(
        name="archive", type_id="qdrant", values={}, api_key=None, env=_env(fleet)
    )

    assert not result.ok and "cannot be decrypted" in result.detail
    assert archive.calls == []


# ---------------------------------------------------------------------------
# The repository next to the service
# ---------------------------------------------------------------------------


def test_the_ingester_can_still_edit_what_the_manager_wrote(
    service: ConnectionService, config: Path
) -> None:
    """The ingester's own interface edits the same file; its edit must survive the manager."""
    service.ensure_default()
    repo = IngestFileRepository(config)

    def ingest_adds(document: dict[str, Any]) -> None:
        document["connections"].append({"name": "from-ingest", "url": "http://i:6333"})

    repo.update(ingest_adds)
    _create(service)

    assert [c.name for c in service.state().connections] == ["default", "archive", "from-ingest"]
