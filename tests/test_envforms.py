"""Unit tests for envforms.build_form and EnvField."""
from __future__ import annotations

from pathlib import Path

import yaml

from app.core.envforms import (
    MARKER_CHANGE_ME,
    MARKER_GENERATE,
    MARKER_NONE,
    MARKER_REPLACE_WITH,
    build_form,
    field_to_dict,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_env(tmp_path: Path, lines: list[str]) -> None:
    (tmp_path / ".env.example").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_manifest(tmp_path: Path, data: dict) -> None:  # type: ignore[type-arg]
    (tmp_path / "papaia-app.yaml").write_text(yaml.dump(data), encoding="utf-8")


# ---------------------------------------------------------------------------
# Marker classification
# ---------------------------------------------------------------------------


def test_literal_marker(tmp_path: Path) -> None:
    _write_env(tmp_path, ["FOO=bar"])
    fields = build_form(tmp_path)
    f = fields[0]
    assert f.marker == MARKER_NONE
    assert f.prefill == "bar"
    assert not f.required
    assert not f.auto_generated


def test_change_me_marker(tmp_path: Path) -> None:
    _write_env(tmp_path, ["SECRET_KEY=CHANGE_ME"])
    f = build_form(tmp_path)[0]
    assert f.marker == MARKER_CHANGE_ME
    assert f.required is True
    assert f.prefill == ""


def test_generate_marker_not_required(tmp_path: Path) -> None:
    """GENERATE_* must never set required=True — regression guard for the original bug."""
    _write_env(tmp_path, ["SECRET_KEY=GENERATE_SECRET", "DB_PASS=GENERATE_PASSWORD"])
    fields = build_form(tmp_path)
    for f in fields:
        assert f.required is False, f"{f.key} must not be required"
        assert f.auto_generated
        assert f.marker == MARKER_GENERATE
        assert f.prefill == ""
        assert f.is_secret


def test_replace_with_marker(tmp_path: Path) -> None:
    _write_env(tmp_path, ["OIDC_SECRET=REPLACE_WITH_CLIENT_SECRET"])
    _write_manifest(tmp_path, {
        "env_replace_secrets": {
            "OIDC_SECRET": {"hint": "Copy from Keycloak"}
        }
    })
    f = build_form(tmp_path)[0]
    assert f.marker == MARKER_REPLACE_WITH
    assert f.required is False
    assert f.is_secret
    assert f.hint == "Copy from Keycloak"
    assert f.prefill == ""


# ---------------------------------------------------------------------------
# Type resolution
# ---------------------------------------------------------------------------


def test_declared_integer_type(tmp_path: Path) -> None:
    _write_env(tmp_path, ["APP_PORT=8080"])
    _write_manifest(tmp_path, {
        "env_prompts": {"APP_PORT": {"type": "integer", "min": 1, "max": 65535}}
    })
    f = build_form(tmp_path)[0]
    assert f.value_type == "integer"
    assert f.type_declared is True
    assert f.html_input_type() == "number"
    assert f.step == "1"
    assert f.min_value == "1"
    assert f.max_value == "65535"


def test_declared_url_type(tmp_path: Path) -> None:
    _write_env(tmp_path, ["APP_URL=http://localhost:8080"])
    _write_manifest(tmp_path, {"env_prompts": {"APP_URL": {"type": "url"}}})
    f = build_form(tmp_path)[0]
    assert f.value_type == "url"
    assert f.type_declared is True
    assert f.html_input_type() == "url"


def test_declared_decimal_type(tmp_path: Path) -> None:
    _write_env(tmp_path, ["RATE=1.5"])
    _write_manifest(tmp_path, {"env_prompts": {"RATE": {"type": "decimal"}}})
    f = build_form(tmp_path)[0]
    assert f.value_type == "decimal"
    assert f.type_declared is True
    assert f.step == "any"


def test_unknown_type_in_manifest_falls_back_to_inference(tmp_path: Path) -> None:
    _write_env(tmp_path, ["APP_PORT=8080"])
    _write_manifest(tmp_path, {"env_prompts": {"APP_PORT": {"type": "intger"}}})
    f = build_form(tmp_path)[0]
    # type_declared must be False — invalid manifest values are silently ignored
    assert f.type_declared is False
    # value_type is inferred from the literal ("8080" → integer), not the bad manifest value
    assert f.value_type == "integer"


def test_inferred_integer_type(tmp_path: Path) -> None:
    _write_env(tmp_path, ["APP_PORT=8080"])
    f = build_form(tmp_path)[0]
    assert f.value_type == "integer"
    assert f.type_declared is False


def test_inferred_url_type(tmp_path: Path) -> None:
    _write_env(tmp_path, ["APP_URL=https://example.com"])
    f = build_form(tmp_path)[0]
    assert f.value_type == "url"
    assert f.type_declared is False


# ---------------------------------------------------------------------------
# Secret resolution
# ---------------------------------------------------------------------------


def test_name_heuristic_detects_secret(tmp_path: Path) -> None:
    _write_env(tmp_path, ["APP_SECRET_KEY=GENERATE_SECRET"])
    f = build_form(tmp_path)[0]
    assert f.is_secret is True


def test_manifest_secret_true_wins(tmp_path: Path) -> None:
    _write_env(tmp_path, ["PAPERLESS_DBPASS=GENERATE_PASSWORD"])
    _write_manifest(tmp_path, {"env_prompts": {"PAPERLESS_DBPASS": {"secret": True}}})
    f = build_form(tmp_path)[0]
    assert f.is_secret is True


def test_manifest_secret_false_overrides_heuristic(tmp_path: Path) -> None:
    _write_env(tmp_path, ["AUTH_TOKEN_DISPLAY=some_public_value"])
    _write_manifest(tmp_path, {"env_prompts": {"AUTH_TOKEN_DISPLAY": {"secret": False}}})
    f = build_form(tmp_path)[0]
    assert f.is_secret is False


# ---------------------------------------------------------------------------
# current_value safety
# ---------------------------------------------------------------------------


def test_secret_current_value_is_none(tmp_path: Path) -> None:
    """Secrets must never expose current_value — even when bundle .env has a value."""
    _write_env(tmp_path, ["DB_PASSWORD=GENERATE_SECRET"])
    bundle = {"DB_PASSWORD": "supersecret"}
    f = build_form(tmp_path, bundle_env=bundle)[0]
    assert f.is_secret is True
    assert f.current_set is True
    assert f.current_value is None


def test_secret_current_value_none_in_dict(tmp_path: Path) -> None:
    _write_env(tmp_path, ["DB_PASSWORD=GENERATE_SECRET"])
    bundle = {"DB_PASSWORD": "supersecret"}
    d = field_to_dict(build_form(tmp_path, bundle_env=bundle)[0])
    assert d["current_value"] is None


def test_non_secret_current_value_visible(tmp_path: Path) -> None:
    _write_env(tmp_path, ["APP_PORT=8080"])
    bundle = {"APP_PORT": "9090"}
    f = build_form(tmp_path, bundle_env=bundle)[0]
    assert f.current_value == "9090"


# ---------------------------------------------------------------------------
# env_replace_secrets as mapping (not list)
# ---------------------------------------------------------------------------


def test_env_replace_secrets_as_mapping(tmp_path: Path) -> None:
    _write_env(tmp_path, ["OIDC_SECRET=REPLACE_WITH_CLIENT_SECRET"])
    _write_manifest(tmp_path, {
        "env_replace_secrets": {"OIDC_SECRET": {"hint": "from Keycloak"}}
    })
    f = build_form(tmp_path)[0]
    assert f.is_secret is True
    assert f.hint == "from Keycloak"


def test_env_replace_secrets_as_list_tolerated(tmp_path: Path) -> None:
    _write_env(tmp_path, ["OIDC_SECRET=REPLACE_WITH_CLIENT_SECRET"])
    _write_manifest(tmp_path, {"env_replace_secrets": ["OIDC_SECRET"]})
    f = build_form(tmp_path)[0]
    assert f.is_secret is True


# ---------------------------------------------------------------------------
# No manifest — graceful degradation
# ---------------------------------------------------------------------------


def test_no_manifest_returns_fields(tmp_path: Path) -> None:
    _write_env(tmp_path, ["FOO=bar", "DB_PASSWORD=GENERATE_SECRET"])
    fields = build_form(tmp_path)
    assert len(fields) == 2
    keys = {f.key for f in fields}
    assert "FOO" in keys and "DB_PASSWORD" in keys


def test_no_env_example_returns_empty(tmp_path: Path) -> None:
    assert build_form(tmp_path) == []


# ---------------------------------------------------------------------------
# default_from_core
# ---------------------------------------------------------------------------


def test_default_from_core_applied(tmp_path: Path) -> None:
    _write_env(tmp_path, ["OIDC_ISSUER=CHANGE_ME"])
    _write_manifest(tmp_path, {
        "env_prompts": {"OIDC_ISSUER": {"default_from_core": "PAPAIA_OIDC_ISSUER"}}
    })
    core = {"PAPAIA_OIDC_ISSUER": "https://keycloak.example.com/realms/papaia"}
    f = build_form(tmp_path, core_env=core)[0]
    # The core value is used as prefill — the operator can submit it as-is
    assert f.prefill == "https://keycloak.example.com/realms/papaia"
    assert f.marker == MARKER_CHANGE_ME
    # required stays True — the field is still operator-confirmed; the prefill satisfies it


# ---------------------------------------------------------------------------
# field_to_dict
# ---------------------------------------------------------------------------


def test_field_to_dict_includes_html_input_type(tmp_path: Path) -> None:
    _write_env(tmp_path, ["APP_PORT=8080"])
    _write_manifest(tmp_path, {"env_prompts": {"APP_PORT": {"type": "integer"}}})
    d = field_to_dict(build_form(tmp_path)[0])
    assert d["html_input_type"] == "number"
    assert d["type_declared"] is True
