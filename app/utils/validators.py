from __future__ import annotations

import re


_CONTROL_CHARS_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


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

