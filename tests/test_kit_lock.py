"""The vendored papaia-kit wheel is the one kit.lock, pyproject.toml and uv.lock name.

The manager vendors kit 0.1.0, the kit's first public release (PLT-37). Its release record
(the kit's reproducible release build of the commit below, `make release TAG=v0.1.0` with
SOURCE_DATE_EPOCH set to the commit time) gives the commit, the epoch and the digests this
file pins; every vendored file must hash to them.
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import tarfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path
from typing import Any

SRC = Path(__file__).resolve().parent.parent / "src"
ROOT = SRC.parent

# The kit 0.1.0 release record.
RELEASE = "0.1.0"
RELEASE_COMMIT = "0ce412759e840ce6f8dfce0c6ad14e7180ac8a55"
RELEASE_EPOCH = 1795451882
RELEASE_WHEEL = "papaia_kit-0.1.0-py3-none-any.whl"
RELEASE_WHEEL_SHA256 = "6ce3870703605ec0a710256abe74d94c29e2f8bbac681294477471ef36419299"
RELEASE_NPM = "papaia-kit-0.1.0.tgz"
RELEASE_NPM_SHA256 = "4925ab93bfe883623cd37ac314dff00dc655d2944dbc10ebc70d540f287e9c5d"


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


def test_kit_license_ships_next_to_the_wheel_and_the_kit_has_no_notice() -> None:
    """MIT redistribution: the wheel's LICENSE is visible in the tree; the kit ships no NOTICE."""
    lock = _toml("kit.lock")
    wheel = SRC / lock["files"][0]["path"]
    info = f"papaia_kit-{lock['version']}.dist-info"
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        assert (wheel.parent / "LICENSE").read_bytes() == archive.read(f"{info}/licenses/LICENSE")
        metadata = Parser().parsestr(archive.read(f"{info}/METADATA").decode("utf-8"))
    assert not any(name.rsplit("/", 1)[-1] == "NOTICE" for name in names)
    assert not (wheel.parent / "NOTICE").exists()
    assert metadata["License-Expression"] == "MIT"
    assert (wheel.parent / "LICENSE").read_text(encoding="utf-8").startswith("MIT License\n")


def test_image_installs_the_vendored_wheel_and_carries_the_kit_label() -> None:
    """`uv sync --frozen` in the builder needs the wheel next to the lock file."""
    lock = _toml("kit.lock")
    dockerfile = (ROOT / "docker" / "Dockerfile").read_text(encoding="utf-8")
    sync = dockerfile.index("RUN uv sync --frozen --no-dev")
    assert 0 <= dockerfile.index("COPY src/vendor ./vendor") < sync
    assert f'LABEL de.fidonis.platform-kit="{lock["version"]}"' in dockerfile


def test_kit_lock_is_the_kit_0_1_0_release_record() -> None:
    """Version, commit, epoch and digest are those of the kit 0.1.0 release record."""
    lock = _toml("kit.lock")
    assert lock["version"] == RELEASE
    assert lock["commit"] == RELEASE_COMMIT
    assert lock["source_date_epoch"] == RELEASE_EPOCH
    assert lock["files"] == [
        {
            "name": RELEASE_WHEEL,
            "path": f"vendor/papaia-kit/{RELEASE}/{RELEASE_WHEEL}",
            "sha256": RELEASE_WHEEL_SHA256,
        }
    ]
    js = tomllib.loads(
        (SRC / "vendor" / "papaia-kit" / "js" / "kit-js.lock").read_text(encoding="utf-8")
    )
    assert js["version"] == RELEASE
    assert js["commit"] == RELEASE_COMMIT
    assert js["files"] == [
        {"path": f"vendor/papaia-kit/js/{RELEASE_NPM}", "sha256": RELEASE_NPM_SHA256}
    ]
    wheel = SRC / "vendor" / "papaia-kit" / RELEASE / RELEASE_WHEEL
    tarball = SRC / "vendor" / "papaia-kit" / "js" / RELEASE_NPM
    assert hashlib.sha256(wheel.read_bytes()).hexdigest() == RELEASE_WHEEL_SHA256
    assert hashlib.sha256(tarball.read_bytes()).hexdigest() == RELEASE_NPM_SHA256


def test_the_vendored_kit_is_0_1_0_and_the_only_one() -> None:
    """PLT-37, PLT-83: one kit version in the repository, equal to the installed one."""
    from importlib.metadata import distribution

    import papaia_kit

    lock = _toml("kit.lock")
    assert lock["version"] == RELEASE
    assert papaia_kit.__version__ == RELEASE
    assert distribution("papaia-kit").version == RELEASE
    assert distribution("papaia-kit").metadata["License-Expression"] == "MIT"
    kit_dir = SRC / "vendor" / "papaia-kit"
    assert sorted(p.name for p in kit_dir.iterdir()) == [RELEASE, "js"]
    assert sorted(p.name for p in (kit_dir / RELEASE).iterdir()) == ["LICENSE", RELEASE_WHEEL]
    assert sorted(p.name for p in (kit_dir / "js").iterdir()) == ["kit-js.lock", RELEASE_NPM]


def test_third_party_licenses_name_the_kit_under_mit() -> None:
    """The generated licence list names the vendored kit with its version and MIT."""
    rows = [
        [cell.strip() for cell in line.strip("|").split("|")]
        for line in (ROOT / "THIRD_PARTY_LICENSES.md").read_text(encoding="utf-8").splitlines()
        if line.startswith("| papaia-kit ")
    ]
    assert [row[:3] for row in rows] == [["papaia-kit", RELEASE, "MIT"]]


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


# A kit version that is not the release: the numbers of the kit's internal milestones and
# their series names. Built from parts so this file names none of them itself.
_DOT = re.escape(".")
_INTERNAL = re.compile(
    rf"(?<![\w.])0{_DOT}(?:1{_DOT}(?:[1-9]\d*|x)|2{_DOT}(?:\d+|x))(?![\w.]*\d)"
)
# A line that speaks of the kit: its name, its package, its image label or its URL path.
_KIT_LINE = re.compile(r"(?i)\bkit\b|papaia[-_]kit|platform-kit|/_kit/|\bpk-")


def _kit_version_mentions(text: str) -> list[int]:
    return [
        number
        for number, line in enumerate(text.splitlines(), start=1)
        if _KIT_LINE.search(line) and _INTERNAL.search(line)
    ]


def test_the_pattern_finds_internal_kit_versions_only() -> None:
    internal = "0" + "." + "2" + "." + "2"
    series = "0" + "." + "1" + "." + "x"
    assert _kit_version_mentions(f"papaia-kit {internal} is vendored") == [1]
    assert _kit_version_mentions(f"/_kit/{internal}/css/papaia-kit.css") == [1]
    assert _kit_version_mentions(f"the kit {series} line") == [1]
    assert _kit_version_mentions(f'LABEL de.fidonis.platform-kit="{internal}"') == [1]
    assert _kit_version_mentions(f"papaia-kit {RELEASE} is vendored") == []
    assert _kit_version_mentions("kit on host 10.0.2.10 and core 1.10.0") == []
    assert _kit_version_mentions(f"studio {internal} without the word") == []


def test_no_file_names_an_internal_kit_version() -> None:
    """Only the release 0.1.0 is named; CHANGELOG.md keeps its history lines."""
    files = subprocess.run(
        ["git", "ls-files", "-z"], cwd=ROOT, check=True, capture_output=True
    ).stdout.decode("utf-8").split("\0")
    found: dict[str, list[int]] = {}
    for name in filter(None, files):
        path = ROOT / name
        if name == "CHANGELOG.md" or not path.is_file():
            continue
        if re.search(r"papaia[-_]kit[-/]0\.", name) and RELEASE not in name:
            found[name] = [0]
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if lines := _kit_version_mentions(text):
            found[name] = lines
    assert not found, f"internal kit versions named in: {found}"
