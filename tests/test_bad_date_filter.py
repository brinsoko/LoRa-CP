"""Regression tests for malformed date filters on check-in endpoints.

Before: the endpoints silently swallowed the parse error and ran the
query with no date filter — a typo could expand the result set without
any indication. After: the API endpoints return 400 invalid_request and
the HTML CSV export returns 400 too."""

from __future__ import annotations

from tests.support import (
    add_membership,
    create_competition,
    create_user,
    login_as,
)


def test_api_checkins_rejects_bad_date_from(client):
    user = create_user(username="bad-df", role="public")
    comp = create_competition(name="DateComp")
    add_membership(user, comp, role="admin")
    login_as(client, user, comp)

    resp = client.get("/api/checkins?date_from=banana")
    assert resp.status_code == 400
    body = resp.get_json() or {}
    assert body.get("error") == "invalid_request"


def test_api_checkins_export_rejects_bad_date_to(client):
    user = create_user(username="bad-dt-exp", role="public")
    comp = create_competition(name="DateExpComp")
    add_membership(user, comp, role="admin")
    login_as(client, user, comp)

    resp = client.get("/api/checkins/export.csv?date_to=2026-13-99")
    assert resp.status_code == 400
    body = resp.get_json() or {}
    assert body.get("error") == "invalid_request"


def test_api_checkins_accepts_valid_dates(client):
    """Sanity check: valid dates still work after the strict-parse change."""
    user = create_user(username="good-d", role="public")
    comp = create_competition(name="DateGoodComp")
    add_membership(user, comp, role="admin")
    login_as(client, user, comp)

    resp = client.get("/api/checkins?date_from=2026-01-01&date_to=2026-12-31")
    assert resp.status_code == 200
