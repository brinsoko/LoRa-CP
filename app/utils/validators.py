from __future__ import annotations

import math
import re

_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


def validate_finite_float(
    value,
    *,
    field_name: str,
    minimum: float | None = None,
    maximum: float | None = None,
) -> tuple[float | None, str | None]:
    """Coerce to float and reject NaN/inf and out-of-range values.

    Returns (parsed_value, error_message). On error, parsed_value is None.
    """
    if value is None or value == "":
        return None, None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None, f"{field_name} must be a number"
    if not math.isfinite(parsed):
        return None, f"{field_name} must be a finite number"
    if minimum is not None and parsed < minimum:
        return None, f"{field_name} must be >= {minimum}"
    if maximum is not None and parsed > maximum:
        return None, f"{field_name} must be <= {maximum}"
    return parsed, None


def validate_positive_int(
    value,
    *,
    field_name: str,
    maximum: int | None = None,
) -> tuple[int | None, str | None]:
    """Coerce to int, reject zero/negatives. Returns (parsed_value, error)."""
    if value is None or value == "":
        return None, None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None, f"{field_name} must be an integer"
    if parsed <= 0:
        return None, f"{field_name} must be > 0"
    if maximum is not None and parsed > maximum:
        return None, f"{field_name} must be <= {maximum}"
    return parsed, None


def _contains_control_chars(value: str) -> bool:
    return bool(_CONTROL_CHARS_RE.search(value))


def validate_text(
    value: str | None,
    *,
    field_name: str,
    max_length: int,
    required: bool = False,
    multiline: bool = False,
) -> tuple[str | None, str | None]:
    cleaned = (value or "").strip()
    if not cleaned:
        if required:
            return None, f"{field_name} is required"
        return None, None
    if len(cleaned) > max_length:
        return None, f"{field_name} must be at most {max_length} characters"
    if _contains_control_chars(cleaned):
        return None, f"{field_name} contains invalid control characters"
    if not multiline and ("\n" in cleaned or "\r" in cleaned):
        return None, f"{field_name} must be a single line"
    return cleaned, None


def validate_username(value: str | None) -> tuple[str | None, str | None]:
    cleaned, err = validate_text(value, field_name="username", max_length=50, required=True)
    if err:
        return None, err
    if not _USERNAME_RE.fullmatch(cleaned):
        return None, "username may contain only letters, numbers, dot, underscore, and hyphen"
    return cleaned, None


def validate_email(value: str | None) -> tuple[str | None, str | None]:
    cleaned, err = validate_text(value, field_name="email", max_length=255, required=False)
    if err or cleaned is None:
        return cleaned, err
    lowered = cleaned.lower()
    if "@" not in lowered or lowered.startswith("@") or lowered.endswith("@"):
        return None, "email is invalid"
    return lowered, None

