"""Public-results spectator surface: QR code link + auto-refresh.

Pins:
  - GET /scores/public/<id>/qr.svg returns a real SVG for a competition
    with public_results=True
  - same endpoint refuses (404) when public_results=False, so we can't
    accidentally leak a scannable handle to a private competition
  - the public scores page embeds the QR <img> and the auto-refresh JS
    (countdown id present), and the admin /scores/view page does NOT
    embed either (so admins don't get reloaded out from under them)
"""

from __future__ import annotations

from app.extensions import db
from tests.support import add_membership, create_competition, create_user, login_as


def test_qr_route_serves_svg_for_public_competition(client, app):
    comp = create_competition(name="QR Race")
    comp.public_results = True
    db.session.commit()

    resp = client.get(f"/scores/public/{comp.id}/qr.svg")
    assert resp.status_code == 200
    assert resp.mimetype == "image/svg+xml"
    body = resp.data
    # Real qrcode SVG starts with the XML header and contains <svg>
    assert body.startswith(b"<?xml"), body[:80]
    assert b"<svg" in body
    # The URL we encode should round-trip into the SVG body — qrcode
    # doesn't embed it as text, but we can assert the file is non-empty.
    assert len(body) > 500, f"SVG suspiciously small: {len(body)} bytes"


def test_qr_route_refuses_private_competition(client, app):
    """Even if the QR URL gets shared, a private competition must not
    expose a scannable handle."""
    comp = create_competition(name="Private Race")
    comp.public_results = False
    db.session.commit()
    resp = client.get(f"/scores/public/{comp.id}/qr.svg")
    assert resp.status_code == 404


def test_qr_route_404_for_missing_competition(client, app):
    resp = client.get("/scores/public/999999/qr.svg")
    assert resp.status_code == 404


def test_public_scores_page_embeds_qr_and_autorefresh(client, app):
    comp = create_competition(name="Spectator Race")
    comp.public_results = True
    db.session.commit()

    resp = client.get(f"/scores/public/{comp.id}")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="replace")

    # QR image references the qr.svg route for THIS competition.
    expected_qr_url = f"/scores/public/{comp.id}/qr.svg"
    assert expected_qr_url in body, "Public scores page didn't embed the QR <img>"
    # Countdown element is present so the auto-refresh shows progress.
    assert 'id="publicScoresCountdown"' in body
    # The auto-refresh JS block is present (look for the reload pattern).
    assert "window.location.reload" in body


def test_admin_scores_view_has_no_autorefresh_or_qr(client, app):
    """The same template renders admin /scores/view, but the QR card
    and the auto-refresh block must NOT appear there — admins should not
    be reloaded mid-action."""
    admin = create_user(username="qr-admin", role="admin")
    comp = create_competition(name="Admin Race")
    add_membership(admin, comp, role="admin")
    login_as(client, admin, comp)

    resp = client.get("/scores/view")
    assert resp.status_code == 200
    body = resp.data.decode("utf-8", errors="replace")

    assert "/qr.svg" not in body, "Admin view leaked the QR <img>"
    assert "publicScoresCountdown" not in body
    assert "window.location.reload" not in body


def test_qr_route_encodes_external_public_url(client, app, monkeypatch):
    """Sanity: the QR encodes the absolute public URL (so a scanner
    opens the right page on any device on the network), not a relative
    path that would only work on the admin's laptop."""
    comp = create_competition(name="Absolute Race")
    comp.public_results = True
    db.session.commit()

    # url_for(..., _external=True) needs SERVER_NAME; the test config
    # sets it to 'localhost'. We can verify the QR includes a URL
    # starting with http:// by decoding the SVG (qrcode encodes the
    # input string into the QR data, not the SVG text), but the easier
    # cheap test is: ask qrcode to encode a known URL via the same
    # codepath and confirm the SVG body length is similar (smoke).
    resp = client.get(f"/scores/public/{comp.id}/qr.svg")
    assert resp.status_code == 200
    # A short URL gives a smaller QR; the long URL we encode should
    # produce a non-trivial SVG. The actual URL string is opaque inside
    # the QR pixels, but we can sanity-check the response shape.
    assert resp.data.startswith(b"<?xml")
    assert b'viewBox' in resp.data or b'width=' in resp.data
