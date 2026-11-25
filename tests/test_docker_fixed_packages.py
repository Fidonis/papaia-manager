"""Debian packages of the runtime image that carry a security fix the base image lacks.

docker/Dockerfile pins each such package as `ARG FIXED_<PACKAGE>=<version>` in the runtime
stage and upgrades exactly these packages, and nothing else, to the pinned version in its
`apt-get` step. docker/check-fixed-packages.sh reads the same pins and asks `dpkg` inside a
built image, per platform, whether each package is installed in at least that version; the
"Docker" workflow runs it on both platforms before the image is signed.

The check itself is tested here with stand-ins for `docker`, `dpkg-query` and `dpkg`; the run
against a real image is the workflow step.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
DOCKERFILE = REPO / "docker" / "Dockerfile"
CHECK = REPO / "docker" / "check-fixed-packages.sh"
SH = shutil.which("sh")

# Package -> (fixed version, CVE it fixes). Debian 13 security updates.
FIXED = {
    "libzstd1": ("1.5.7+dfsg-1+deb13u1", "CVE-2026-124851"),
    "zlib1g": ("1:1.3.dfsg+really1.3.1-1+deb13u1", "CVE-2026-85091"),
}


def _runtime_stage() -> str:
    text = DOCKERFILE.read_text(encoding="utf-8")
    start = text.index("FROM python:3.12-slim AS runtime")
    return text[start:]


def _pins() -> dict[str, str]:
    found = re.findall(r"^ARG FIXED_([A-Z0-9_]+)=(\S+)$", _runtime_stage(), flags=re.MULTILINE)
    return {name.lower().replace("_", "-"): version for name, version in found}


def _run_steps() -> list[str]:
    """Every RUN instruction of the runtime stage, continuation lines joined."""
    joined = re.sub(r"\\\n", " ", _runtime_stage())
    return [line for line in joined.splitlines() if line.startswith("RUN ")]


# -- the Dockerfile ------------------------------------------------------------------------


def test_the_runtime_stage_pins_the_fixed_versions() -> None:
    assert _pins() == {name: version for name, (version, _cve) in FIXED.items()}


def test_each_pin_names_the_cve_it_fixes() -> None:
    lines = _runtime_stage().splitlines()
    for name, (_version, cve) in FIXED.items():
        arg = f"ARG FIXED_{name.upper().replace('-', '_')}="
        at = next(i for i, line in enumerate(lines) if line.startswith(arg))
        comment = lines[at - 1]
        assert comment.startswith(f"# {name}: {cve} "), (name, comment)


def test_only_the_pinned_packages_are_upgraded() -> None:
    upgrades = [step for step in _run_steps() if "--only-upgrade" in step]
    assert len(upgrades) == 1
    step = upgrades[0]
    assert step.index("apt-get update") < step.index("--only-upgrade")
    command = step.split("--only-upgrade", 1)[1].split("&&", 1)[0].split()
    assert command == ['"libzstd1=${FIXED_LIBZSTD1}"', '"zlib1g=${FIXED_ZLIB1G}"']
    # The upgrade sits in the step that installs the runtime packages, so one apt-get
    # update serves both and the lists are removed at its end.
    assert "docker-ce-cli" in step
    assert step.rstrip().endswith("rm -rf /var/lib/apt/lists/*")


def test_no_other_package_is_upgraded_and_signatures_stay_checked() -> None:
    text = DOCKERFILE.read_text(encoding="utf-8")
    for refused in (
        "apt-get upgrade",
        "apt-get dist-upgrade",
        "apt-get full-upgrade",
        "--allow-unauthenticated",
        "--allow-downgrades",
        "--allow-remove-essential",
        "--allow-change-held-packages",
        "AllowInsecureRepositories",
        "AllowDowngradeToInsecureRepositories",
        "trusted=yes",
        "--force-yes",
    ):
        assert refused not in text, refused


def test_user_port_and_entrypoint_of_the_image_are_unchanged() -> None:
    stage = _runtime_stage()
    assert re.findall(r"^USER (\S+)$", stage, flags=re.MULTILINE) == ["app"]
    assert re.findall(r"^EXPOSE (.+)$", stage, flags=re.MULTILINE) == ["8000"]
    assert 'ENTRYPOINT ["/app/bin/entrypoint"]' in stage


# -- the image check -----------------------------------------------------------------------

needs_sh = pytest.mark.skipif(SH is None, reason="sh is not available")

STUB_DOCKER = """#!/bin/sh
printf '%s\\n' "$@" > "$STUB_DIR/docker-args"
while [ "$#" -gt 0 ] && [ "$1" != "-c" ]; do shift; done
shift
script=$1
shift
exec sh -c "$script" "$@"
"""

# `dpkg-query -W -f=<format> <package>`: the package's state and version from a file.
STUB_DPKG_QUERY = """#!/bin/sh
for last; do :; done
[ -f "$STUB_DIR/pkg-$last" ] || { echo "dpkg-query: no packages found matching $last" >&2; exit 1; }
cat "$STUB_DIR/pkg-$last"
"""

# `dpkg --compare-versions <a> ge <b>`: true when equal or when <a> is listed as newer.
STUB_DPKG = """#!/bin/sh
[ "$1" = "--compare-versions" ] && [ "$3" = "ge" ] || exit 2
[ "$2" = "$4" ] && exit 0
grep -qxF -- "$2" "$STUB_DIR/newer" 2>/dev/null
"""


def _stubs(tmp_path: Path) -> Path:
    stubs = tmp_path / "bin"
    stubs.mkdir()
    for name, body in (
        ("docker", STUB_DOCKER),
        ("dpkg-query", STUB_DPKG_QUERY),
        ("dpkg", STUB_DPKG),
    ):
        path = stubs / name
        path.write_text(body, encoding="utf-8")
        path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return stubs


def _installed(tmp_path: Path, **packages: str) -> None:
    for name, line in packages.items():
        (tmp_path / f"pkg-{name}").write_text(line, encoding="utf-8")


def _check(
    tmp_path: Path, *argv: str, dockerfile: Path = DOCKERFILE, name: str | None = None
) -> subprocess.CompletedProcess[str]:
    assert SH is not None
    env = {
        **os.environ,
        "PATH": f"{_stubs(tmp_path)}:{os.environ['PATH']}",
        "STUB_DIR": str(tmp_path),
    }
    env.pop("CHECK_CONTAINER_NAME", None)
    if name is not None:
        env["CHECK_CONTAINER_NAME"] = name
    image = "ghcr.io/fidonis/papaia-manager:1.8.0"
    args = argv if argv else (str(dockerfile), image, "linux/arm64")
    return subprocess.run(
        [SH, str(CHECK), *args], capture_output=True, text=True, env=env, check=False
    )


def _docker_args(tmp_path: Path) -> list[str]:
    return (tmp_path / "docker-args").read_text(encoding="utf-8").splitlines()


@needs_sh
def test_an_image_with_the_fixed_versions_passes(tmp_path: Path) -> None:
    _installed(
        tmp_path,
        libzstd1="installed 1.5.7+dfsg-1+deb13u1",
        zlib1g="installed 1:1.3.dfsg+really1.3.1-1+deb13u1",
    )
    done = _check(tmp_path)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "libzstd1 1.5.7+dfsg-1+deb13u1" in done.stdout
    assert "zlib1g 1:1.3.dfsg+really1.3.1-1+deb13u1" in done.stdout


@needs_sh
def test_a_newer_version_passes(tmp_path: Path) -> None:
    _installed(
        tmp_path,
        libzstd1="installed 1.5.7+dfsg-1+deb13u2",
        zlib1g="installed 1:1.3.dfsg+really1.3.1-1+deb13u1",
    )
    (tmp_path / "newer").write_text("1.5.7+dfsg-1+deb13u2\n", encoding="utf-8")
    assert _check(tmp_path).returncode == 0


@needs_sh
def test_an_image_with_the_old_version_fails_and_names_it(tmp_path: Path) -> None:
    _installed(
        tmp_path,
        libzstd1="installed 1.5.7+dfsg-1",
        zlib1g="installed 1:1.3.dfsg+really1.3.1-1+deb13u1",
    )
    done = _check(tmp_path)
    assert done.returncode == 1
    assert "libzstd1 1.5.7+dfsg-1 is older than the fixed 1.5.7+dfsg-1+deb13u1" in done.stdout


@needs_sh
@pytest.mark.parametrize("line", [None, "config-files 1:1.3.dfsg+really1.3.1-1+deb13u1"])
def test_a_package_that_is_not_installed_fails(tmp_path: Path, line: str | None) -> None:
    _installed(tmp_path, libzstd1="installed 1.5.7+dfsg-1+deb13u1")
    if line is not None:
        _installed(tmp_path, zlib1g=line)
    done = _check(tmp_path)
    assert done.returncode == 1
    assert "zlib1g: not installed" in done.stdout


@needs_sh
def test_the_image_runs_without_network_read_only_on_the_given_platform(
    tmp_path: Path,
) -> None:
    _installed(
        tmp_path,
        libzstd1="installed 1.5.7+dfsg-1+deb13u1",
        zlib1g="installed 1:1.3.dfsg+really1.3.1-1+deb13u1",
    )
    done = _check(
        tmp_path, str(DOCKERFILE), "registry.example/manager@sha256:" + "a" * 64, "linux/amd64"
    )
    assert done.returncode == 0, done.stderr
    args = _docker_args(tmp_path)
    assert args[:2] == ["run", "--rm"]
    assert "--name" not in args
    assert args[args.index("--platform") + 1] == "linux/amd64"
    assert args[args.index("--network") + 1] == "none"
    assert "--read-only" in args
    assert args[args.index("--entrypoint") + 1] == "sh"
    assert "registry.example/manager@sha256:" + "a" * 64 in args
    assert args[-2:] == [
        "libzstd1=1.5.7+dfsg-1+deb13u1",
        "zlib1g=1:1.3.dfsg+really1.3.1-1+deb13u1",
    ]


@needs_sh
def test_the_container_takes_the_given_name(tmp_path: Path) -> None:
    _installed(
        tmp_path,
        libzstd1="installed 1.5.7+dfsg-1+deb13u1",
        zlib1g="installed 1:1.3.dfsg+really1.3.1-1+deb13u1",
    )
    assert _check(tmp_path, name="fixed-packages-check").returncode == 0
    args = _docker_args(tmp_path)
    assert args[args.index("--name") + 1] == "fixed-packages-check"


@needs_sh
@pytest.mark.parametrize(
    "argv",
    [
        (),
        ("only-one",),
        ("a", "b"),
        ("a", "b", "c", "d"),
    ],
)
def test_the_check_takes_exactly_three_arguments(tmp_path: Path, argv: tuple[str, ...]) -> None:
    if not argv:
        assert SH is not None
        done = subprocess.run([SH, str(CHECK)], capture_output=True, text=True, check=False)
    else:
        done = _check(tmp_path, *argv)
    assert done.returncode == 2
    assert "usage:" in done.stderr


@needs_sh
@pytest.mark.parametrize(
    ("image", "platform"),
    [
        ("ghcr.io/fidonis/papaia-manager:1.8.0", "linux/386"),
        ("ghcr.io/fidonis/papaia-manager:1.8.0", "linux/arm64;id"),
        ("ghcr.io/fidonis/papaia-manager:1.8.0", ""),
        ("-v", "linux/amd64"),
        ("", "linux/amd64"),
        ("image name", "linux/amd64"),
        ("image$(id)", "linux/amd64"),
    ],
)
def test_bad_arguments_are_refused(tmp_path: Path, image: str, platform: str) -> None:
    done = _check(tmp_path, str(DOCKERFILE), image, platform)
    assert done.returncode == 2
    assert not (tmp_path / "docker-args").exists()


@needs_sh
@pytest.mark.parametrize(
    "pins",
    [
        "",
        "ARG FIXED_ZLIB1G=1.0 extra\n",
        "ARG FIXED_ZLIB1G=$(id)\n",
        "ARG FIXED_ZLIB1G=\n",
        "ARG FIXED_ZLIB1G=;id\n",
        "ARG FIXED_BAD$NAME=1.0\n",
    ],
)
def test_a_dockerfile_without_valid_pins_is_refused(tmp_path: Path, pins: str) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text(f"FROM scratch\n{pins}", encoding="utf-8")
    done = _check(tmp_path, dockerfile=dockerfile)
    assert done.returncode == 2
    assert not (tmp_path / "docker-args").exists()


@needs_sh
def test_a_missing_dockerfile_is_refused(tmp_path: Path) -> None:
    done = _check(tmp_path, dockerfile=tmp_path / "missing")
    assert done.returncode == 2
    assert not (tmp_path / "docker-args").exists()


def test_the_docker_workflow_checks_both_platforms_before_signing() -> None:
    workflow = (REPO / ".github" / "workflows" / "docker.yml").read_text(encoding="utf-8")
    check = workflow.index("docker/check-fixed-packages.sh docker/Dockerfile")
    assert workflow.index("name: Build and push") < check < workflow.index("cosign sign")
    step = workflow[check - 600 : check + 200]
    assert "linux/amd64 linux/arm64" in step
