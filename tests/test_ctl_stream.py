"""Streaming of papaia-ctl output must survive arbitrarily long lines.

`_stream_subprocess` used to iterate the asyncio pipe reader, which splits on
newlines with a 64 KiB limit and raises LimitOverrunError on longer lines
(docker's classic builder emits ANSI-escaped pull progress as one endless
line). These tests drive it with a real interpreter process, no docker
needed. They are written as sync wrappers around an ``asyncio.run`` driver so
they also run in environments without an async pytest plugin installed.
"""
from __future__ import annotations

import asyncio
import sys

import pytest

from app.core.ctl import CtlError, _stream_subprocess


def _stream_lines(program: str) -> list[str]:
    async def run() -> list[str]:
        return [
            line
            async for line in _stream_subprocess(
                [sys.executable, "-c", program],
                env={"PATH": "/usr/bin:/bin"},
            )
        ]

    return asyncio.run(run())


def test_lines_longer_than_the_64kib_reader_limit_survive() -> None:
    # >64 KiB in a single write with no newline anywhere -- what the docker
    # classic builder's pull progress looks like. The old reader iteration
    # raised LimitOverrunError here and failed the whole job.
    payload = "x" * (64 * 1024 + 4096)
    program = (
        "import sys\n"
        "sys.stdout.write('start\\n')\n"
        f"sys.stdout.write('x' * {len(payload)})\n"
        "sys.stdout.write('\\n')\n"
        "sys.stdout.write('done\\n')\n"
    )
    assert _stream_lines(program) == ["start", payload, "done"]


def test_partial_lines_across_chunk_boundaries_are_reassembled() -> None:
    # The chunked reader must not emit a fragment mid-word: a 70 KiB write
    # with no newline lands as one yielded line, not as two chunks.
    payload = "y" * (70 * 1024)
    program = f"import sys\nsys.stdout.write('y' * {len(payload)})\n"
    assert _stream_lines(program) == [payload]


def test_short_lines_still_arrive_one_by_one() -> None:
    program = (
        "import sys\n"
        "sys.stdout.write('one\\n')\n"
        "sys.stdout.write('two\\n')\n"
        "sys.stdout.write('three\\n')\n"
    )
    assert _stream_lines(program) == ["one", "two", "three"]


def test_trailing_line_without_newline_is_yielded() -> None:
    program = "import sys\nsys.stdout.write('partial')\n"
    assert _stream_lines(program) == ["partial"]


def test_nonzero_exit_still_raises_ctl_error() -> None:
    program = "import sys\nsys.stdout.write('out\\n')\nsys.exit(3)\n"
    with pytest.raises(CtlError, match="exited with code 3"):
        _stream_lines(program)
