"""ProxyFix wires X-Forwarded-* headers into request.scheme/host so OAuth
redirect URIs and other _external=True URLs come out as the proxied
values instead of the internal http://web:5000.

Without ProxyFix configured, Google rejects the OAuth callback with a
redirect_uri mismatch. With ProxyFix configured but a future bad x_*
arg combination, the same regression returns silently — the only signal
is that login starts failing in production. Catching it in tests keeps
that from being the canary.
"""

from __future__ import annotations

from flask import request, url_for


def test_proxyfix_promotes_x_forwarded_proto_to_https(app_factory):
    """Behind Caddy, X-Forwarded-Proto: https should win over the WSGI
    server's "http". A url_for(..., _external=True) call must then emit
    https://, which is what Google OAuth requires for redirect_uri.

    We drop SERVER_NAME so Flask doesn't reject the proxied host as a
    host-mismatch — production doesn't set SERVER_NAME either, so this
    matches reality."""

    application = app_factory(SERVER_NAME=None)

    captured: dict[str, str] = {}

    @application.route("/__test_proxyfix__")
    def _proxyfix_view():
        captured["scheme"] = request.scheme
        captured["host"] = request.host
        # Build an external URL — this is what the OAuth callback uses.
        captured["external_self"] = url_for("_proxyfix_view", _external=True)
        return {"ok": True}

    client = application.test_client()
    response = client.get(
        "/__test_proxyfix__",
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "example.com",
            "X-Forwarded-For": "203.0.113.7",
        },
    )

    assert response.status_code == 200
    assert captured["scheme"] == "https"
    assert captured["host"].startswith("example.com")
    assert captured["external_self"].startswith("https://example.com")


def test_proxyfix_is_a_noop_without_forward_headers(app_factory):
    """Without proxy headers (e.g. dev runs hitting the app directly),
    ProxyFix must leave request.scheme alone."""

    application = app_factory(SERVER_NAME=None)

    captured: dict[str, str] = {}

    @application.route("/__test_proxyfix_noop__")
    def _proxyfix_noop_view():
        captured["scheme"] = request.scheme
        return {"ok": True}

    client = application.test_client()
    response = client.get("/__test_proxyfix_noop__")
    assert response.status_code == 200
    assert captured["scheme"] == "http"
