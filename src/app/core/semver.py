"""SemVer precedence for add-on versions.

An add-on manifest's `version` is meant to be `MAJOR.MINOR.PATCH` (optionally with a
pre-release and build metadata), but nothing forces it, so every function here says "not
comparable" instead of raising. The order is the one papaia-ctl uses to say whether an upgrade is
an upgrade or a downgrade, so the two agree on what "newer" means.
"""
from __future__ import annotations

import re

_VERSION_RE = re.compile(
    r"^(\d+)\.(\d+)\.(\d+)"
    r"(?:-(?P<pre>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)

_Key = tuple[tuple[int, int, int], bool, list[tuple[int, int, str]]]


def _key(text: str) -> _Key | None:
    match = _VERSION_RE.match(text.strip())
    if match is None:
        return None
    pre = match.group("pre")
    # A pre-release ranks below the release it precedes; numeric identifiers rank below
    # alphanumeric ones and compare as numbers (SemVer 2.0.0, item 11).
    identifiers = (
        [(0, int(p), "") if p.isdigit() else (1, 0, p) for p in pre.split(".")] if pre else []
    )
    triple = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    return triple, not pre, identifiers


def compare(a: object, b: object) -> int | None:
    """-1, 0 or 1 for the precedence of `a` against `b`; None if either is not SemVer."""
    ka, kb = _key(str(a)), _key(str(b))
    if ka is None or kb is None:
        return None
    if ka[0] != kb[0]:
        return -1 if ka[0] < kb[0] else 1
    if ka[1] != kb[1]:
        return -1 if kb[1] else 1
    if ka[2] == kb[2]:
        return 0
    return -1 if ka[2] < kb[2] else 1


def is_newer(candidate: object, installed: object) -> bool | None:
    """Whether `candidate` is a higher version than `installed`; None if not comparable."""
    order = compare(candidate, installed)
    return None if order is None else order > 0
