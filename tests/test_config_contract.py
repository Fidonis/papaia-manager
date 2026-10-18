"""The manager against the core's real code: what it asks of `setup` and `render-preview` is what
they do.

The Modules tab drives the core's `setup` with the options of one module, asks `module-info`
what a module holds and `module-purge` to delete it, and the Overlay tab asks its `render-preview`
command for the effective result of a draft. These tests load the core's own tables, run its real
commands in a throwaway copy of the checkout, and compare. A drift between the two is a switch that
stores nothing, a variant the core refuses, a list of overlay targets that no longer matches what
the render merges, a preview the page can't read, or a dialog that offers to delete what the core
would refuse to.

They need the core's checkout and are skipped without it. Point `PAPAIA_SRC` at a checkout of
`Fidonis/papaia` that has `render-preview`, `module-info` and `module-purge` (Fidonis/papaia#238),
for example `C:\\Projects\\fidonis\\papaia`. Repeat them when either side's option names, targets
or the documents the commands print change.
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

_SRC = os.environ.get("PAPAIA_SRC", "")


def _core_has_the_commands() -> bool:
    source = Path(_SRC) / "tools" / "lib" / "cli.py"
    if not (_SRC and source.is_file()):
        return False
    text = source.read_text(encoding="utf-8")
    return all(name in text for name in ("render-preview", "module-info", "module-purge"))


pytestmark = pytest.mark.skipif(
    not _core_has_the_commands(),
    reason="PAPAIA_SRC does not point at a checkout of the core that has render-preview "
    "and the module commands",
)

for _key, _value in {
    "OIDC_ISSUER_KC_AUTH": "https://kc.test/auth",
    "OIDC_ISSUER_KC_TOKEN": "https://kc.test/token",
    "OIDC_ISSUER_KC_CERTS": "https://kc.test/certs",
    "MANAGER_ADMIN_ROLE": "admin",
    "MANAGER_USER_ROLE": "user",
    "MANAGER_HOST": "http://localhost:8120",
    "MANAGER_OIDC_CLIENT_SECRET": "client-secret",
    "MANAGER_SESSION_SECRET": "test-session-secret-value",
    "PAPAIA_CONFIG_DIR": tempfile.mkdtemp(prefix="papaia-config-contract-config-"),
    "PAPAIA_WORKSPACE_DIR": tempfile.mkdtemp(prefix="papaia-config-contract-workspace-"),
}.items():
    os.environ.setdefault(_key, _value)

from app.core import ctl  # noqa: E402
from app.core.config import modules, overlay, preview  # noqa: E402
from app.core.ctl import (  # noqa: E402
    ModuleSetup,
    SmtpSetup,
    run_module_info,
    run_module_purge,
    run_module_setup,
    run_smtp_setup,
)
from app.core.mail.settings import load_mail_settings  # noqa: E402


@pytest.fixture(scope="module")
def core() -> Iterator[dict[str, ModuleType]]:
    """The core's own modules, imported off the checkout and forgotten afterwards."""
    tools = str(Path(_SRC) / "tools")
    sys.path.insert(0, tools)
    try:
        yield {
            "gpu_detect": importlib.import_module("lib.gpu_detect"),
            "render_core": importlib.import_module("lib.render_core"),
            "modules": importlib.import_module("lib.modules"),
            "resolve": importlib.import_module("lib.resolve"),
        }
    finally:
        sys.path.remove(tools)
        for name in [n for n in sys.modules if n == "lib" or n.startswith("lib.")]:
            del sys.modules[name]


# -- the tables the manager mirrors --------------------------------------------------------


def test_the_variants_the_page_offers_are_variants_the_core_has(
    core: dict[str, ModuleType],
) -> None:
    catalogue = set(core["gpu_detect"].VARIANTS)
    assert set(ctl.LOCALAI_VARIANTS) | set(modules.HOST_ONLY_VARIANTS) == catalogue
    assert set(ctl.LOCALAI_VARIANTS).isdisjoint(modules.HOST_ONLY_VARIANTS)
    assert set(modules.VARIANT_LABELS) == set(ctl.LOCALAI_VARIANTS)


def test_the_core_only_looks_at_the_devices_of_the_host_for_the_variant_left_out(
    core: dict[str, ModuleType], tmp_path: Path
) -> None:
    """Why `vulkan` is host-only: its fragment depends on /dev. The others do not."""
    fragment = core["gpu_detect"].compose_fragment
    nothing = tmp_path / "dev"
    nothing.mkdir()
    for variant in ctl.LOCALAI_VARIANTS:
        assert fragment(variant, nothing) == fragment(variant, tmp_path), variant
    (tmp_path / "dri").mkdir()
    assert fragment("vulkan", nothing) != fragment("vulkan", tmp_path)


def test_the_overlay_targets_are_all_of_the_cores_render_targets(
    core: dict[str, ModuleType],
) -> None:
    mine = {t.path for t in overlay.TARGETS} | set(overlay.OTHER_RENDER_TARGETS)
    assert mine == set(core["render_core"].BASE_RENDER_TARGETS)


def test_the_profiles_of_the_overlay_targets_and_the_modules_exist_in_the_compose_files(
    checkout_copy: Path, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    shutil.copytree(checkout_copy, workspace / "papaia")
    for profile in {t.profile for t in overlay.TARGETS} | {
        m.profile for m in modules.MODULES if m.switchable
    }:
        services, _ = modules.service_count(str(workspace), profile)
        assert services > 0, profile


def test_the_flags_the_manager_passes_are_options_the_core_knows() -> None:
    text = (Path(_SRC) / "tools" / "lib" / "cli.py").read_text(encoding="utf-8")
    for flag in (
        "--enable-local-ai",
        "--enable-web-search",
        "--enable-rag",
        "--enable-smtp",
        "--localai-variant",
        "--module",
        "--data",
        "--config",
    ):
        assert f'"{flag}"' in text, flag
    specs = (
        ModuleSetup("localai", True, "cpu"),
        ModuleSetup("web-search", False),
        ModuleSetup("rag", True),
    )
    for spec in specs:
        for item in ctl.module_setup_flags(spec):
            assert f'"{item.split("=", 1)[0]}"' in text, item
    for item in ctl.smtp_setup_flags(SmtpSetup(enable=True)):
        assert f'"{item.split("=", 1)[0]}"' in text, item
    assert '"render-preview"' in text
    assert '"module-info"' in text and '"module-purge"' in text
    assert preview.COMMAND == "render-preview"


def test_the_modules_the_manager_can_delete_are_the_modules_the_core_knows(
    core: dict[str, ModuleType],
) -> None:
    assert set(core["modules"].MODULES) == set(ctl.PURGE_MODULES)
    for name, definition in modules.SWITCHABLE.items():
        assert core["modules"].MODULES[name].profile == definition.profile, name


def test_the_addons_that_block_the_rag_system_are_the_ones_the_core_names(
    core: dict[str, ModuleType],
) -> None:
    assert set(modules.RAG_CONFLICTING_ADDONS) == set(core["resolve"]._RAG_CONFLICTING_ADDONS)  # noqa: SLF001


# -- a real deployment -------------------------------------------------------------------------


@pytest.fixture(scope="module")
def checkout_copy() -> Iterator[Path]:
    """A copy of the core's tools and compose files, so a `setup` run writes its `src/**/.env`
    into a directory of its own and never into the checkout under test."""
    root = Path(tempfile.mkdtemp(prefix="papaia-config-contract-core-"))
    for name in ("tools", "src", "VERSION", "ADDON_API"):
        source = Path(_SRC) / name
        if source.is_dir():
            shutil.copytree(
                source,
                root / name,
                ignore=shutil.ignore_patterns(
                    ".venv",
                    "__pycache__",
                    ".pytest_cache",
                    ".ruff_cache",
                    "tests",
                    ".env",
                    "node_modules",
                ),
            )
        elif source.is_file():
            shutil.copy2(source, root / name)
    yield root
    shutil.rmtree(root, ignore_errors=True)


class _Deployment:
    """A throwaway deployment: a workspace holding a copy of the core, and a config directory."""

    def __init__(self, template: Path, base: Path) -> None:
        self.workspace = base / "workspace"
        self.repo = self.workspace / "papaia"
        shutil.copytree(template, self.repo)
        self.config = base / "config"

    def setup(self, *flags: str) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(self.repo / "tools")
        env["PAPAIA_CONFIG_DIR"] = str(self.config)
        done = subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "lib.cli",
                "--repo-root",
                str(self.repo),
                "--config-dir",
                str(self.config),
                "setup",
                *flags,
            ],
            env=env,
            capture_output=True,
            text=True,
            cwd=str(self.repo / "tools"),
            check=False,
        )
        assert done.returncode == 0, done.stderr[-800:]

    def env(self, relative: str) -> dict[str, str]:
        values: dict[str, str] = {}
        for line in (self.config / relative).read_text(encoding="utf-8").splitlines():
            if line.strip() and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip()
        return values

    def profiles(self) -> set[str]:
        return set(self.env(".env")["COMPOSE_PROFILES"].split(","))


@pytest.fixture
def deployment(checkout_copy: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Deployment:
    # `run_module_setup` starts `python3`; this machine's interpreter is the one under test.
    original = asyncio.create_subprocess_exec

    async def spawn(*argv: str, **kwargs: Any) -> Any:
        first = sys.executable if argv and argv[0] == "python3" else argv[0]
        return await original(first, *argv[1:], **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    made = _Deployment(checkout_copy, tmp_path)
    made.setup(
        "--app-host=https://papaia.test", "--enable-web-search=false", "--enable-local-ai=false"
    )
    return made


async def _apply(made: _Deployment, spec: ModuleSetup) -> tuple[int, str, str]:
    return await run_module_setup(
        spec, workspace_dir=str(made.workspace), config_dir=str(made.config), limit=120
    )


async def test_switching_localai_on_stores_the_profile_and_the_variant(
    deployment: _Deployment,
) -> None:
    assert "localai" not in deployment.profiles()
    code, _, err = await _apply(deployment, ModuleSetup("localai", True, "intel"))
    assert code == 0, err
    assert "localai" in deployment.profiles()
    assert deployment.env(".env")["LOCALAI_IMAGE_VARIANT"] == "intel"
    # What the manager reads back is what the core stored.
    assert "localai" in modules.active(str(deployment.config))
    assert modules.stored_variant(str(deployment.config)) == "intel"
    mirrored = (deployment.config / "deployment.yaml").read_text(encoding="utf-8")
    assert "localai" in mirrored


async def test_switching_localai_off_removes_the_profile_and_keeps_the_variant(
    deployment: _Deployment,
) -> None:
    await _apply(deployment, ModuleSetup("localai", True, "nvidia-cuda-12"))
    before = deployment.profiles() - {"localai"}
    code, _, err = await _apply(deployment, ModuleSetup("localai", False))
    assert code == 0, err
    assert deployment.profiles() == before
    assert deployment.env(".env")["LOCALAI_IMAGE_VARIANT"] == "nvidia-cuda-12"


async def test_web_search_is_one_profile_and_touches_no_other(deployment: _Deployment) -> None:
    before = deployment.profiles()
    code, _, err = await _apply(deployment, ModuleSetup("web-search", True))
    assert code == 0, err
    assert deployment.profiles() == before | {"librechat-websearch"}
    code, _, err = await _apply(deployment, ModuleSetup("web-search", False))
    assert code == 0, err
    assert deployment.profiles() == before


async def test_the_revision_follows_what_setup_stored(deployment: _Deployment) -> None:
    first = modules.revision(str(deployment.config))
    await _apply(deployment, ModuleSetup("web-search", True))
    second = modules.revision(str(deployment.config))
    await _apply(deployment, ModuleSetup("web-search", False))
    assert first != second
    assert modules.revision(str(deployment.config)) == first


async def test_the_state_the_page_shows_is_the_state_in_the_deployment(
    deployment: _Deployment,
) -> None:
    await _apply(deployment, ModuleSetup("localai", True, "cpu"))
    rows = {r["name"]: r for r in modules.state(str(deployment.config), str(deployment.workspace))}
    assert rows["localai"]["enabled"] is True
    assert rows["web-search"]["enabled"] is False
    assert rows["localai"]["services"] > 0
    assert rows["web-search"]["services"] > 0 and rows["web-search"]["modules"] > 1


# -- the preview ------------------------------------------------------------------------------


async def test_the_core_previews_a_draft_and_the_page_reads_the_answer(
    deployment: _Deployment,
) -> None:
    assert preview.available(str(deployment.workspace)) is True
    draft = "search:\n  default_lang: de\n"
    result = await preview.run(
        workspace_dir=str(deployment.workspace),
        config_dir=str(deployment.config),
        target_path="services/searxng/settings.yml",
        draft=draft,
    )
    assert result.ok and result.changed
    assert result.added >= 1
    assert any(d["kind"] == "add" and "de" in d["text"] for d in result.diff)
    # The draft file is gone and nothing was written to the overlay.
    assert list((deployment.config / "manager" / "tmp").iterdir()) == []
    assert not (deployment.config / "overlay" / "services" / "searxng" / "settings.yml").exists()


async def test_a_draft_the_core_cannot_merge_comes_back_with_its_line(
    deployment: _Deployment,
) -> None:
    result = await preview.run(
        workspace_dir=str(deployment.workspace),
        config_dir=str(deployment.config),
        target_path="ai/librechat/librechat.yaml",
        draft="a: 1\nb: [\n",
    )
    assert result.ok is False
    assert result.line is not None and result.line >= 2
    assert "overlay/ai/librechat/librechat.yaml" in result.error


async def test_every_target_the_page_offers_can_be_previewed_and_what_it_shows_is_the_render(
    deployment: _Deployment,
) -> None:
    for target in overlay.TARGETS:
        result = await preview.run(
            workspace_dir=str(deployment.workspace),
            config_dir=str(deployment.config),
            target_path=target.path,
            draft="",
        )
        assert result.ok, target.path
        assert result.changed is False
        assert result.current == (deployment.config / target.path).read_text(encoding="utf-8")


def test_the_preview_is_refused_for_a_target_the_page_does_not_offer(
    deployment: _Deployment,
) -> None:
    """The command exits 2 for it, which the page turns into an error message, not a result."""
    draft = deployment.config / "draft.tmp"
    draft.write_text("a: 1\n", encoding="utf-8")
    done = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "lib.cli",
            "--repo-root",
            str(deployment.repo),
            "--config-dir",
            str(deployment.config),
            "render-preview",
            "--target=../../etc/passwd",
            f"--overlay-file={draft}",
        ],
        env={**os.environ, "PYTHONPATH": str(deployment.repo / "tools")},
        capture_output=True,
        text=True,
        cwd=str(deployment.repo / "tools"),
        check=False,
    )
    assert done.returncode == 2
    assert done.stdout == ""


# -- the RAG system ----------------------------------------------------------------------------


async def test_switching_the_rag_system_on_and_off_edits_one_profile(
    deployment: _Deployment,
) -> None:
    before = deployment.profiles()
    code, _, err = await _apply(deployment, ModuleSetup("rag", True))
    assert code == 0, err
    assert deployment.profiles() == before | {"rag"}
    code, _, err = await _apply(deployment, ModuleSetup("rag", False))
    assert code == 0, err
    assert deployment.profiles() == before


async def test_an_addon_with_the_same_services_is_refused_by_the_core_as_the_page_says(
    deployment: _Deployment,
) -> None:
    (deployment.config / "deployment.yaml").write_text(
        "addons:\n  - name: qdrant-ingest\n    active: true\n    path: /x\n", encoding="utf-8"
    )
    assert modules.conflicting_addons(str(deployment.config)) == ["qdrant-ingest"]
    code, _, err = await _apply(deployment, ModuleSetup("rag", True))
    assert code != 0
    assert "qdrant-ingest" in err
    assert "rag" not in deployment.profiles()


# -- the mail relay ----------------------------------------------------------------------------

_MAIL = SmtpSetup(
    enable=None,
    host="smtp.example.org",
    port=587,
    security="starttls",
    user="mailer",
    sender="noreply@example.org",
    sender_name="Our Mail",
    password="a-long-password-value",
)


async def _mail(made: _Deployment, spec: SmtpSetup) -> tuple[int, str, str]:
    return await run_smtp_setup(
        spec, workspace_dir=str(made.workspace), config_dir=str(made.config), limit=120
    )


async def test_the_mail_server_is_stored_with_the_relay_off_and_the_profile_stays_off(
    deployment: _Deployment,
) -> None:
    before = deployment.profiles()
    code, _, err = await _mail(deployment, _MAIL)
    assert code == 0, err
    assert deployment.profiles() == before
    assert "smtp" not in deployment.profiles()
    assert modules.mail_configured(str(deployment.config)) is True
    stored = load_mail_settings(str(deployment.config))
    assert (stored.profile_on, stored.host, stored.from_address, stored.has_password) == (
        False,
        "smtp.example.org",
        "noreply@example.org",
        True,
    )


async def test_the_relay_is_switched_on_with_the_stored_settings_and_off_keeping_them(
    deployment: _Deployment,
) -> None:
    await _mail(deployment, _MAIL)
    code, _, err = await _mail(deployment, SmtpSetup(enable=True))
    assert code == 0, err
    assert "smtp" in deployment.profiles()
    code, _, err = await _mail(deployment, SmtpSetup(enable=False))
    assert code == 0, err
    assert "smtp" not in deployment.profiles()
    stored = load_mail_settings(str(deployment.config))
    assert stored.configured and stored.has_password


async def test_the_relay_without_a_mail_server_is_refused_by_the_core_as_the_page_says(
    deployment: _Deployment,
) -> None:
    assert modules.mail_configured(str(deployment.config)) is False
    code, _, err = await _mail(deployment, SmtpSetup(enable=True))
    assert code != 0
    assert "smtp-host" in err
    assert "smtp" not in deployment.profiles()


# -- what a module holds and what deleting it does ----------------------------------------------


async def test_the_info_names_the_volumes_the_dialog_lists(deployment: _Deployment) -> None:
    info = await modules.module_info(
        "rag", workspace_dir=str(deployment.workspace), config_dir=str(deployment.config)
    )
    assert info["available"] is True
    assert info["module"] == "rag" and info["profile"] == "rag"
    assert info["settings_need_data"] is True
    assert info["volumes"], "the RAG system keeps its vectors in a volume"
    for volume in info["volumes"]:
        assert volume["name"].endswith("_" + volume["key"])
        assert volume["exists"] in (True, False, None)
    assert info["settings"]["dirs"]


async def test_every_module_answers_the_info_the_dialog_reads(deployment: _Deployment) -> None:
    for name in modules.SWITCHABLE:
        code, out, err = await run_module_info(
            name, workspace_dir=str(deployment.workspace), config_dir=str(deployment.config)
        )
        assert code == 0, (name, err)
        document = json.loads(out)
        assert {"module", "profile", "active", "volumes", "settings", "settings_need_data"} <= set(
            document
        )
        assert {"dirs", "root_keys", "files", "regenerates_secrets"} <= set(document["settings"])


async def test_a_module_that_is_still_on_is_not_purged(deployment: _Deployment) -> None:
    await _apply(deployment, ModuleSetup("rag", True))
    code, _, err = await run_module_purge(
        "rag",
        data=True,
        config=True,
        workspace_dir=str(deployment.workspace),
        config_dir=str(deployment.config),
    )
    assert code == 3
    assert "still switched on" in err


async def test_the_rag_settings_are_not_deleted_without_the_data(deployment: _Deployment) -> None:
    code, _, err = await run_module_purge(
        "rag",
        data=False,
        config=True,
        workspace_dir=str(deployment.workspace),
        config_dir=str(deployment.config),
    )
    assert code == 3
    assert "together with its data" in err


async def test_deleting_the_relays_settings_empties_what_the_page_calls_configured(
    deployment: _Deployment,
) -> None:
    await _mail(deployment, _MAIL)
    assert modules.mail_configured(str(deployment.config)) is True
    code, out, err = await run_module_purge(
        "mail",
        data=False,
        config=True,
        workspace_dir=str(deployment.workspace),
        config_dir=str(deployment.config),
    )
    assert code == 0, err
    assert "upstream-password" in out
    assert modules.mail_configured(str(deployment.config)) is False
    assert load_mail_settings(str(deployment.config)).has_password is False


# -- the answers to what `setup` asks -----------------------------------------------------------


async def test_the_public_url_the_dialog_collects_is_what_setup_stores(
    deployment: _Deployment,
) -> None:
    code, _, err = await _apply(
        deployment, ModuleSetup("rag", True, public_url="https://qdrant.example.org:9001")
    )
    assert code == 0, err
    assert deployment.env(".env")["QDRANT_PUBLIC_URL"] == "https://qdrant.example.org:9001"
    code, _, err = await _apply(
        deployment, ModuleSetup("localai", True, "cpu", public_url="https://ai.example.org")
    )
    assert code == 0, err
    assert deployment.env(".env")["LOCALAI_PUBLIC_URL"] == "https://ai.example.org"


async def test_a_stack_that_was_set_up_has_no_open_question_and_the_dialog_stays_away(
    deployment: _Deployment,
) -> None:
    info = await modules.module_info(
        "rag", workspace_dir=str(deployment.workspace), config_dir=str(deployment.config)
    )
    [question] = info["questions"]
    assert question["ask"] is False
    assert question["option"] == "--qdrant-host"
    assert question["key"] == "QDRANT_PUBLIC_URL"


async def test_deleting_the_settings_opens_the_question_the_dialog_then_asks(
    deployment: _Deployment,
) -> None:
    # Only the settings: the data of a module is never deleted by a test on a machine with Docker.
    await _apply(
        deployment, ModuleSetup("localai", True, "cpu", public_url="https://ai.example.org")
    )
    await _apply(deployment, ModuleSetup("localai", False))
    code, _, err = await run_module_purge(
        "localai",
        data=False,
        config=True,
        workspace_dir=str(deployment.workspace),
        config_dir=str(deployment.config),
    )
    assert code == 0, err
    info = await modules.module_info(
        "localai", workspace_dir=str(deployment.workspace), config_dir=str(deployment.config)
    )
    [question] = info["questions"]
    assert question["ask"] is True
    assert question["default"].startswith("https://papaia.test:")
    # The answer the dialog collects is accepted again.
    code, _, err = await _apply(
        deployment, ModuleSetup("localai", True, "cpu", public_url=question["default"])
    )
    assert code == 0, err
    info = await modules.module_info(
        "localai", workspace_dir=str(deployment.workspace), config_dir=str(deployment.config)
    )
    assert info["questions"][0]["ask"] is False


async def test_the_relay_is_switched_on_with_a_full_mail_server_in_one_setup(
    deployment: _Deployment,
) -> None:
    assert modules.mail_configured(str(deployment.config)) is False
    spec = SmtpSetup(**{**_MAIL.__dict__, "enable": True})
    code, _, err = await _mail(deployment, spec)
    assert code == 0, err
    assert "smtp" in deployment.profiles()
    stored = load_mail_settings(str(deployment.config))
    assert (stored.profile_on, stored.host, stored.has_password) == (True, "smtp.example.org", True)
