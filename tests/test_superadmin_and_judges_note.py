"""Tests for the superadmin console + checkpoints.judges_note.

Three guarantees pinned here:
  1. /superadmin/ is gated on User.role == 'superadmin'. Anonymous gets
     a login redirect; authenticated non-superadmins get 403.
  2. /superadmin/sheets-status.json returns a well-shaped snapshot even
     when no Sheets client has been initialized (no Sheets calls yet
     this process).
  3. The judges_note free-text round-trips through the checkpoint API
     PATCH and shows up alongside the read-only assigned_judges list.
"""

from __future__ import annotations

from app.extensions import db
from app.models import JudgeCheckpoint
from tests.support import (
    add_membership,
    create_checkpoint,
    create_competition,
    create_user,
    login_as,
)


def test_superadmin_index_redirects_anonymous(client, app):
    resp = client.get("/superadmin/", follow_redirects=False)
    # Login redirect (302 -> /login) - anonymous never reaches the page.
    assert resp.status_code in (301, 302)
    assert "/login" in (resp.headers.get("Location") or "")


def test_superadmin_index_forbids_non_superadmin(client, app):
    with app.app_context():
        admin = create_user(username="not-super", role="admin")
        comp = create_competition(name="Super Test")
        add_membership(admin, comp, role="admin")
        login_as(client, admin, comp)

        resp = client.get("/superadmin/")
        assert resp.status_code == 403, resp.data[:200]


def test_superadmin_index_renders_for_superadmin(client, app):
    with app.app_context():
        sa_user = create_user(username="super-test", role="superadmin")
        # Superadmins don't need a competition membership; the role check
        # is global.
        login_as(client, sa_user, None)

        resp = client.get("/superadmin/")
        assert resp.status_code == 200, resp.data[:200]
        # The table includes the superadmin user themselves.
        assert b"super-test" in resp.data


def test_sheets_status_endpoint_when_client_uninitialised(client, app):
    """Before any Sheets call, the singleton client may not exist yet.
    The status endpoint must report a zeroed snapshot, not 500."""
    with app.app_context():
        sa_user = create_user(username="super-status", role="superadmin")
        login_as(client, sa_user, None)
        # Ensure no client cached on this app.
        app.extensions.pop("sheets_client", None)

        resp = client.get("/superadmin/sheets-status.json")
        assert resp.status_code == 200, resp.data[:200]
        body = resp.get_json()
        assert body["used"] == 0
        assert body["limit"] == 40
        assert body["client_initialized"] is False
        assert body["remaining_seconds"] == 60.0


def test_sheets_status_endpoint_with_initialised_client(client, app, monkeypatch):
    """When the SheetsClient singleton exists, get_window_status() drives
    the snapshot. Forge a tiny stub client to avoid touching Google."""
    import threading

    class _Stub:
        def __init__(self):
            self._lock = threading.Lock()
            self._call_count = 7
            self._call_window_start = None  # not used, get_window_status overridden

        def get_window_status(self):
            return {
                "used": 7,
                "limit": 40,
                "window_seconds": 60,
                "elapsed_seconds": 12.3,
                "remaining_seconds": 47.7,
            }

    with app.app_context():
        sa_user = create_user(username="super-stub", role="superadmin")
        login_as(client, sa_user, None)
        app.extensions["sheets_client"] = _Stub()
        try:
            resp = client.get("/superadmin/sheets-status.json")
        finally:
            app.extensions.pop("sheets_client", None)

        assert resp.status_code == 200
        body = resp.get_json()
        assert body["used"] == 7
        assert body["client_initialized"] is True
        assert body["remaining_seconds"] == 47.7


def test_judges_note_round_trips_through_checkpoint_api(client, app):
    with app.app_context():
        admin = create_user(username="judges-note-admin", role="admin")
        comp = create_competition(name="Judges Note Race")
        add_membership(admin, comp, role="admin")
        cp = create_checkpoint(comp, name="CP-One")
        login_as(client, admin, comp)

        # PATCH with judges_note set.
        resp = client.patch(
            f"/api/checkpoints/{cp.id}",
            json={"judges_note": "Mike (no app login)\nAna (also no login)"},
        )
        assert resp.status_code == 200, resp.data[:300]

        # GET surfaces the value back.
        resp = client.get(f"/api/checkpoints/{cp.id}")
        body = resp.get_json()
        assert body["judges_note"] == "Mike (no app login)\nAna (also no login)"

        # PATCH again clearing the value -> back to None.
        resp = client.patch(
            f"/api/checkpoints/{cp.id}",
            json={"judges_note": ""},
        )
        assert resp.status_code == 200
        body = client.get(f"/api/checkpoints/{cp.id}").get_json()
        assert body["judges_note"] is None


def test_assigned_judges_list_shows_app_judges_alongside_note(client, app):
    """The /api/checkpoints/<id> serializer includes an assigned_judges
    list derived from JudgeCheckpoint, so the edit page can show both
    'app judges' badges and the free-text notes side by side."""
    with app.app_context():
        admin = create_user(username="cp-judges-admin", role="admin")
        judge = create_user(username="real-judge", role="public")
        comp = create_competition(name="Combined Judges Race")
        add_membership(admin, comp, role="admin")
        add_membership(judge, comp, role="judge")
        cp = create_checkpoint(comp, name="CP-Two")
        db.session.add(JudgeCheckpoint(user_id=judge.id, checkpoint_id=cp.id))
        db.session.commit()
        login_as(client, admin, comp)

        resp = client.get(f"/api/checkpoints/{cp.id}")
        body = resp.get_json()
        usernames = [j["username"] for j in body.get("assigned_judges", [])]
        assert "real-judge" in usernames, body
