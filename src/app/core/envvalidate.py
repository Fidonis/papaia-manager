"""Server-side validation and coercion of add-on env values."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from urllib.parse import urlsplit

from app.core.envforms import MARKER_NONE, EnvField


class EnvValidationError(Exception):
    def __init__(self, field: str, message: str) -> None:
        self.field = field
        self.message = message
        super().__init__(f"{field}: {message}")


def coerce_env_values(
    fields: list[EnvField], env: dict[str, str]
) -> tuple[dict[str, str], list[str]]:
    """Validate and normalise operator-supplied env values.

    Returns ``(normalised_env, warnings)``.
    Raises :class:`EnvValidationError` for hard failures:
    - newline / carriage-return injection in any value (always rejected)
    - declared-type constraint violations (integer, decimal, url, pattern)

    Inferred-type mismatches produce a warning entry and pass through.
    Unknown keys (not in .env.example) produce a warning and pass through —
    the config editor allows adding keys that a newer image supports.
    """
    field_index = {f.key: f for f in fields}
    out: dict[str, str] = {}
    warnings: list[str] = []

    for key, raw_value in env.items():
        # Strip leading/trailing whitespace on all values.
        value = raw_value.strip()

        # Newline/CR injection guard — always hard-reject.
        # The three .env write paths use "\n".join(f"{k}={v}") without escaping,
        # so a value containing \n would inject arbitrary KEY=VALUE lines.
        if "\n" in value or "\r" in value:
            raise EnvValidationError(
                key, "value must not contain newline or carriage-return characters"
            )

        f = field_index.get(key)
        if f is None:
            warnings.append(
                f"[warn] {key}: not in .env.example — written as-is"
            )
            out[key] = value
            continue

        if f.type_declared:
            value = _coerce_declared(f, value)
        else:
            # Inferred type — hint only, never reject.
            if f.value_type == "integer" and value and not _is_integer(value):
                warnings.append(
                    f"[warn] {key}: expected integer (inferred), got {value!r} — written as-is"
                )
            elif f.value_type == "decimal" and value and not _is_decimal(value):
                warnings.append(
                    f"[warn] {key}: expected decimal (inferred), got {value!r} — written as-is"
                )

        out[key] = value

    return out, warnings


def _coerce_declared(f: EnvField, value: str) -> str:
    """Validate and normalise a value for a field with a declared type.

    Raises :class:`EnvValidationError` on constraint violations.
    Empty values are only rejected when the field is required.
    """
    if not value:
        if f.required:
            raise EnvValidationError(f.key, "required field must not be empty")
        return value

    if f.value_type == "integer":
        if not _is_integer(value):
            raise EnvValidationError(
                f.key, f"expected integer, got {value!r}"
            )
        int_val = int(value)
        if f.min_value is not None and int_val < int(f.min_value):
            raise EnvValidationError(
                f.key, f"value {int_val} is below minimum {f.min_value}"
            )
        if f.max_value is not None and int_val > int(f.max_value):
            raise EnvValidationError(
                f.key, f"value {int_val} exceeds maximum {f.max_value}"
            )

    elif f.value_type == "decimal":
        # Accept both comma and dot as decimal separator.
        normalised = value.replace(",", ".")
        if not _is_decimal(normalised):
            raise EnvValidationError(
                f.key, f"expected decimal number, got {value!r}"
            )
        value = normalised
        dec_val = Decimal(normalised)
        if f.min_value is not None and dec_val < Decimal(f.min_value):
            raise EnvValidationError(
                f.key, f"value {dec_val} is below minimum {f.min_value}"
            )
        if f.max_value is not None and dec_val > Decimal(f.max_value):
            raise EnvValidationError(
                f.key, f"value {dec_val} exceeds maximum {f.max_value}"
            )

    elif f.value_type == "url":
        parsed = urlsplit(value)
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise EnvValidationError(
                f.key,
                f"expected a URL with http or https scheme and a host, got {value!r}",
            )

    if f.pattern and f.value_type == "text":
        import re  # noqa: PLC0415

        if not re.fullmatch(f.pattern, value):
            raise EnvValidationError(
                f.key,
                f"value {value!r} does not match required pattern {f.pattern!r}",
            )

    return value


def _is_integer(value: str) -> bool:
    try:
        int(value)
        return True
    except ValueError:
        return False


def _is_decimal(value: str) -> bool:
    try:
        Decimal(value)
        return True
    except InvalidOperation:
        return False
