"""The shared page modules (src/app/static/js/core/) under `node --test` (tests/js/).

ES modules without a bundler: every file under js/ is served as it is in the repository, so
what the tests import is what the browser loads.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
JS = REPO / "src" / "app" / "static" / "js"
NODE = shutil.which("node")


@pytest.mark.skipif(NODE is None, reason="node is not available")
def test_the_core_modules_pass_their_unit_tests() -> None:
    assert NODE is not None
    done = subprocess.run(
        [NODE, "--test", "tests/js/*.test.mjs"],
        cwd=REPO,
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert done.returncode == 0, done.stdout + done.stderr


CORE_MODULES = (
    "actions.js",
    "announce.js",
    "csrf.js",
    "errors.js",
    "format.js",
    "islands.js",
    "jobs.js",
    "legacy.js",
    "main.js",
    "menus.js",
    "page.js",
    "poll.js",
    "runtime.js",
    "session.js",
    "toasts.js",
)
# Every name the shared page script of base.html and partials/_rag_js.html gave the pages as
# a global before it moved into the modules (T-UI-04).
FORMER_GLOBALS = frozenset(
    {
        "I18N", "_addonPost", "_authGuard", "_escapeHtml", "_pollJob", "_ragDetail",
        "_ragError", "_ragEscape", "_ragRequest", "_reauth", "_stackBody", "_stackPost",
        "addonAction", "addonMaintenanceAction", "catalogAction", "catalogDelete",
        "catalogUpdate", "closeFloatMenu", "csrfToken", "fmt", "fmtHtml", "fmtPlural",
        "galleryAction", "placeFloatMenu", "refreshServices", "refreshStackRunner",
        "serviceAddonAction", "showToast", "stackAction", "stackGroupAction",
    }
)  # fmt: skip
TEMPLATES = REPO / "src" / "app" / "templates"


def test_the_module_layout() -> None:
    assert sorted(p.name for p in (JS / "core").glob("*.js")) == list(CORE_MODULES)


def _legacy_names() -> list[str]:
    source = (JS / "core" / "legacy.js").read_text(encoding="utf-8")
    block = source[source.index("LEGACY_NAMES = Object.freeze([") :]
    return re.findall(r'"([^"]+)",', block[: block.index("]);")])


def _used(name: str) -> list[str]:
    pattern = re.compile(r"(?<![\w$.])" + re.escape(name) + r"(?![\w$])")
    # Page modules (js/pages/) import what they use; tests/test_settings_scripts.py checks it.
    users = [
        p
        for p in [*TEMPLATES.rglob("*.html"), *JS.rglob("*.js")]
        if p.parent != JS / "core" and not p.is_relative_to(JS / "pages")
    ]
    return [str(p.relative_to(REPO)) for p in users if pattern.search(p.read_text("utf-8"))]


def test_the_legacy_names_are_exactly_the_former_globals_the_pages_still_use() -> None:
    listed = _legacy_names()
    assert listed == sorted(set(listed))
    assert set(listed) <= FORMER_GLOBALS
    used = {name for name in FORMER_GLOBALS if _used(name)}
    assert set(listed) == used, (
        f"listed but no page uses it: {sorted(set(listed) - used)}; "
        f"used but not listed: {sorted(used - set(listed))}"
    )


def test_main_installs_every_legacy_name() -> None:
    source = (JS / "core" / "main.js").read_text(encoding="utf-8")
    block = source[source.index("installLegacy(window, {") :]
    block = block[: block.index("\n});")]
    assigned = re.findall(r"^  (\w+)(?::|,)", block, re.MULTILINE)
    assert sorted(assigned) == _legacy_names()


def test_modules_import_only_relative_paths_of_the_tree() -> None:
    """No bundler and no package names: every import resolves to a file under js/."""
    for path in JS.rglob("*.js"):
        source = path.read_text(encoding="utf-8")
        for target in re.findall(r'^\s*import\s[^"\']*["\']([^"\']+)["\']', source, re.MULTILINE):
            assert target.startswith("./") or target.startswith("../"), (path, target)
            assert (path.parent / target).resolve().is_relative_to(JS), (path, target)
            assert (path.parent / target).resolve().is_file(), (path, target)


def test_page_modules_reach_no_legacy_global() -> None:
    """A page module imports from core/ and never reads a name core/legacy.js puts on window."""
    pages = sorted((JS / "pages").glob("*.js"))
    assert pages
    for path in pages:
        source = path.read_text(encoding="utf-8")
        for name in _legacy_names():
            assert not re.search(r"\bwindow\." + re.escape(name) + r"\b", source), (path, name)
        assert "../core/main.js" not in source, f"{path.name} must not import main.js again"
