from __future__ import annotations

import secrets

from flask import abort, current_app, request, session
from markupsafe import Markup, escape


_CSRF_SESSION_KEY = "_csrf_token"
_SAFE_METHODS = {"GET", "HEAD", "OPTIONS", "TRACE"}
_EXEMPT_PATHS = {
    "/api/ingest",
    "/api/auth/login",
}


def csrf_enabled() -> bool:
    return bool(current_app.config.get("WTF_CSRF_ENABLED", True))


def get_csrf_token() -> str:
    token = session.get(_CSRF_SESSION_KEY)
    if not token:
        token = secrets.token_urlsafe(32)
        session[_CSRF_SESSION_KEY] = token
    return token


def csrf_input() -> Markup:
    token = escape(get_csrf_token())
    return Markup(f'<input type="hidden" name="csrf_token" value="{token}">')


def _submitted_token() -> str:
    header_token = (request.headers.get("X-CSRF-Token") or "").strip()
    if header_token:
        return header_token

    form_token = (request.form.get("csrf_token") or "").strip()
    if form_token:
        return form_token

    payload = request.get_json(silent=True)
    if isinstance(payload, dict):
        return str(payload.get("csrf_token") or "").strip()
    return ""


def protect_request() -> None:
    if not csrf_enabled():
        return
    if request.method.upper() in _SAFE_METHODS:
        return
    if request.path in _EXEMPT_PATHS:
        return

    expected = session.get(_CSRF_SESSION_KEY) or get_csrf_token()
    provided = _submitted_token()
    if not expected or not secrets.compare_digest(provided, expected):
        abort(400, description="CSRF token missing or invalid.")
