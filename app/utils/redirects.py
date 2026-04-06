from __future__ import annotations

from urllib.parse import urlparse

from flask import request, url_for


def is_safe_redirect_target(target: str | None) -> bool:
    target = (target or "").strip()
    if not target:
        return False

    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc:
        return False
    if not target.startswith("/") or target.startswith("//"):
        return False
    return True


def safe_redirect_target(target: str | None, default: str) -> str:
    return target if is_safe_redirect_target(target) else default


def safe_next_from_request(default_endpoint: str = "main.index") -> str:
    target = request.args.get("next") or request.form.get("next")
    return safe_redirect_target(target, url_for(default_endpoint))

