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


_CLAUSE_RE = re.compile(r"^(>=|<=|==|!=|>|<|\^|~)?\s*(\S+)$")


def satisfies(version: object, constraint: str) -> bool | None:
    """Whether `version` meets `constraint`; None if either cannot be read.

    Covers the range syntax papaia-ctl accepts in manifests: comparison operators
    (`>= > <= < == !=`), caret `^` and tilde `~`, and comma-separated clauses that must
    all hold. A bare version means `==`. As in papaia-ctl, a pre-release only satisfies a
    range when one of its bounds is a pre-release of the same `MAJOR.MINOR.PATCH`, so
    `1.3.0-rc.1` does not meet `>=1.2.0`. The manager only mirrors papaia-ctl's verdict
    for display, so anything it cannot read (including an empty clause, which papaia-ctl
    rejects) is "not checked", never a refusal."""
    version_key = _key(str(version))
    if version_key is None:
        return None
    clauses = [c.strip() for c in constraint.split(",")]
    if not all(clauses):
        return None
    bounds = [_CLAUSE_RE.match(c) for c in clauses]
    if any(b is None or _key(b.group(2)) is None for b in bounds):
        return None
    if not version_key[1] and not any(
        (k := _key(b.group(2))) is not None and not k[1] and k[0] == version_key[0]
        for b in bounds
        if b is not None
    ):
        return False
    for clause in clauses:
        match = _CLAUSE_RE.match(clause)
        if match is None:
            return None
        op, bound = match.group(1) or "==", match.group(2)
        bound_key = _key(bound)
        if bound_key is None:
            return None
        if op in ("^", "~"):
            major, minor, patch = bound_key[0]
            if op == "~":
                upper = f"{major}.{minor + 1}.0"
            elif major > 0:
                upper = f"{major + 1}.0.0"
            elif minor > 0:
                upper = f"0.{minor + 1}.0"
            else:
                upper = f"0.0.{patch + 1}"
            low, high = compare(version, bound), compare(version, upper)
            if low is None or high is None or low < 0 or high >= 0:
                return False
            continue
        order = compare(version, bound)
        if order is None:
            return None
        ok = {
            ">=": order >= 0,
            ">": order > 0,
            "<=": order <= 0,
            "<": order < 0,
            "==": order == 0,
            "!=": order != 0,
        }[op]
        if not ok:
            return False
    return True
