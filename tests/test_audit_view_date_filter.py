"""Regression test for the HTML audit view's bad-date filter handling.

The API audit ("Pass 5") noted that the audit-events page's date
filter handling wasn't covered by tests. After the audit pass made
_parse_date_range raise on malformed input, the HTML view at
/audit/ catches the ValueError, flashes a warning, and continues
with unfiltered results — but nothing exercised that path.

This test fills the gap."""

from __future__ import annotations

from tests.support import (
    add_membership,
    create_competition,
    create_user,
    login_as,
)


def test_audit_view_with_bad_date_flashes_and_renders(client):
    """A typo in ?date_from must not crash the page — the route
    catches ValueError, flashes a warning, and renders the unfiltered
    list. The response should be HTTP 200 with the warning visible
    in the rendered HTML."""
    admin = create_user(username="audit-bad-date-admin", role="public")
    comp = create_competition(name="AuditDateComp")
    add_membership(admin, comp, role="admin")
    login_as(client, admin, comp)

    resp = client.get("/audit/?date_from=banana&date_to=2026-01-01")
    assert resp.status_code == 200, resp.data[:500]
    # The flashed warning should be rendered into the page.
    body = resp.get_data(as_text=True)
    assert (
        "Invalid date filter" in body
        or "Neveljaven filter datuma" in body  # sl translation
    ), "expected the bad-date warning to be flashed into the page"


def test_audit_view_with_valid_date_returns_200(client):
    """Sanity check: a valid date filter still works."""
    admin = create_user(username="audit-good-date-admin", role="public")
    comp = create_competition(name="AuditDateGoodComp")
    add_membership(admin, comp, role="admin")
    login_as(client, admin, comp)

    resp = client.get("/audit/?date_from=2026-01-01&date_to=2026-12-31")
    assert resp.status_code == 200


def test_audit_view_with_bad_date_to_does_not_crash(client):
    """Malformed date_to also gets the same warning-and-continue
    treatment."""
    admin = create_user(username="audit-bad-dt-admin", role="public")
    comp = create_competition(name="AuditDateDtComp")
    add_membership(admin, comp, role="admin")
    login_as(client, admin, comp)

    resp = client.get("/audit/?date_to=2026-13-99")
    assert resp.status_code == 200
