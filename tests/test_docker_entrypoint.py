"""The image's entrypoint: a user id without a name gets one, because ssh insists on it."""
from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

ENTRYPOINT = Path(__file__).resolve().parents[1] / "docker" / "entrypoint.sh"
SH = shutil.which("sh")

pytestmark = pytest.mark.skipif(SH is None, reason="sh is not available")


def _run(tmp_path: Path, id_script: str) -> dict[str, str]:
    stub = tmp_path / "id"
    stub.write_text(f"#!/bin/sh\n{id_script}\n", encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    env.pop("LD_PRELOAD", None)
    env.pop("NSS_WRAPPER_PASSWD", None)
    done = subprocess.run(
        [str(SH), str(ENTRYPOINT), "sh", "-c", "env"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )
    return dict(line.split("=", 1) for line in done.stdout.splitlines() if "=" in line)


def test_a_known_user_is_left_alone(tmp_path: Path) -> None:
    assert "NSS_WRAPPER_PASSWD" not in _run(tmp_path, 'echo app')


def test_an_unknown_user_gets_a_passwd_entry(tmp_path: Path) -> None:
    script = """case "$1" in -un) exit 1 ;; -u) echo 1000 ;; -g) echo 1000 ;; esac"""
    env = _run(tmp_path, script)
    entry = Path(env["NSS_WRAPPER_PASSWD"]).read_text(encoding="utf-8").splitlines()[-1]
    assert entry.startswith("papaia:x:1000:1000:")
    assert env["LD_PRELOAD"] == "libnss_wrapper.so"
