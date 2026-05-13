"""Regression tests for the HTML routes that previously had no auth
decorator and silently rendered empty pages (or, in the CSV case,
downloaded a JSON-formatted "csv" containing the API's 401 body)
when hit unauthenticated.

The underlying JSON API was always gated, so no data could leak.
This test locks in the @login_required redirect behavior so the
UX is correct."""

from __future__ import annotations


def _is_login_redirect(resp) -> bool:
    """Flask-Login emits 302 to the configured login view with the
    original URL as ?next=. Accept any of the redirect family."""
    if resp.status_code not in (301, 302, 303, 307, 308):
        return False
    location = resp.headers.get("Location", "")
    # Either the auth.login endpoint name or a path containing /login.
    return "/login" in location or "auth.login" in location


def test_checkins_list_redirects_anonymous_user(client):
    """GET /checkins/ (the HTML view) without a session must redirect
    to login, not render an empty list with a misleading flash."""
    resp = client.get("/checkins/", follow_redirects=False)
    assert _is_login_redirect(resp), (
        f"expected a redirect to login, got {resp.status_code} "
        f"to {resp.headers.get('Location')!r}"
    )


def test_checkins_csv_redirects_anonymous_user(client):
    """GET /checkins/export.csv without a session must redirect.
    Previously this returned a file download whose body was the API's
    JSON 401 response."""
    resp = client.get("/checkins/export.csv", follow_redirects=False)
    assert _is_login_redirect(resp), (
        f"expected a redirect to login, got {resp.status_code}"
    )


def test_main_checkins_csv_redirects_anonymous_user(client):
    """The legacy /checkins.csv path on main_bp had the same
    no-auth-decorator gap."""
    resp = client.get("/checkins.csv", follow_redirects=False)
    assert _is_login_redirect(resp), (
        f"expected a redirect to login, got {resp.status_code}"
    )


def test_main_view_checkins_redirects_anonymous_user(client):
    """GET /checkins (main blueprint HTML view) had no decorator
    either — even though it tries to look up the competition, the
    auth gate has to be first."""
    resp = client.get("/checkins", follow_redirects=False)
    assert _is_login_redirect(resp), (
        f"expected a redirect to login, got {resp.status_code}"
    )
