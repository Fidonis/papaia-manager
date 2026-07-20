"""Unit tests for envvalidate.coerce_env_values."""
from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from app.core.envforms import build_form
from app.core.envvalidate import EnvValidationError, coerce_env_values

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_addon(tmp_path: Path, env_lines: list[str], manifest: dict | None = None) -> Path:  # type: ignore[type-arg]
    (tmp_path / ".env.example").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    if manifest:
        (tmp_path / "papaia-app.yaml").write_text(yaml.dump(manifest), encoding="utf-8")
    return tmp_path


# ---------------------------------------------------------------------------
# Newline / CR injection guard
# ---------------------------------------------------------------------------


def test_newline_in_value_rejected(tmp_path: Path) -> None:
    _make_addon(tmp_path, ["FOO=bar"])
    fields = build_form(tmp_path)
    with pytest.raises(EnvValidationError) as exc_info:
        coerce_env_values(fields, {"FOO": "bar\nEVIL=1"})
    assert exc_info.value.field == "FOO"


def test_cr_in_value_rejected(tmp_path: Path) -> None:
    _make_addon(tmp_path, ["FOO=bar"])
    fields = build_form(tmp_path)
    with pytest.raises(EnvValidationError) as exc_info:
        coerce_env_values(fields, {"FOO": "bar\rEVIL=1"})
    assert exc_info.value.field == "FOO"


# ---------------------------------------------------------------------------
# Declared integer
# ---------------------------------------------------------------------------


def test_declared_integer_accepted(tmp_path: Path) -> None:
    _make_addon(tmp_path, ["PORT=8080"], {"env_prompts": {"PORT": {"type": "integer"}}})
    fields = build_form(tmp_path)
    out, warns = coerce_env_values(fields, {"PORT": "9090"})
    assert out["PORT"] == "9090"
    assert warns == []


def test_declared_integer_rejected(tmp_path: Path) -> None:
    _make_addon(tmp_path, ["PORT=8080"], {"env_prompts": {"PORT": {"type": "integer"}}})
    fields = build_form(tmp_path)
    with pytest.raises(EnvValidationError) as exc_info:
        coerce_env_values(fields, {"PORT": "abc"})
    assert exc_info.value.field == "PORT"
    assert "integer" in exc_info.value.message


def test_declared_integer_min_rejected(tmp_path: Path) -> None:
    _make_addon(
        tmp_path, ["PORT=8080"], {"env_prompts": {"PORT": {"type": "integer", "min": 1024}}}
    )
    fields = build_form(tmp_path)
    with pytest.raises(EnvValidationError) as exc_info:
        coerce_env_values(fields, {"PORT": "80"})
    assert "minimum" in exc_info.value.message


def test_declared_integer_max_rejected(tmp_path: Path) -> None:
    _make_addon(
        tmp_path, ["PORT=8080"],
        {"env_prompts": {"PORT": {"type": "integer", "min": 1, "max": 65535}}}
    )
    fields = build_form(tmp_path)
    with pytest.raises(EnvValidationError) as exc_info:
        coerce_env_values(fields, {"PORT": "99999"})
    assert "maximum" in exc_info.value.message


# ---------------------------------------------------------------------------
# Declared decimal
# ---------------------------------------------------------------------------


def test_declared_decimal_accepted(tmp_path: Path) -> None:
    _make_addon(tmp_path, ["RATE=1.0"], {"env_prompts": {"RATE": {"type": "decimal"}}})
    fields = build_form(tmp_path)
    out, warns = coerce_env_values(fields, {"RATE": "2,5"})
    assert out["RATE"] == "2.5"
    assert warns == []


def test_declared_decimal_rejected(tmp_path: Path) -> None:
    _make_addon(tmp_path, ["RATE=1.0"], {"env_prompts": {"RATE": {"type": "decimal"}}})
    fields = build_form(tmp_path)
    with pytest.raises(EnvValidationError):
        coerce_env_values(fields, {"RATE": "not-a-number"})


# ---------------------------------------------------------------------------
# Declared URL
# ---------------------------------------------------------------------------


def test_declared_url_accepted(tmp_path: Path) -> None:
    _make_addon(tmp_path, ["URL=http://localhost"], {"env_prompts": {"URL": {"type": "url"}}})
    fields = build_form(tmp_path)
    out, _ = coerce_env_values(fields, {"URL": "https://example.com"})
    assert out["URL"] == "https://example.com"


def test_declared_url_ftp_rejected(tmp_path: Path) -> None:
    _make_addon(tmp_path, ["URL=http://localhost"], {"env_prompts": {"URL": {"type": "url"}}})
    fields = build_form(tmp_path)
    with pytest.raises(EnvValidationError) as exc_info:
        coerce_env_values(fields, {"URL": "ftp://files.example.com"})
    assert "http" in exc_info.value.message


def test_declared_url_bare_host_rejected(tmp_path: Path) -> None:
    _make_addon(tmp_path, ["URL=http://localhost"], {"env_prompts": {"URL": {"type": "url"}}})
    fields = build_form(tmp_path)
    with pytest.raises(EnvValidationError):
        coerce_env_values(fields, {"URL": "example.com"})


# ---------------------------------------------------------------------------
# Inferred types — warn, never reject
# ---------------------------------------------------------------------------


def test_inferred_integer_mismatch_warns(tmp_path: Path) -> None:
    _make_addon(tmp_path, ["PORT=8080"])  # no manifest → inferred integer
    fields = build_form(tmp_path)
    out, warns = coerce_env_values(fields, {"PORT": "not-a-number"})
    assert out["PORT"] == "not-a-number"
    assert any("warn" in w.lower() for w in warns)


def test_inferred_decimal_mismatch_warns(tmp_path: Path) -> None:
    _make_addon(tmp_path, ["RATE=1.5"])  # inferred decimal
    fields = build_form(tmp_path)
    out, warns = coerce_env_values(fields, {"RATE": "not-a-number"})
    assert out["RATE"] == "not-a-number"
    assert any("warn" in w.lower() for w in warns)


# ---------------------------------------------------------------------------
# Unknown keys pass through with warning
# ---------------------------------------------------------------------------


def test_unknown_key_passes_through(tmp_path: Path) -> None:
    _make_addon(tmp_path, ["FOO=bar"])
    fields = build_form(tmp_path)
    out, warns = coerce_env_values(fields, {"FOO": "baz", "EXTRA_KEY": "value"})
    assert out["EXTRA_KEY"] == "value"
    assert any("EXTRA_KEY" in w for w in warns)


# ---------------------------------------------------------------------------
# Empty value for optional field — allowed
# ---------------------------------------------------------------------------


def test_empty_value_for_optional_field_passes(tmp_path: Path) -> None:
    _make_addon(tmp_path, ["APP_PORT=8080"], {"env_prompts": {"APP_PORT": {"type": "integer"}}})
    fields = build_form(tmp_path)
    out, warns = coerce_env_values(fields, {"APP_PORT": ""})
    assert out["APP_PORT"] == ""
    assert warns == []


# ---------------------------------------------------------------------------
# Whitespace trimming
# ---------------------------------------------------------------------------


def test_values_are_trimmed(tmp_path: Path) -> None:
    _make_addon(tmp_path, ["FOO=bar"])
    fields = build_form(tmp_path)
    out, _ = coerce_env_values(fields, {"FOO": "  hello world  "})
    assert out["FOO"] == "hello world"
