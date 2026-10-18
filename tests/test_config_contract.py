"""The manager against the core's real code: what it asks of `setup` and `render-preview` is what
they do.

The Modules tab drives the core's `setup` with the options of one module, and the Overlay tab asks
its `render-preview` command for the effective result of a draft. These tests load the core's own
tables, run its real `setup` and its real `render-preview` in a throwaway copy of the checkout, and
compare. A drift between the two is a switch that stores nothing, a variant the core refuses, a
list of overlay targets that no longer matches what the render merges, or a preview the page can't
read.

They need the core's checkout and are skipped without it. Point `PAPAIA_SRC` at a checkout of
`Fidonis/papaia` that has `render-preview` (Fidonis/papaia#238), for example
`C:\\Projects\\fidonis\\papaia`. Repeat them when either side's option names, targets or the
document `render-preview` prints change.
"""

from __future__ import annotations

import asyncio
import importlib
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


def _core_has_the_preview() -> bool:
    source = Path(_SRC) / "tools" / "lib" / "cli.py"
    return (
        bool(_SRC) and source.is_file() and "render-preview" in source.read_text(encoding="utf-8")
    )


pytestmark = pytest.mark.skipif(
    not _core_has_the_preview(),
    reason="PAPAIA_SRC does not point at a checkout of the core that has render-preview",
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
from app.core.ctl import ModuleSetup, run_module_setup  # noqa: E402


@pytest.fixture(scope="module")
def core() -> Iterator[dict[str, ModuleType]]:
    """The core's own modules, imported off the checkout and forgotten afterwards."""
    tools = str(Path(_SRC) / "tools")
    sys.path.insert(0, tools)
    try:
        yield {
            "gpu_detect": importlib.import_module("lib.gpu_detect"),
            "render_core": importlib.import_module("lib.render_core"),
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
    for flag in ("--enable-local-ai", "--enable-web-search", "--localai-variant"):
        assert f'"{flag}"' in text, flag
    for spec in (ModuleSetup("localai", True, "cpu"), ModuleSetup("web-search", False)):
        for item in ctl.module_setup_flags(spec):
            assert f'"{item.split("=", 1)[0]}"' in text, item
    assert '"render-preview"' in text
    assert preview.COMMAND == "render-preview"


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
