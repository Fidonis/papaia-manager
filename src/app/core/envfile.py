"""Reading of `KEY=value` environment files.

The manager reads the core `.env` and add-on bundle `.env` files in several
places. This module is the single implementation of that parse so the
callers agree on what a comment, a blank line and a value containing `=`
mean.

Deliberately minimal: no quote stripping, no interpolation, no `export`
handling. These files are written by the papAIa orchestrator, not by hand,
and adding shell semantics here would silently diverge from how Compose
reads the very same files.
"""
from __future__ import annotations

from pathlib import Path


def parse_env_text(text: str) -> dict[str, str]:
    """Parse `KEY=value` lines, ignoring blanks and `#` comments.

    The first `=` separates key from value, so values may contain `=`.
    """
    result: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        result[key.strip()] = value.strip()
    return result


def load_env_file(path: Path) -> dict[str, str]:
    """Parse an env file, returning an empty mapping if it does not exist.

    A missing file is a normal state (a profile that was never enabled), not
    an error -- callers fall back to leaving placeholders unresolved.
    """
    if not path.is_file():
        return {}
    return parse_env_text(path.read_text(encoding="utf-8"))
