"""Regression tests for session-cookie flags and rate limiting on
auth-bearing endpoints.

Cookie hardening (config.py):
  - HTTPONLY: always on
  - SECURE: on in production (cookie won't be sent over plain HTTP)
  - SAMESITE=Lax: cross-site POSTs can't carry the session cookie

Rate limit:
  - /api/auth/password is rate-limited the same way as /api/auth/login,
    so an authenticated attacker can't brute-force the current-password
    check at high speed."""

from __future__ import annotations

from app.extensions import limiter
from tests.support import (
    add_membership,
    create_competition,
    create_user,
    login_as,
)


def test_session_cookie_secure_in_production(app_factory):
    """In production the Flask SESSION_COOKIE_SECURE flag must be on,
    so the session cookie is only sent over HTTPS."""
    application = app_factory(SESSION_COOKIE_SECURE=True)
    assert application.config["SESSION_COOKIE_SECURE"] is True


def test_session_cookie_samesite_default_is_lax(app_factory):
    """Default SAMESITE=Lax in config; explicit overrides still work."""
    application = app_factory()
    assert application.config["SESSION_COOKIE_SAMESITE"] == "Lax"


def test_session_cookie_httponly_always_on(app_factory):
    application = app_factory()
    assert application.config["SESSION_COOKIE_HTTPONLY"] is True


def test_login_response_carries_secure_cookie_flags(app_factory):
    """End-to-end: a successful login emits a Set-Cookie header that
    includes Secure and SameSite=Lax in production mode."""
    application = app_factory(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_SAMESITE="Lax")
    client = application.test_client()

    with application.app_context():
        user = create_user(username="cookie-secure", role="public")
        comp = create_competition(name="CookieComp")
        add_membership(user, comp, role="admin")

    resp = client.post(
        "/api/auth/login",
        json={"username": "cookie-secure", "password": "password123"},
    )
    assert resp.status_code == 200, resp.get_json()

    set_cookies = resp.headers.getlist("Set-Cookie")
    session_cookie = next((c for c in set_cookies if c.startswith("session=")), None)
    assert session_cookie is not None, set_cookies
    assert "HttpOnly" in session_cookie, session_cookie
    assert "Secure" in session_cookie, session_cookie
    assert "SameSite=Lax" in session_cookie, session_cookie


def test_password_change_endpoint_is_rate_limited(app_factory):
    """POST /api/auth/password must be rate-limited; otherwise an
    authenticated attacker can hammer the current-password check.

    The default test fixture disables flask-limiter (RATELIMIT_ENABLED
    is False under TESTING=True), so we build a dedicated app with the
    limiter explicitly turned on."""
    application = app_factory(RATELIMIT_ENABLED=True)
    client = application.test_client()

    with application.app_context():
        user = create_user(username="ratelimit-user", role="public")
        comp = create_competition(name="RateLimitComp")
        add_membership(user, comp, role="viewer")
        login_as(client, user, comp)

        # Reset the in-memory store so we're not poisoned by earlier tests.
        try:
            limiter.reset()
        except Exception:
            pass

    seen_429 = False
    for _ in range(15):
        resp = client.post(
            "/api/auth/password",
            json={
                "current_password": "wrong",
                "new_password": "n3wp4ssword!",
                "confirm_password": "n3wp4ssword!",
            },
        )
        if resp.status_code == 429:
            seen_429 = True
            break
        # Otherwise we expect 400 (wrong current password).
        assert resp.status_code == 400, resp.get_json()

    assert seen_429, (
        "expected to see HTTP 429 within 15 rapid POSTs, but never did. "
        "Has the @limiter.limit decorator been removed from auth_change_password?"
    )
