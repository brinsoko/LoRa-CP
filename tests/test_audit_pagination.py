"""Test suite 5: Audit pagination."""
from __future__ import annotations

import pytest

from app.extensions import db
from app.models import AuditEvent
from tests.support import (
    add_membership,
    create_competition,
    create_user,
    login_as,
)


@pytest.fixture
def _seeded(app, client):
    user = create_user(username="audit-admin", role="admin")
    comp = create_competition(name="Audit Race")
    add_membership(user, comp, role="admin")
    login_as(client, user, comp)
    return comp, user


def _insert_audit_events(comp_id: int, count: int):
    """Insert N audit events directly into the database."""
    for i in range(count):
        db.session.add(AuditEvent(
            competition_id=comp_id,
            event_type="test_event",
            entity_type="test",
            entity_id=i,
            actor_type="system",
            actor_label="test-system",
            summary=f"Test event {i}",
        ))
    db.session.commit()


def _count_table_rows(html: str, marker: str = "test_event") -> int:
    """Count occurrences of marker inside <td><code>...</code></td> table cells only."""
    return html.count(f"<code>{marker}</code>")


class TestAuditPagination:
    def test_audit_default_page_size_50(self, client, _seeded):
        comp, _ = _seeded
        _insert_audit_events(comp.id, 75)

        resp = client.get("/audit/")
        assert resp.status_code == 200
        row_count = _count_table_rows(resp.get_data(as_text=True))
        assert row_count == 50, f"Expected 50 events on page 1, got {row_count}"

    def test_audit_page_2(self, client, _seeded):
        comp, _ = _seeded
        _insert_audit_events(comp.id, 75)

        resp = client.get("/audit/?page=2")
        assert resp.status_code == 200
        row_count = _count_table_rows(resp.get_data(as_text=True))
        assert row_count == 25, f"Expected 25 events on page 2, got {row_count}"

    def test_audit_page_1_default(self, client, _seeded):
        comp, _ = _seeded
        _insert_audit_events(comp.id, 60)

        resp_no_param = client.get("/audit/")
        resp_page_1 = client.get("/audit/?page=1")
        assert resp_no_param.status_code == 200
        assert resp_page_1.status_code == 200
        count_no = _count_table_rows(resp_no_param.get_data(as_text=True))
        count_p1 = _count_table_rows(resp_page_1.get_data(as_text=True))
        assert count_no == count_p1 == 50

    def test_audit_invalid_page_zero(self, client, _seeded):
        comp, _ = _seeded
        _insert_audit_events(comp.id, 10)

        resp = client.get("/audit/?page=0")
        assert resp.status_code == 200

    def test_audit_invalid_page_negative(self, client, _seeded):
        comp, _ = _seeded
        _insert_audit_events(comp.id, 10)

        resp = client.get("/audit/?page=-1")
        assert resp.status_code == 200

    def test_audit_page_beyond_last(self, client, _seeded):
        comp, _ = _seeded
        _insert_audit_events(comp.id, 10)
        # Only 1 page (10 items, 50 per page)
        resp = client.get("/audit/?page=999")
        assert resp.status_code == 200
        row_count = _count_table_rows(resp.get_data(as_text=True))
        assert row_count == 0

    def test_audit_pagination_metadata_in_response(self, client, _seeded):
        comp, _ = _seeded
        _insert_audit_events(comp.id, 75)

        resp = client.get("/audit/")
        html = resp.get_data(as_text=True)
        assert "1 / 2" in html or "Page 1" in html or "1/2" in html
