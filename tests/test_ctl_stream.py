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

from app.core.ctl import (
    MAX_LINE_BYTES,
    TRUNCATION_MARKER,
    CtlError,
    _stream_subprocess,
)


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


def test_an_overlong_line_is_truncated_and_the_rest_dropped() -> None:
    # #154: a line past the 1 MiB cap yields its head plus a visible marker,
    # and the discarded remainder never surfaces as extra fragments.
    program = (
        "import sys\n"
        "sys.stdout.write('start\\n')\n"
        f"sys.stdout.write('z' * {3 * MAX_LINE_BYTES})\n"
        "sys.stdout.write('\\n')\n"
        "sys.stdout.write('after\\n')\n"
    )
    lines = _stream_lines(program)
    assert lines == [
        "start",
        "z" * MAX_LINE_BYTES + TRUNCATION_MARKER,
        "after",
    ]


def test_an_overlong_tail_without_a_newline_is_truncated_once() -> None:
    # End of stream inside an over-long line: the head plus marker is the
    # only thing emitted, nothing else leaks from the discarded tail.
    program = f"import sys\nsys.stdout.write('z' * {3 * MAX_LINE_BYTES + 1})\n"
    assert _stream_lines(program) == ["z" * MAX_LINE_BYTES + TRUNCATION_MARKER]


def test_many_short_lines_still_arrive_one_by_one_after_a_long_one() -> None:
    # The incremental newline search must not lose or duplicate lines around
    # the chunk boundary after a truncation has reset the buffer.
    program = (
        "import sys\n"
        f"sys.stdout.write('z' * {MAX_LINE_BYTES - 512})\n"
        "sys.stdout.write('\\n')\n"
        "sys.stdout.write('one\\n')\n"
        "sys.stdout.write('two\\n')\n"
        "sys.stdout.write('three\\n')\n"
    )
    assert _stream_lines(program) == [
        "z" * (MAX_LINE_BYTES - 512),
        "one",
        "two",
        "three",
    ]


def test_a_line_just_over_the_cap_is_truncated_when_its_newline_follows() -> None:
    # The newline of a line a few bytes over the cap can arrive in the very
    # chunk that crosses it. The cap holds there too, however the pipe splits
    # the reads.
    program = (
        "import sys\n"
        f"sys.stdout.write('z' * {MAX_LINE_BYTES + 100})\n"
        "sys.stdout.write('\\n')\n"
        "sys.stdout.write('after\\n')\n"
    )
    assert _stream_lines(program) == [
        "z" * MAX_LINE_BYTES + TRUNCATION_MARKER,
        "after",
    ]


def test_a_line_of_exactly_the_cap_is_not_truncated() -> None:
    # Written as bytes: a text-mode "\n" is "\r\n" on Windows, which would put
    # one byte more into the line than the cap allows.
    program = (
        "import sys\n"
        f"sys.stdout.buffer.write(b'z' * {MAX_LINE_BYTES})\n"
        "sys.stdout.buffer.write(b'\\nafter\\n')\n"
    )
    assert _stream_lines(program) == ["z" * MAX_LINE_BYTES, "after"]


def test_the_cut_does_not_split_a_multibyte_character() -> None:
    # The cap lands between the two bytes of an "e acute" (U+00E9): the half
    # character is left out instead of showing as a replacement character
    # before the marker.
    program = (
        "import sys\n"
        f"sys.stdout.buffer.write(b'a' + b'\\xc3\\xa9' * {MAX_LINE_BYTES})\n"
        "sys.stdout.buffer.write(b'\\nafter\\n')\n"
    )
    head = "a" + "é" * ((MAX_LINE_BYTES - 2) // 2)
    assert _stream_lines(program) == [head + TRUNCATION_MARKER, "after"]
