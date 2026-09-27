"""Append-only JSONL audit log."""
from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_SECRET_KEY_RE = re.compile(
    r"(secret|password|passwd|token|api[_-]?key|authorization|credential)", re.IGNORECASE
)
# Userinfo of a URL that starts the string, e.g. `https://user:pw@host/...`. Only
# the scheme-anchored form is handled -- catalog URLs are the one place this
# feature puts a credential-bearing URL in front of the log, and they are never
# embedded inside a longer string.
_URL_USERINFO_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.\-]*://)[^/@]+@")


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


def redact_params(value: Any) -> Any:
    """Recursively mask secret-looking values before they reach the log or a viewer.

    A dict value is replaced with ``***`` when its key looks like a credential
    (token, password, ...); a string that starts with a URL carrying userinfo
    (``https://user:pw@host/...``) has that userinfo masked. Callers pass their
    params through this before ``write_audit_entry`` -- and a future viewer
    applies it again on read, since a caller predating this feature may not have.
    """
    if isinstance(value, dict):
        return {
            k: "***" if _SECRET_KEY_RE.search(k) else redact_params(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [redact_params(v) for v in value]
    if isinstance(value, str):
        return _URL_USERINFO_RE.sub(lambda m: f"{m.group(1)}***@", value)
    return value
