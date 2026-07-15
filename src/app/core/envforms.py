"""Env-form spec: translate .env.example + manifest prompts into form fields."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

_SECRET_RE = re.compile(r"SECRET|PASSWORD|TOKEN|API_KEY", re.IGNORECASE)
_CHANGE_ME_RE = re.compile(r"^(CHANGE_ME|GENERATE_\w+|REPLACE_WITH_\w+)$")


@dataclass
class EnvField:
    key: str
    label: str
    default: str
    required: bool
    is_secret: bool
    current_set: bool = False
    hint: str = ""
    auto_handled: bool = False


def build_form(
    addon_path: Path,
    *,
    bundle_env: dict[str, str] | None = None,
    core_env: dict[str, str] | None = None,
    auth_provider: str = "internal_keycloak",
) -> list[EnvField]:
    """Return a list of form fields derived from the addon's .env.example.

    - Keys without CHANGE_ME/GENERATE_* are omitted (already seeded).
    - env_prompts from papaia-app.yaml enrich labels and defaults.
    - env_replace_secrets keys are marked auto_handled when internal_keycloak.
    """
    env_example = addon_path / ".env.example"
    if not env_example.exists():
        return []

    raw_env = _parse_env_file(env_example)
    manifest = _load_manifest(addon_path)
    env_prompts: dict[str, Any] = manifest.get("env_prompts", {})
    replace_secrets: list[str] = manifest.get("env_replace_secrets", [])

    fields: list[EnvField] = []
    for key, default in raw_env.items():
        if not _CHANGE_ME_RE.match(default):
            continue

        prompt: dict[str, Any] = env_prompts.get(key, {})
        label = str(prompt.get("label", key.replace("_", " ").title()))
        hint = str(prompt.get("hint", ""))

        resolved_default = default
        if "default_from_core" in prompt and core_env:
            core_key = str(prompt["default_from_core"])
            resolved_default = core_env.get(core_key, default)

        is_secret = bool(_SECRET_RE.search(key))
        current_set = bool(bundle_env and key in bundle_env and bundle_env[key] != default)
        auto = key in replace_secrets and auth_provider == "internal_keycloak"

        fields.append(
            EnvField(
                key=key,
                label=label,
                default=resolved_default,
                required=not current_set,
                is_secret=is_secret,
                current_set=current_set,
                hint=hint,
                auto_handled=auto,
            )
        )

    return fields


def _parse_env_file(path: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, val = line.partition("=")
            result[key.strip()] = val.strip()
    return result


def _load_manifest(addon_path: Path) -> dict[str, Any]:
    manifest_file = addon_path / "papaia-app.yaml"
    if not manifest_file.exists():
        return {}
    raw: dict[str, Any] = yaml.safe_load(manifest_file.read_text(encoding="utf-8")) or {}
    return raw
