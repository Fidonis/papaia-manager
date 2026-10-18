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


@pytest.mark.parametrize("name", ["manager", "rag", "mail", "keycloak", "../x", ""])
def test_no_other_module_can_be_switched(name: str) -> None:
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


def test_render_preview_is_a_read_only_command_and_setup_stays_the_only_writer() -> None:
    assert "render-preview" in ctl.ALLOWED_PY_COMMANDS
    assert frozenset({"setup"}) == ctl.ALLOWED_PY_WRITE_COMMANDS
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


def test_only_two_modules_are_switchable() -> None:
    assert set(modules.SWITCHABLE) == {"localai", "web-search"}
    rows = modules.state("/nonexistent", "/nonexistent")
    assert [r["name"] for r in rows if r["switchable"]] == ["localai", "web-search"]
    assert all(r["link"] or r["note"] for r in rows if not r["switchable"])


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
    assert modules.validate_enable("web-search", None) is None
    with pytest.raises(modules.ModuleError, match="Only LocalAI"):
        modules.validate_enable("web-search", "cpu")
    for name in ("rag", "mail", "manager", "nope"):
        with pytest.raises(modules.ModuleError, match="can't be switched"):
            modules.validate_enable(name, None)
