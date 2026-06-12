from __future__ import annotations

import math
import re

# Aliased as "_" so the documented pybabel extract command (which relies on
# Babel's default keywords) picks these msgids up. Unlike gettext, the
# returned LazyString resolves at render time, so building messages here at
# import/validation time is safe.
from flask_babel import lazy_gettext as _

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
    Error messages are lazy-translated; callers must not re-wrap them in
    gettext, and should coerce with str() before JSON-dumping outside Flask.
    """
    if value is None or value == "":
        return None, None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None, _("%(field)s must be a number", field=field_name)
    if not math.isfinite(parsed):
        return None, _("%(field)s must be a finite number", field=field_name)
    if minimum is not None and parsed < minimum:
        return None, _("%(field)s must be >= %(minimum)s", field=field_name, minimum=minimum)
    if maximum is not None and parsed > maximum:
        return None, _("%(field)s must be <= %(maximum)s", field=field_name, maximum=maximum)
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
        return None, _("%(field)s must be an integer", field=field_name)
    if parsed <= 0:
        return None, _("%(field)s must be > 0", field=field_name)
    if maximum is not None and parsed > maximum:
        return None, _("%(field)s must be <= %(maximum)s", field=field_name, maximum=maximum)
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
            return None, _("%(field)s is required", field=field_name)
        return None, None
    if len(cleaned) > max_length:
        return None, _("%(field)s must be at most %(max_length)s characters", field=field_name, max_length=max_length)
    if _contains_control_chars(cleaned):
        return None, _("%(field)s contains invalid control characters", field=field_name)
    if not multiline and ("\n" in cleaned or "\r" in cleaned):
        return None, _("%(field)s must be a single line", field=field_name)
    return cleaned, None


def validate_username(value: str | None) -> tuple[str | None, str | None]:
    cleaned, err = validate_text(value, field_name="username", max_length=50, required=True)
    if err:
        return None, err
    if not _USERNAME_RE.fullmatch(cleaned):
        return None, _("username may contain only letters, numbers, dot, underscore, and hyphen")
    return cleaned, None


def validate_email(value: str | None) -> tuple[str | None, str | None]:
    cleaned, err = validate_text(value, field_name="email", max_length=255, required=False)
    if err or cleaned is None:
        return cleaned, err
    lowered = cleaned.lower()
    if "@" not in lowered or lowered.startswith("@") or lowered.endswith("@"):
        return None, _("email is invalid")
    return lowered, None
