"""The browser libraries of the image: pinned by version and checked against a SHA-256.

docker/Dockerfile downloads the CSP build of Alpine, htmx and Sortable and
hands each file to docker/verify-sha256.sh with the digest written in the Dockerfile; a
file that does not match fails the image build. The design and CSP checks install the same
versions from scripts/design/package.json, so the browser runs see the bytes the image ships.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO / "docker" / "Dockerfile"
VERIFY = REPO / "docker" / "verify-sha256.sh"
BASH = shutil.which("bash")

# Build argument prefix -> (npm package, file in the package, file in the image).
ASSETS = {
    "HTMX": ("htmx.org", "dist/htmx.min.js", "htmx.min.js"),
    "ALPINE_CSP": ("@alpinejs/csp", "dist/cdn.min.js", "alpine-csp.min.js"),
    "SORTABLE": ("sortablejs", "Sortable.min.js", "sortable.min.js"),
}


def _args() -> dict[str, str]:
    text = DOCKERFILE.read_text(encoding="utf-8")
    return dict(re.findall(r"^ARG ([A-Z_0-9]+)=(\S+)$", text, flags=re.MULTILINE))


def _verify(*argv: str) -> subprocess.CompletedProcess[str]:
    assert BASH is not None
    return subprocess.run([BASH, str(VERIFY), *argv], capture_output=True, text=True, check=False)


# -- the check -----------------------------------------------------------------------------


needs_bash = pytest.mark.skipif(BASH is None, reason="bash is not available")


@needs_bash
def test_a_matching_file_passes(tmp_path: Path) -> None:
    asset = tmp_path / "alpine-csp.min.js"
    asset.write_bytes(b"/* build */\n")
    done = _verify(str(asset), hashlib.sha256(b"/* build */\n").hexdigest())
    assert done.returncode == 0, done.stderr


@needs_bash
def test_a_changed_checksum_fails(tmp_path: Path) -> None:
    asset = tmp_path / "alpine-csp.min.js"
    asset.write_bytes(b"/* build */\n")
    digest = hashlib.sha256(b"/* build */\n").hexdigest()
    changed = ("0" if digest[0] != "0" else "1") + digest[1:]
    done = _verify(str(asset), changed)
    assert done.returncode == 1
    assert "alpine-csp.min.js" in done.stderr
    assert "does not match" in done.stderr


@needs_bash
def test_a_changed_file_fails(tmp_path: Path) -> None:
    asset = tmp_path / "htmx.min.js"
    asset.write_bytes(b"tampered")
    done = _verify(str(asset), hashlib.sha256(b"original").hexdigest())
    assert done.returncode == 1


@needs_bash
@pytest.mark.parametrize(
    "digest", ["", "abc", "g" * 64, "A" * 64, "0" * 63, "0" * 65, "0" * 64 + "\n"]
)
def test_a_malformed_checksum_is_refused(tmp_path: Path, digest: str) -> None:
    asset = tmp_path / "x.js"
    asset.write_bytes(b"x")
    done = _verify(str(asset), digest)
    assert done.returncode == 2


@needs_bash
def test_a_missing_file_fails(tmp_path: Path) -> None:
    done = _verify(str(tmp_path / "missing.js"), "0" * 64)
    assert done.returncode != 0


@needs_bash
def test_the_check_takes_exactly_two_arguments(tmp_path: Path) -> None:
    assert _verify().returncode == 2
    assert _verify(str(tmp_path), "0" * 64, "extra").returncode == 2


# -- the Dockerfile ------------------------------------------------------------------------


def test_every_library_is_pinned_and_checked() -> None:
    args = _args()
    text = DOCKERFILE.read_text(encoding="utf-8")
    for prefix, (package, path, target) in ASSETS.items():
        assert re.fullmatch(r"\d+\.\d+\.\d+", args[f"{prefix}_VERSION"]), prefix
        assert re.fullmatch(r"[0-9a-f]{64}", args[f"{prefix}_SHA256"]), prefix
        url = f"https://unpkg.com/{package}@${{{prefix}_VERSION}}/{path}"
        assert f'"{url}"' in text, url
        assert f'verify-sha256 /static/{target} "${{{prefix}_SHA256}}"' in text, target


def test_the_image_ships_the_csp_build_of_alpine_only() -> None:
    """The standard build evaluates expressions with `eval`, which the enforced policy refuses
    (T-UI-03); it is neither downloaded nor installed for the browser checks."""
    args = _args()
    assert "ALPINE_VERSION" not in args
    assert "ALPINE_SHA256" not in args
    text = DOCKERFILE.read_text(encoding="utf-8")
    assert "unpkg.com/alpinejs@" not in text
    assert "/static/alpine.min.js" not in text
    package = json.loads((REPO / "scripts" / "design" / "package.json").read_text("utf-8"))
    lock = json.loads((REPO / "scripts" / "design" / "package-lock.json").read_text("utf-8"))
    assert "alpinejs" not in package["dependencies"]
    assert "alpinejs" not in lock["packages"][""]["dependencies"]
    assert "node_modules/alpinejs" not in lock["packages"]


def test_the_browser_checks_install_the_versions_of_the_image() -> None:
    package = json.loads((REPO / "scripts" / "design" / "package.json").read_text("utf-8"))
    lock = json.loads((REPO / "scripts" / "design" / "package-lock.json").read_text("utf-8"))
    args = _args()
    for prefix, (name, _path, _target) in ASSETS.items():
        version = args[f"{prefix}_VERSION"]
        assert package["dependencies"][name] == version, name
        assert lock["packages"][f"node_modules/{name}"]["version"] == version, name


INSTALLED = REPO / ".pk" / "node_modules"


@pytest.mark.skipif(
    not INSTALLED.is_dir(), reason="browser tools not installed (scripts/design-tools.sh)"
)
def test_the_digests_are_those_of_the_installed_packages() -> None:
    args = _args()
    for prefix, (name, path, _target) in ASSETS.items():
        data = (INSTALLED / name / path).read_bytes()
        assert hashlib.sha256(data).hexdigest() == args[f"{prefix}_SHA256"], name
