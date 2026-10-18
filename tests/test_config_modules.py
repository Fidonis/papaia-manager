"""The optional modules: how the state is read, the `setup` options a switch becomes, and what a
request may ask for.

The core stores the choice (`COMPOSE_PROFILES`, `LOCALAI_IMAGE_VARIANT`); these tests pin that the
manager reads that, builds nothing but the options of one module, and refuses the rest before a job
is queued. The job itself is `test_config_service.py`, the HTTP surface `test_api_config.py`.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from app.core import ctl
from app.core.config import modules
from app.core.ctl import LOCALAI_VARIANTS, ModuleSetup, module_setup_flags


def _env(config_dir: Path, text: str) -> None:
    config_dir.mkdir(parents=True, exist_ok=True)
    (config_dir / ".env").write_text(text, encoding="utf-8")


# -- the options a switch becomes ------------------------------------------------------


@pytest.mark.parametrize("variant", LOCALAI_VARIANTS)
def test_localai_on_names_the_variant(variant: str) -> None:
    assert module_setup_flags(ModuleSetup("localai", True, variant)) == [
        "--enable-local-ai=true",
        f"--localai-variant={variant}",
    ]


def test_localai_off_takes_no_variant() -> None:
    assert module_setup_flags(ModuleSetup("localai", False)) == ["--enable-local-ai=false"]
    with pytest.raises(ValueError, match="no variant"):
        module_setup_flags(ModuleSetup("localai", False, "cpu"))


@pytest.mark.parametrize("variant", [None, "", "vulkan", "auto", "cpu; rm -rf /", "--force"])
def test_localai_on_refuses_a_variant_it_does_not_offer(variant: str | None) -> None:
    with pytest.raises(ValueError, match="variant"):
        module_setup_flags(ModuleSetup("localai", True, variant))


def test_web_search_is_a_plain_switch() -> None:
    assert module_setup_flags(ModuleSetup("web-search", True)) == ["--enable-web-search=true"]
    assert module_setup_flags(ModuleSetup("web-search", False)) == ["--enable-web-search=false"]
    with pytest.raises(ValueError, match="only LocalAI"):
        module_setup_flags(ModuleSetup("web-search", True, "cpu"))


def test_the_rag_system_is_a_plain_switch() -> None:
    assert module_setup_flags(ModuleSetup("rag", True)) == ["--enable-rag=true"]
    assert module_setup_flags(ModuleSetup("rag", False)) == ["--enable-rag=false"]
    with pytest.raises(ValueError, match="only LocalAI"):
        module_setup_flags(ModuleSetup("rag", True, "cpu"))


@pytest.mark.parametrize("name", ["manager", "mail", "keycloak", "../x", ""])
def test_no_other_module_can_be_switched_with_a_module_setup(name: str) -> None:
    # The mail relay is switched with `SmtpSetup`, which also carries its server settings.
    with pytest.raises(ValueError, match="cannot be switched"):
        module_setup_flags(ModuleSetup(name, True))


def test_setup_gets_nothing_but_the_options_of_the_module(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, Any] = {}

    async def fake(**kwargs: Any) -> tuple[int, str, str]:
        seen.update(kwargs)
        return 0, "", ""

    monkeypatch.setattr(ctl, "_run_py", fake)
    asyncio.run(
        ctl.run_module_setup(
            ModuleSetup("localai", True, "cpu"), workspace_dir="/w", config_dir="/c"
        )
    )
    assert seen["command"] == "setup"
    assert seen["extra_flags"] == ["--enable-local-ai=true", "--localai-variant=cpu"]
    for flag in seen["extra_flags"]:
        assert not flag.startswith(("--force", "--env", "--host", "--app-host"))


def test_the_read_only_commands_and_the_two_writers_stay_apart() -> None:
    assert {"render-preview", "module-info"} <= ctl.ALLOWED_PY_COMMANDS
    assert frozenset({"setup", "module-purge"}) == ctl.ALLOWED_PY_WRITE_COMMANDS
    assert not ctl.ALLOWED_PY_COMMANDS & ctl.ALLOWED_PY_WRITE_COMMANDS


# -- the state ----------------------------------------------------------------------------


def test_the_state_comes_from_compose_profiles(tmp_path: Path) -> None:
    _env(tmp_path, "COMPOSE_PROFILES=nginx,librechat,librechat-websearch,smtp\n")
    on = {row["name"]: row["enabled"] for row in modules.state(str(tmp_path), str(tmp_path / "w"))}
    assert on == {
        "localai": False,
        "web-search": True,
        "rag": False,
        "mail": True,
        "manager": False,
    }


def test_the_four_optional_modules_are_switchable_and_the_manager_is_not() -> None:
    assert set(modules.SWITCHABLE) == {"localai", "web-search", "rag", "mail"}
    assert set(modules.SWITCHABLE) == set(ctl.PURGE_MODULES)
    rows = modules.state("/nonexistent", "/nonexistent")
    by_name = {r["name"]: r for r in rows}
    assert by_name["manager"]["switchable"] is False
    assert by_name["manager"]["note"]


def test_the_variant_defaults_to_cpu_and_is_read_back(tmp_path: Path) -> None:
    _env(tmp_path, "COMPOSE_PROFILES=localai\n")
    assert modules.stored_variant(str(tmp_path)) == "cpu"
    _env(tmp_path, "COMPOSE_PROFILES=localai\nLOCALAI_IMAGE_VARIANT=nvidia-cuda-12\n")
    assert modules.stored_variant(str(tmp_path)) == "nvidia-cuda-12"


def test_the_revision_changes_with_the_profiles_and_the_variant(tmp_path: Path) -> None:
    _env(tmp_path, "COMPOSE_PROFILES=nginx\n")
    first = modules.revision(str(tmp_path))
    _env(tmp_path, "COMPOSE_PROFILES=nginx,localai\n")
    second = modules.revision(str(tmp_path))
    _env(tmp_path, "COMPOSE_PROFILES=nginx,localai\nLOCALAI_IMAGE_VARIANT=intel\n")
    third = modules.revision(str(tmp_path))
    assert len({first, second, third}) == 3


def test_the_revision_ignores_the_order_of_the_profiles(tmp_path: Path) -> None:
    _env(tmp_path, "COMPOSE_PROFILES=a,b\n")
    one = modules.revision(str(tmp_path))
    _env(tmp_path, "COMPOSE_PROFILES=b,a\n")
    assert modules.revision(str(tmp_path)) == one


def test_the_service_count_is_read_from_the_compose_fragments(tmp_path: Path) -> None:
    src = tmp_path / "papaia" / "src"
    (src / "ai").mkdir(parents=True)
    (src / "docker-compose.yml").write_text(
        "include:\n  - ai/docker-compose.yml\n", encoding="utf-8"
    )
    (src / "ai" / "docker-compose.yml").write_text(
        "services:\n"
        "  a:\n    profiles: [localai]\n    labels:\n      de.fidonis.module: papaia-localai\n"
        "  b:\n    profiles: [localai]\n    labels:\n      de.fidonis.module: papaia-localai\n"
        "  c:\n    profiles: [other]\n",
        encoding="utf-8",
    )
    assert modules.service_count(str(tmp_path), "localai") == (2, 1)
    assert modules.service_count(str(tmp_path), "nothing") == (0, 0)


# -- what a request may ask ---------------------------------------------------------------


def test_enabling_localai_needs_a_variant_the_page_offers() -> None:
    assert modules.validate_enable("localai", "intel") == "intel"
    with pytest.raises(modules.ModuleError, match="Choose"):
        modules.validate_enable("localai", None)
    with pytest.raises(modules.ModuleError, match="set on the host"):
        modules.validate_enable("localai", "vulkan")
    with pytest.raises(modules.ModuleError, match="Unknown"):
        modules.validate_enable("localai", "quantum")


def test_enabling_anything_else_takes_no_variant() -> None:
    for name in ("web-search", "rag", "mail"):
        assert modules.validate_enable(name, None) is None
        with pytest.raises(modules.ModuleError, match="Only LocalAI"):
            modules.validate_enable(name, "cpu")
    for name in ("manager", "nope"):
        with pytest.raises(modules.ModuleError, match="can't be switched"):
            modules.validate_enable(name, None)


# -- a core that ships (or lacks) the module operations -----------------------------------


def _core(workspace: Path, *, operations: bool = True, profiles: tuple[str, ...] = ()) -> None:
    """A workspace whose `papaia/` has Compose fragments for `profiles` (one service each) and,
    with `operations`, the core's `tools/lib/modules.py`."""
    src = workspace / "papaia" / "src"
    src.mkdir(parents=True)
    (src / "docker-compose.yml").write_text(
        "include:\n  - m/docker-compose.yml\n", encoding="utf-8"
    )
    (src / "m").mkdir()
    services = "".join(
        f"  svc-{p}:\n    profiles: [{p}]\n    labels:\n      de.fidonis.module: papaia-{p}\n"
        for p in profiles
    )
    (src / "m" / "docker-compose.yml").write_text(f"services:\n{services}", encoding="utf-8")
    if operations:
        lib = workspace / "papaia" / "tools" / "lib"
        lib.mkdir(parents=True)
        (lib / "modules.py").write_text("", encoding="utf-8")


def test_the_core_manages_modules_when_it_ships_the_module_operations(tmp_path: Path) -> None:
    _core(tmp_path / "new")
    _core(tmp_path / "old", operations=False)
    assert modules.core_manages_modules(str(tmp_path / "new")) is True
    assert modules.core_manages_modules(str(tmp_path / "old")) is False


def test_rag_and_mail_need_a_core_with_the_module_operations(tmp_path: Path) -> None:
    config = tmp_path / "config"
    _env(config, "COMPOSE_PROFILES=nginx\n")
    _core(tmp_path / "w", operations=False, profiles=("rag", "smtp", "localai"))
    rows = {r["name"]: r for r in modules.state(str(config), str(tmp_path / "w"))}
    assert rows["localai"]["switchable"] is True
    for name in ("rag", "mail"):
        assert rows[name]["switchable"] is False
        assert "too old" in str(rows[name]["problem"])
        assert rows[name]["can_delete"] is False


def test_a_profile_the_core_does_not_have_cannot_be_switched(tmp_path: Path) -> None:
    config = tmp_path / "config"
    _env(config, "COMPOSE_PROFILES=nginx\n")
    _core(tmp_path / "w", profiles=("localai",))
    rows = {r["name"]: r for r in modules.state(str(config), str(tmp_path / "w"))}
    assert rows["localai"]["switchable"] is True
    assert rows["rag"]["switchable"] is False
    assert "too old" in str(rows["rag"]["problem"])


def test_a_module_that_is_on_can_always_be_switched_off(tmp_path: Path) -> None:
    config = tmp_path / "config"
    _env(config, "COMPOSE_PROFILES=nginx,rag\n")
    _core(tmp_path / "w", operations=False, profiles=("rag",))
    rows = {r["name"]: r for r in modules.state(str(config), str(tmp_path / "w"))}
    assert rows["rag"]["switchable"] is True
    assert rows["rag"]["can_delete"] is False


def test_the_rag_link_is_offered_only_while_the_rag_system_is_on(tmp_path: Path) -> None:
    config = tmp_path / "config"
    _core(tmp_path / "w", profiles=("rag", "smtp"))
    _env(config, "COMPOSE_PROFILES=nginx\n")
    rows = {r["name"]: r for r in modules.state(str(config), str(tmp_path / "w"))}
    assert rows["rag"]["link"] == ""
    assert rows["mail"]["link"] == "/mail/server"
    _env(config, "COMPOSE_PROFILES=nginx,rag\n")
    rows = {r["name"]: r for r in modules.state(str(config), str(tmp_path / "w"))}
    assert rows["rag"]["link"] == "/connections"


# -- what the core would refuse, asked first ----------------------------------------------


def _deployment(config: Path, *entries: tuple[str, bool]) -> None:
    config.mkdir(parents=True, exist_ok=True)
    body = "".join(f"  - name: {n}\n    active: {str(a).lower()}\n" for n, a in entries)
    (config / "deployment.yaml").write_text(f"addons:\n{body}", encoding="utf-8")


def test_an_active_addon_with_the_same_services_blocks_the_rag_system(tmp_path: Path) -> None:
    config = tmp_path / "config"
    _env(config, "COMPOSE_PROFILES=nginx\n")
    _core(tmp_path / "w", profiles=("rag",))
    _deployment(config, ("qdrant-ingest", True), ("qdrant", False), ("other", True))
    assert modules.conflicting_addons(str(config)) == ["qdrant-ingest"]
    with pytest.raises(modules.ModuleError, match="qdrant-ingest"):
        modules.check_enable("rag", str(config), str(tmp_path / "w"))


def test_an_inactive_addon_does_not_block_the_rag_system(tmp_path: Path) -> None:
    config = tmp_path / "config"
    _env(config, "COMPOSE_PROFILES=nginx\n")
    _core(tmp_path / "w", profiles=("rag",))
    _deployment(config, ("qdrant", False))
    modules.check_enable("rag", str(config), str(tmp_path / "w"))


def test_the_mail_relay_needs_a_mail_server_to_switch_on(tmp_path: Path) -> None:
    config = tmp_path / "config"
    _env(config, "COMPOSE_PROFILES=nginx\n")
    _core(tmp_path / "w", profiles=("smtp",))
    with pytest.raises(modules.ModuleError, match="No mail server"):
        modules.check_enable("mail", str(config), str(tmp_path / "w"))
    module_env = config / "infra" / "smtp-relay" / ".env"
    module_env.parent.mkdir(parents=True)
    module_env.write_text(
        "SMTP_RELAY_UPSTREAM_HOST=smtp.example.org\nSMTP_RELAY_FROM_ADDRESS=a@example.org\n",
        encoding="utf-8",
    )
    modules.check_enable("mail", str(config), str(tmp_path / "w"))


def test_enabling_on_a_core_without_the_module_operations_is_refused(tmp_path: Path) -> None:
    config = tmp_path / "config"
    _env(config, "COMPOSE_PROFILES=nginx\n")
    _core(tmp_path / "w", operations=False, profiles=("rag",))
    with pytest.raises(modules.ModuleError, match="too old"):
        modules.check_enable("rag", str(config), str(tmp_path / "w"))


def test_a_module_the_core_has_no_services_for_is_refused(tmp_path: Path) -> None:
    config = tmp_path / "config"
    _env(config, "COMPOSE_PROFILES=nginx\n")
    _core(tmp_path / "w", profiles=("localai",))
    with pytest.raises(modules.ModuleError, match="has no Web search"):
        modules.check_enable("web-search", str(config), str(tmp_path / "w"))


# -- what a switch-off may delete ---------------------------------------------------------


def test_keeping_everything_needs_no_confirmation(tmp_path: Path) -> None:
    assert modules.validate_scope("rag", "keep", None, str(tmp_path)) == "keep"


@pytest.mark.parametrize("scope", ["data", "all"])
def test_a_deletion_needs_the_modules_name_typed_back(tmp_path: Path, scope: str) -> None:
    _core(tmp_path / "w")
    assert modules.validate_scope("rag", scope, " rag ", str(tmp_path / "w")) == scope
    for wrong in (None, "", "RAG", "mail", "rag data"):
        with pytest.raises(modules.ModuleError, match="Type the module name"):
            modules.validate_scope("rag", scope, wrong, str(tmp_path / "w"))


def test_no_fourth_state_exists(tmp_path: Path) -> None:
    # Settings alone are not offered: they can't be deleted with the data kept.
    assert modules.SCOPES == ("keep", "data", "all")
    for scope in ("config", "settings", "", "ALL", "data,config"):
        with pytest.raises(modules.ModuleError, match="Choose"):
            modules.validate_scope("rag", scope, "rag", str(tmp_path))


def test_a_core_that_cannot_delete_refuses_a_deletion(tmp_path: Path) -> None:
    _core(tmp_path / "w", operations=False)
    with pytest.raises(modules.ModuleError, match="can't delete"):
        modules.validate_scope("rag", "data", "rag", str(tmp_path / "w"))
    assert modules.validate_scope("rag", "keep", None, str(tmp_path / "w")) == "keep"


# -- what the core reports about a module -------------------------------------------------


def _answer(monkeypatch: pytest.MonkeyPatch, code: int, out: str, err: str = "") -> list[str]:
    asked: list[str] = []

    async def fake(name: str, **kwargs: Any) -> tuple[int, str, str]:
        asked.append(name)
        return code, out, err

    monkeypatch.setattr(modules, "run_module_info", fake)
    return asked


async def test_the_info_is_the_cores_document_marked_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _core(tmp_path / "w")
    document = '{"module": "rag", "volumes": [{"key": "q", "name": "p_q", "exists": true}]}'
    asked = _answer(monkeypatch, 0, document)
    info = await modules.module_info("rag", workspace_dir=str(tmp_path / "w"), config_dir="/c")
    assert asked == ["rag"]
    assert info["available"] is True
    assert info["volumes"][0]["name"] == "p_q"


@pytest.mark.parametrize(
    ("code", "out", "err", "reason"),
    [
        (2, "", "ERROR: unknown module", "unknown module"),
        (0, "not json", "", "could not be read"),
        (0, "[1, 2]", "", "could not be read"),
    ],
)
async def test_an_answer_that_is_not_usable_leaves_the_plain_switch_off(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, code: int, out: str, err: str, reason: str
) -> None:
    _core(tmp_path / "w")
    _answer(monkeypatch, code, out, err)
    info = await modules.module_info("rag", workspace_dir=str(tmp_path / "w"), config_dir="/c")
    assert info["available"] is False
    assert reason in info["reason"]


async def test_a_core_without_the_operations_is_not_asked(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _core(tmp_path / "w", operations=False)
    asked = _answer(monkeypatch, 0, "{}")
    info = await modules.module_info("rag", workspace_dir=str(tmp_path / "w"), config_dir="/c")
    assert info["available"] is False
    assert asked == []


async def test_a_child_that_cannot_run_is_not_available(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _core(tmp_path / "w")

    async def broken(name: str, **kwargs: Any) -> tuple[int, str, str]:
        raise ctl.CtlError("timed out", exit_code=124)

    monkeypatch.setattr(modules, "run_module_info", broken)
    info = await modules.module_info("mail", workspace_dir=str(tmp_path / "w"), config_dir="/c")
    assert info["available"] is False
    assert "did not answer" in info["reason"]


async def test_an_unknown_module_is_refused_before_the_core_is_asked() -> None:
    with pytest.raises(modules.ModuleError, match="can't be switched"):
        await modules.module_info("manager", workspace_dir="/w", config_dir="/c")


# -- the answers to what `setup` asks -------------------------------------------------------


def test_an_empty_answer_is_no_answer() -> None:
    for value in (None, "", "   "):
        assert modules.validate_public_url("rag", value) is None


def test_a_url_is_checked_and_cleaned() -> None:
    assert modules.validate_public_url("rag", " https://q.example.org:6333/ ") == (
        "https://q.example.org:6333"
    )
    assert modules.validate_public_url("localai", "http://ai.lan:8080") == "http://ai.lan:8080"


def test_a_url_that_is_wrong_says_what_is_wrong() -> None:
    with pytest.raises(modules.ModuleError, match="public URL is not valid.*http://"):
        modules.validate_public_url("rag", "qdrant.example.org")


@pytest.mark.parametrize("name", ["web-search", "mail", "manager", "nope"])
def test_a_module_without_a_url_question_takes_none(name: str) -> None:
    with pytest.raises(modules.ModuleError, match="takes no public URL"):
        modules.validate_public_url(name, "https://x.example.org")


def test_the_relay_is_not_refused_for_lacking_a_server_when_the_request_brings_one(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    _env(config, "COMPOSE_PROFILES=nginx\n")
    _core(tmp_path / "w", profiles=("smtp",))
    with pytest.raises(modules.ModuleError, match="No mail server"):
        modules.check_enable("mail", str(config), str(tmp_path / "w"))
    modules.check_enable("mail", str(config), str(tmp_path / "w"), mail_given=True)


def test_the_mail_dialog_starts_from_what_the_core_stored_and_never_has_the_password(
    tmp_path: Path,
) -> None:
    config = tmp_path / "config"
    _env(config, "COMPOSE_PROFILES=nginx\n")
    module = config / "infra" / "smtp-relay"
    module.mkdir(parents=True)
    (module / ".env").write_text(
        "SMTP_RELAY_UPSTREAM_HOST=smtp.example.org\nSMTP_RELAY_UPSTREAM_USER=mailer\n",
        encoding="utf-8",
    )
    (module / "upstream-password").write_text("s3cret-value", encoding="utf-8")
    defaults = modules.mail_defaults(str(config))
    assert defaults["host"] == "smtp.example.org"
    assert defaults["user"] == "mailer" and defaults["has_password"] is True
    assert defaults["security"] == "starttls" and defaults["port"] == 587
    assert "s3cret" not in str(defaults)
    assert set(defaults) == {
        "host",
        "port",
        "security",
        "user",
        "login",
        "has_password",
        "from_address",
        "from_name",
    }


def test_a_module_that_was_never_set_up_starts_the_mail_dialog_empty(tmp_path: Path) -> None:
    config = tmp_path / "config"
    _env(config, "COMPOSE_PROFILES=nginx\n")
    defaults = modules.mail_defaults(str(config))
    assert (defaults["host"], defaults["user"], defaults["has_password"]) == ("", "", False)
    assert defaults["security"] == "starttls"


async def test_the_open_questions_come_with_the_info(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _core(tmp_path / "w")
    document = (
        '{"module": "rag", "questions": [{"key": "QDRANT_PUBLIC_URL", "option": "--qdrant-host",'
        ' "label": "Public URL of Qdrant", "default": "https://h:6333", "value": "", "ask": true}]}'
    )
    _answer(monkeypatch, 0, document)
    info = await modules.module_info("rag", workspace_dir=str(tmp_path / "w"), config_dir="/c")
    assert info["questions"][0]["ask"] is True
    assert info["questions"][0]["default"] == "https://h:6333"
