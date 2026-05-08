"""Verify MAX_CONTENT_LENGTH and the 413 handler.

We override the cap to a tiny size in a sub-app so we don't have to ship
a 32MB body just to trigger the limit.
"""

from __future__ import annotations


def test_oversized_request_returns_413_json_envelope(app_factory):
    application = app_factory(MAX_CONTENT_LENGTH=1024)
    client = application.test_client()

    # Body well over the 1 KiB cap. Use any POST endpoint — the cap is
    # enforced by Werkzeug before the handler runs.
    big_body = "a" * 4096
    response = client.post(
        "/api/auth/login",
        data=big_body,
        content_type="application/json",
    )

    assert response.status_code == 413
    body = response.get_json()
    assert body["error"] == "payload_too_large"
    assert body["code"] == 413
