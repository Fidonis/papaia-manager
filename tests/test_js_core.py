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
    "main.js",
    "marked.js",
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


_ASSIGNED_GLOBAL = re.compile(r"\b(?:window|globalThis|self)\.(\w+)\s*=(?!=)")


def test_the_transition_globals_are_gone() -> None:
    """No former global of the page script is put on `window` any more (T-UI-03): core/legacy.js
    is removed, and no module of the tree assigns one of the names."""
    assert not (JS / "core" / "legacy.js").exists()
    for path in JS.rglob("*.js"):
        source = path.read_text(encoding="utf-8")
        assert "installLegacy" not in source, path
        assert "legacy.js" not in source, path
        assigned = set(_ASSIGNED_GLOBAL.findall(source))
        assert not assigned & FORMER_GLOBALS, (path, sorted(assigned & FORMER_GLOBALS))


def test_the_templates_reach_no_former_global_outside_a_component() -> None:
    """A template expression that names a former global (fmt, fmtPlural) runs in a component
    that offers it as a member; base.html starts no component that would."""
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    for name in FORMER_GLOBALS:
        assert not re.search(r"(?<![\w$.])" + re.escape(name) + r"\(", base), name


def test_modules_import_only_relative_paths_of_the_tree() -> None:
    """No bundler and no package names: every import resolves to a file under js/."""
    for path in JS.rglob("*.js"):
        source = path.read_text(encoding="utf-8")
        for target in re.findall(r'^\s*import\s[^"\']*["\']([^"\']+)["\']', source, re.MULTILINE):
            assert target.startswith("./") or target.startswith("../"), (path, target)
            assert (path.parent / target).resolve().is_relative_to(JS), (path, target)
            assert (path.parent / target).resolve().is_file(), (path, target)


def test_page_modules_reach_no_former_global() -> None:
    """A page module imports from core/ and never reads a former global from window."""
    pages = sorted((JS / "pages").glob("*.js"))
    assert pages
    for path in pages:
        source = path.read_text(encoding="utf-8")
        for name in sorted(FORMER_GLOBALS):
            assert not re.search(r"\bwindow\." + re.escape(name) + r"\b", source), (path, name)
        assert "../core/main.js" not in source, f"{path.name} must not import main.js again"
