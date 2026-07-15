"""Bootstrap the papaia lib import path and run the startup version handshake."""
from __future__ import annotations

import logging
import sys
from pathlib import Path

logger = logging.getLogger(__name__)

# Range of papaia core versions the manager is tested against.
# Outside this range the manager warns but does not refuse to start —
# the operator must not lose UI access just because the core is newer.
SUPPORTED_CORE_MIN = (0, 9, 0)
SUPPORTED_CORE_MAX = (2, 0, 0)  # exclusive


def bootstrap(workspace_dir: str) -> None:
    """Add papaia/tools to sys.path and verify core compatibility.

    Must be called once at application startup before any lib.* import.
    Raises RuntimeError if the workspace layout is missing; logs a warning
    if the core version falls outside the supported range.
    """
    tools_path = Path(workspace_dir) / "papaia" / "tools"
    if not tools_path.is_dir():
        raise RuntimeError(
            f"papaia tools directory not found: {tools_path}. "
            "Ensure PAPAIA_WORKSPACE_DIR points to the correct path."
        )

    str_path = str(tools_path)
    if str_path not in sys.path:
        sys.path.insert(0, str_path)
        logger.info("added %s to sys.path", str_path)

    _check_core_version(workspace_dir)


def _check_core_version(workspace_dir: str) -> None:
    version_file = Path(workspace_dir) / "papaia" / "VERSION"
    if not version_file.exists():
        logger.warning("papaia/VERSION not found; skipping core version check")
        return

    raw = version_file.read_text(encoding="utf-8").strip()
    try:
        parts = tuple(int(x) for x in raw.lstrip("v").split(".")[:3])
        while len(parts) < 3:
            parts = (*parts, 0)
        ver: tuple[int, int, int] = (parts[0], parts[1], parts[2])
    except (ValueError, IndexError):
        logger.warning("cannot parse papaia core version %r; skipping check", raw)
        return

    if ver < SUPPORTED_CORE_MIN or ver >= SUPPORTED_CORE_MAX:
        logger.warning(
            "papaia core version %s is outside supported range [%s, %s); "
            "the manager may behave incorrectly",
            raw,
            ".".join(str(x) for x in SUPPORTED_CORE_MIN),
            ".".join(str(x) for x in SUPPORTED_CORE_MAX),
        )
    else:
        logger.info("papaia core version %s is within supported range", raw)
