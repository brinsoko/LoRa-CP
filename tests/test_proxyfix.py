"""ProxyFix wires X-Forwarded-* headers into request.scheme/host so OAuth
redirect URIs and other _external=True URLs come out as the proxied
values instead of the internal http://web:5000.

Gated behind TRUST_PROXY_HEADERS — defaults on in production, off
otherwise. Without an actual reverse proxy in front, ProxyFix would let
clients spoof X-Forwarded-Host / X-Forwarded-Proto, so the flag must
NOT be on when Flask is reachable directly.

Three scenarios covered:
- Flag on + headers present  -> trusted (production behavior).
- Flag on + no headers       -> no-op.
- Flag off + headers present -> headers ignored (defense in depth).
"""

from __future__ import annotations

from flask import request, url_for


def test_proxyfix_promotes_x_forwarded_proto_to_https(app_factory):
    """Behind Caddy with TRUST_PROXY_HEADERS=True, X-Forwarded-Proto: https
    should win over the WSGI server's "http". A url_for(..., _external=True)
    call must then emit https://, which is what Google OAuth requires
    for redirect_uri.

    We drop SERVER_NAME so Flask doesn't reject the proxied host as a
    host-mismatch — production doesn't set SERVER_NAME either."""

    application = app_factory(SERVER_NAME=None, TRUST_PROXY_HEADERS=True)

    captured: dict[str, str] = {}

    @application.route("/__test_proxyfix__")
    def _proxyfix_view():
        captured["scheme"] = request.scheme
        captured["host"] = request.host
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
    """With the flag on but no proxy headers present, ProxyFix is a
    no-op."""

    application = app_factory(SERVER_NAME=None, TRUST_PROXY_HEADERS=True)

    captured: dict[str, str] = {}

    @application.route("/__test_proxyfix_noop__")
    def _proxyfix_noop_view():
        captured["scheme"] = request.scheme
        return {"ok": True}

    client = application.test_client()
    response = client.get("/__test_proxyfix_noop__")
    assert response.status_code == 200
    assert captured["scheme"] == "http"


def test_proxy_headers_ignored_when_trust_disabled(app_factory):
    """Defense in depth: when TRUST_PROXY_HEADERS is False (the default
    outside production), spoofed X-Forwarded-* headers must NOT be
    promoted into request.scheme/host. Otherwise a misconfigured deploy
    that exposes Flask directly would let clients pretend to be
    https://victim.example.com."""

    application = app_factory(SERVER_NAME=None, TRUST_PROXY_HEADERS=False)

    captured: dict[str, str] = {}

    @application.route("/__test_proxyfix_off__")
    def _proxyfix_off_view():
        captured["scheme"] = request.scheme
        captured["host"] = request.host
        return {"ok": True}

    client = application.test_client()
    response = client.get(
        "/__test_proxyfix_off__",
        headers={
            "X-Forwarded-Proto": "https",
            "X-Forwarded-Host": "evil.example.com",
        },
    )
    assert response.status_code == 200
    assert captured["scheme"] == "http"
    assert "evil.example.com" not in captured["host"]
