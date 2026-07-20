"""Env-form spec: translate .env.example + manifest prompts into form fields."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import yaml

# Marker values in .env.example and their semantics:
#   CHANGE_ME       — operator must supply a value before install
#   GENERATE_*      — auto-generated random secret on first install (seed_addon_env)
#   REPLACE_WITH_*  — filled after a Keycloak client import
#   (anything else) — literal default, copied verbatim

MARKER_NONE = ""
MARKER_CHANGE_ME = "change_me"
MARKER_GENERATE = "generate"
MARKER_REPLACE_WITH = "replace_with"

VALUE_TYPES = ("text", "integer", "decimal", "url")

_GENERATE_RE = re.compile(r"^GENERATE_\w+$")
_REPLACE_WITH_RE = re.compile(r"^REPLACE_WITH_\w+$")

# Name heuristic for secrets — fallback only; manifest secret: true/false wins.
# PASS$/PASSWD$ anchored at end catches PAPERLESS_DBPASS without BYPASS_X.
_SECRET_RE = re.compile(
    r"SECRET|PASSWORD|TOKEN|API_KEY|CREDENTIAL|PASSWD$|PASS$|_KEY$",
    re.IGNORECASE,
)


@dataclass
class EnvField:
    key: str
    label: str
    default: str            # resolved default (after default_from_core)
    required: bool
    is_secret: bool
    current_set: bool = False
    hint: str = ""
    auto_handled: bool = False
    current_value: str | None = None
    prompt_on_install: bool = False  # deprecated — kept for payload compat
    # ── type metadata ──────────────────────────────────────────────────────
    value_type: str = "text"         # one of VALUE_TYPES
    type_declared: bool = False      # True only when the manifest declared type:
    # ── marker ─────────────────────────────────────────────────────────────
    marker: str = MARKER_NONE
    auto_generated: bool = False     # marker == MARKER_GENERATE
    # ── prefill / placeholder ───────────────────────────────────────────────
    example_value: str = ""          # raw .env.example literal (pre-resolution)
    prefill: str = ""                # initial value= for non-secret inputs
    placeholder: str = ""           # placeholder= text
    # ── HTML constraint attributes ──────────────────────────────────────────
    pattern: str = ""
    input_mode: str = ""
    step: str = ""
    min_value: str | None = None
    max_value: str | None = None

    def html_input_type(self) -> str:
        if self.is_secret:
            return "password"
        return {"integer": "number", "decimal": "number", "url": "url"}.get(
            self.value_type, "text"
        )


def field_to_dict(f: EnvField) -> dict[str, Any]:
    """Canonical serialization used by both the JSON API and template context."""
    d = asdict(f)
    d["html_input_type"] = f.html_input_type()
    # Secrets never expose current_value — it is always None by construction,
    # but make the intent explicit in the serialized form too.
    if f.is_secret:
        d["current_value"] = None
    return d


def build_form(
    addon_path: Path,
    *,
    bundle_env: dict[str, str] | None = None,
    core_env: dict[str, str] | None = None,
    auth_provider: str = "internal_keycloak",
) -> list[EnvField]:
    """Return form fields derived from the addon's .env.example.

    All keys from .env.example are included (no filtering). Bundle .env values
    supply current_set; .env.example values serve as prefill for non-secret
    non-marker fields. env_prompts from papaia-app.yaml enrich labels, hints,
    types, and secret declarations.
    """
    env_example = addon_path / ".env.example"
    if not env_example.exists():
        return []

    raw_env = _parse_env_file(env_example)
    manifest = _load_manifest(addon_path)
    env_prompts: dict[str, Any] = manifest.get("env_prompts") or {}

    replace_secrets_raw = manifest.get("env_replace_secrets") or {}
    if isinstance(replace_secrets_raw, list):
        # Tolerate plain list for forward-compat (authored as mapping, but guard).
        replace_secrets: dict[str, Any] = {k: {} for k in replace_secrets_raw}
    else:
        replace_secrets = dict(replace_secrets_raw)

    fields: list[EnvField] = []
    for key, example_value in raw_env.items():
        prompt: dict[str, Any] = env_prompts.get(key) or {}

        # ── label ────────────────────────────────────────────────────────────
        label = str(prompt.get("label") or key.replace("_", " ").title())

        # ── hint: env_prompts wins, then env_replace_secrets fallback ─────────
        hint = str(prompt.get("hint") or "")
        if not hint and key in replace_secrets:
            rs_hint = (replace_secrets.get(key) or {}).get("hint", "")
            hint = str(rs_hint) if rs_hint else ""

        # ── default_from_core / env_prompts.default resolution ───────────────
        resolved_default = example_value
        if "default" in prompt:
            resolved_default = str(prompt["default"])
        if "default_from_core" in prompt and core_env:
            core_key = str(prompt["default_from_core"])
            resolved_default = core_env.get(core_key, resolved_default)

        # ── marker classification ─────────────────────────────────────────────
        marker = _classify_marker(example_value)

        # ── current_set ───────────────────────────────────────────────────────
        is_placeholder_val = marker != MARKER_NONE
        current_set = bool(
            bundle_env
            and key in bundle_env
            and (not is_placeholder_val or bundle_env[key] != example_value)
        )

        # ── secret resolution: manifest > markers > name heuristic ────────────
        declared_secret = prompt.get("secret")
        if isinstance(declared_secret, bool):
            is_secret = declared_secret
        else:
            is_secret = (
                bool(_SECRET_RE.search(key))
                or key in replace_secrets
                or marker in (MARKER_GENERATE, MARKER_REPLACE_WITH)
            )

        # Secrets never expose their current value to the browser — set to None
        # regardless of what the bundle .env contains.
        current_value = (
            bundle_env.get(key)
            if (bundle_env and current_set and not is_secret)
            else None
        )

        # ── type resolution: manifest > inference ─────────────────────────────
        value_type, type_declared = _resolve_type(prompt, example_value)
        step, input_mode = _type_html_attrs(value_type, type_declared)
        pattern = str(prompt.get("pattern") or "")
        min_value: str | None = str(prompt["min"]) if "min" in prompt else None
        max_value: str | None = str(prompt["max"]) if "max" in prompt else None

        # ── auto_generated / required / auto_handled ──────────────────────────
        auto_generated = marker == MARKER_GENERATE
        required = marker == MARKER_CHANGE_ME and not current_set
        auto = key in replace_secrets and auth_provider == "internal_keycloak"

        # ── prefill / placeholder ─────────────────────────────────────────────
        prefill, placeholder = _resolve_prefill(
            marker=marker,
            is_secret=is_secret,
            resolved_default=resolved_default,
        )

        # prompt_on_install is kept for payload compat but no longer used as a
        # display filter — all fields are shown.
        prompt_on_install = (
            marker in (MARKER_CHANGE_ME, MARKER_GENERATE, MARKER_REPLACE_WITH)
            or key in env_prompts
        )

        fields.append(
            EnvField(
                key=key,
                label=label,
                default=resolved_default,
                required=required,
                is_secret=is_secret,
                current_set=current_set,
                hint=hint,
                auto_handled=auto,
                current_value=current_value,
                prompt_on_install=prompt_on_install,
                value_type=value_type,
                type_declared=type_declared,
                marker=marker,
                auto_generated=auto_generated,
                example_value=example_value,
                prefill=prefill,
                placeholder=placeholder,
                pattern=pattern,
                input_mode=input_mode,
                step=step,
                min_value=min_value,
                max_value=max_value,
            )
        )

    return fields


def _classify_marker(value: str) -> str:
    if value == "CHANGE_ME":
        return MARKER_CHANGE_ME
    if _GENERATE_RE.match(value):
        return MARKER_GENERATE
    if _REPLACE_WITH_RE.match(value):
        return MARKER_REPLACE_WITH
    return MARKER_NONE


def _resolve_type(
    prompt: dict[str, Any], example_value: str
) -> tuple[str, bool]:
    declared = prompt.get("type")
    if isinstance(declared, str):
        cleaned = declared.strip().lower()
        if cleaned in VALUE_TYPES:
            return cleaned, True
        # Unknown type value in manifest — fall back silently.
    return _infer_type(example_value), False


def _infer_type(value: str) -> str:
    """Infer a display-hint type from the .env.example literal value.

    Inference is intentionally conservative — it drives only inputmode hints,
    never hard validation or type="number". Callers check type_declared before
    enforcing anything.
    """
    stripped = value.strip()
    if stripped.isdigit():
        return "integer"
    if stripped.startswith(("http://", "https://")):
        return "url"
    try:
        from decimal import Decimal, InvalidOperation  # noqa: PLC0415

        Decimal(stripped)
        if "." in stripped:
            return "decimal"
    except (InvalidOperation, ValueError):
        pass
    return "text"


def _type_html_attrs(value_type: str, type_declared: bool) -> tuple[str, str]:
    """Return (step, inputmode). Only non-empty for declared types."""
    if not type_declared:
        return "", ""
    if value_type == "integer":
        return "1", "numeric"
    if value_type == "decimal":
        return "any", "decimal"
    if value_type == "url":
        return "", "url"
    return "", ""


def _resolve_prefill(
    *,
    marker: str,
    is_secret: bool,
    resolved_default: str,
) -> tuple[str, str]:
    """Return (prefill, placeholder).

    Secrets are never prefilled with a literal value — an auto-generated secret
    written as a prefill value would bypass seed_addon_env and start the service
    with the literal string GENERATE_SECRET as its key.
    """
    if is_secret:
        if marker == MARKER_GENERATE:
            return "", "wird automatisch generiert"
        if marker == MARKER_REPLACE_WITH:
            return "", "nach Keycloak-Import eintragen"
        return "", ""

    if marker == MARKER_CHANGE_ME:
        # Prefill if default_from_core or env_prompts.default resolved the value.
        if resolved_default and resolved_default != "CHANGE_ME":
            return resolved_default, resolved_default
        return "", ""

    if marker == MARKER_GENERATE:
        return "", "wird automatisch generiert"

    if marker == MARKER_REPLACE_WITH:
        return "", "nach Keycloak-Import eintragen"

    # Plain literal — use the resolved default.
    return resolved_default, resolved_default


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
