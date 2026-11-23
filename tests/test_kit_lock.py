"""The vendored papaia-kit wheel is the one kit.lock, pyproject.toml and uv.lock name."""
from __future__ import annotations

import hashlib
import json
import re
import tarfile
import tomllib
import zipfile
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parent.parent / "src"
ROOT = SRC.parent


def _toml(name: str) -> dict[str, Any]:
    return tomllib.loads((SRC / name).read_text(encoding="utf-8"))


def test_vendored_wheel_matches_kit_lock() -> None:
    lock = _toml("kit.lock")
    assert re.fullmatch(r"[0-9a-f]{40}", lock["commit"])
    [entry] = lock["files"]
    assert entry["path"] == f"vendor/papaia-kit/{lock['version']}/{entry['name']}"
    wheel = SRC / entry["path"]
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == entry["sha256"]


def test_pyproject_and_uv_lock_pin_the_same_kit() -> None:
    lock = _toml("kit.lock")
    [entry] = lock["files"]
    project = _toml("pyproject.toml")
    assert f"papaia-kit=={lock['version']}" in project["project"]["dependencies"]
    assert project["tool"]["uv"]["sources"]["papaia-kit"] == {"path": entry["path"]}
    [kit] = [p for p in _toml("uv.lock")["package"] if p["name"] == "papaia-kit"]
    assert kit["version"] == lock["version"]
    assert kit["source"] == {"path": entry["path"]}
    assert [w["hash"] for w in kit["wheels"]] == [f"sha256:{entry['sha256']}"]


def test_kit_license_and_notice_ship_next_to_the_wheel() -> None:
    """Apache-2.0 redistribution: LICENSE and NOTICE of the wheel are visible in the tree."""
    lock = _toml("kit.lock")
    wheel = SRC / lock["files"][0]["path"]
    info = f"papaia_kit-{lock['version']}.dist-info/licenses"
    with zipfile.ZipFile(wheel) as archive:
        for name in ("LICENSE", "NOTICE"):
            assert (wheel.parent / name).read_bytes() == archive.read(f"{info}/{name}")


def test_image_installs_the_vendored_wheel_and_carries_the_kit_label() -> None:
    """`uv sync --frozen` in the builder needs the wheel next to the lock file."""
    lock = _toml("kit.lock")
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    sync = dockerfile.index("RUN uv sync --frozen --no-dev")
    assert 0 <= dockerfile.index("COPY src/vendor ./vendor") < sync
    assert f'LABEL de.fidonis.platform-kit="{lock["version"]}"' in dockerfile


def test_the_vendored_kit_is_0_2_2_and_the_only_one() -> None:
    """PLT-83: one kit version in the repository, equal to the installed one."""
    import papaia_kit

    lock = _toml("kit.lock")
    assert lock["version"] == "0.2.2"
    assert papaia_kit.__version__ == lock["version"]
    folders = sorted(p.name for p in (SRC / "vendor" / "papaia-kit").iterdir() if p.is_dir())
    assert folders == ["0.2.2", "js"]
    assert sorted(p.name for p in (SRC / "vendor" / "papaia-kit" / "0.2.2").iterdir()) == [
        "LICENSE",
        "NOTICE",
        "papaia_kit-0.2.2-py3-none-any.whl",
    ]


def test_the_kit_js_package_matches_its_lock() -> None:
    """The browser tool of the design check: npm pack of the kit's js/ at the locked commit."""
    kit = _toml("kit.lock")
    js = tomllib.loads(
        (SRC / "vendor" / "papaia-kit" / "js" / "kit-js.lock").read_text(encoding="utf-8")
    )
    assert js["version"] == kit["version"]
    assert js["commit"] == kit["commit"]
    [entry] = js["files"]
    assert entry["path"] == f"vendor/papaia-kit/js/papaia-kit-{kit['version']}.tgz"
    tarball = SRC / entry["path"]
    assert hashlib.sha256(tarball.read_bytes()).hexdigest() == entry["sha256"]
    with tarfile.open(tarball, "r:gz") as archive:
        names = set(archive.getnames())
        assert {"package/package.json", "package/bin/pk-conform.ts", "package/LICENSE"} <= names
        package = archive.extractfile("package/package.json")
        licence = archive.extractfile("package/LICENSE")
        assert package is not None and licence is not None
        assert json.loads(package.read())["version"] == kit["version"]
        vendored = (SRC / "vendor" / "papaia-kit" / kit["version"] / "LICENSE").read_bytes()
        assert licence.read() == vendored


def test_the_image_does_not_carry_the_kit_js_package() -> None:
    """The npm package is a tool of the design check, not part of the server image."""
    ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "src/vendor/papaia-kit/js" in ignored
