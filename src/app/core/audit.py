"""Append-only JSONL audit log."""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def write_audit_entry(
    config_dir: str,
    *,
    user: str,
    action: str,
    target: str,
    params: dict[str, Any] | None = None,
    job_id: str | None = None,
    result: str = "ok",
) -> None:
    """Append one entry to the audit log.

    The log lives at ``$PAPAIA_CONFIG_DIR/manager/audit.log`` and is
    append-only. Sensitive values (tokens, secrets) must be redacted by
    the caller before passing them in ``params``.
    """
    audit_path = Path(config_dir) / "manager" / "audit.log"
    audit_path.parent.mkdir(parents=True, exist_ok=True)

    entry: dict[str, Any] = {
        "ts": datetime.now(tz=UTC).isoformat(),
        "user": user,
        "action": action,
        "target": target,
        "result": result,
    }
    if params is not None:
        entry["params"] = params
    if job_id is not None:
        entry["job_id"] = job_id

    with audit_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry) + "\n")
