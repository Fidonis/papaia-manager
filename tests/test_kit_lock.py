"""The vendored papaia-kit wheel is the one kit.lock, pyproject.toml and uv.lock name."""
from __future__ import annotations

import hashlib
import re
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
