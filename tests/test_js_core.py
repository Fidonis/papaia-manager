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


def test_the_module_layout() -> None:
    assert (JS / "core" / "main.js").is_file()
    assert (JS / "core" / "islands.js").is_file()
    assert (JS / "core" / "poll.js").is_file()


def test_modules_import_only_relative_paths_of_the_tree() -> None:
    """No bundler and no package names: every import resolves to a file under js/."""
    for path in JS.rglob("*.js"):
        source = path.read_text(encoding="utf-8")
        for target in re.findall(r'^\s*import\s[^"\']*["\']([^"\']+)["\']', source, re.MULTILINE):
            assert target.startswith("./") or target.startswith("../"), (path, target)
            assert (path.parent / target).resolve().is_relative_to(JS), (path, target)
            assert (path.parent / target).resolve().is_file(), (path, target)
