"""Verify the login endpoints reject after the configured rate limit."""

from __future__ import annotations


def test_api_login_rate_limited_after_threshold(app_factory):
    application = app_factory(RATELIMIT_ENABLED=True)
    client = application.test_client()

    statuses = []
    for _ in range(15):
        response = client.post(
            "/api/auth/login",
            json={"username": "ghost", "password": "wrong"},
        )
        statuses.append(response.status_code)

    # Limit is 10/minute on /api/auth/login. The 11th call onward must 429.
    assert statuses[:10] == [401] * 10
    assert any(code == 429 for code in statuses[10:])


def test_html_login_rate_limited_after_threshold(app_factory):
    application = app_factory(RATELIMIT_ENABLED=True)
    client = application.test_client()

    statuses = []
    for _ in range(15):
        response = client.post(
            "/login",
            data={"username": "ghost", "password": "wrong"},
            follow_redirects=False,
        )
        statuses.append(response.status_code)

    assert any(code == 429 for code in statuses[10:])
