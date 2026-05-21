"""Tests for the superadmin console + checkpoints.judges_note.

Guarantees pinned here:
  1. /superadmin/ is gated on User.role == 'superadmin'. Anonymous gets
     a login redirect; authenticated non-superadmins get 403.
  2. /superadmin/sheets-status.json returns a well-shaped snapshot even
     when no Sheets client has been initialized (no Sheets calls yet
     this process).
  3. /superadmin/users/bulk-add creates users with generated passwords,
     skips dupes/invalids, and is gated on superadmin.
  4. /superadmin/users/<id>/delete hard-deletes a user, refuses self-
     delete, and is gated on superadmin.
  5. The judges_note free-text round-trips through the checkpoint API
     PATCH and shows up alongside the read-only assigned_judges list.
"""

from __future__ import annotations

from app.extensions import db
from app.models import CompetitionMember, JudgeCheckpoint, User
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


def test_bulk_add_users_creates_with_generated_passwords(client, app):
    """Bulk-add: valid usernames become real, loginable users."""
    with app.app_context():
        sa_user = create_user(username="super-bulk", role="superadmin")
        login_as(client, sa_user, None)

        resp = client.post(
            "/superadmin/users/bulk-add",
            data={
                "usernames": "alpha\nbravo\ncharlie\n",
                "role": "public",
            },
        )
        assert resp.status_code == 200, resp.data[:300]
        # Results page lists every created username.
        for name in ("alpha", "bravo", "charlie"):
            assert name.encode() in resp.data

        # All three exist in the DB with role=public and a usable password.
        for name in ("alpha", "bravo", "charlie"):
            u = User.query.filter_by(username=name).first()
            assert u is not None, f"{name} missing"
            assert u.role == "public"
            assert u.password_hash, "password should be set"


def test_bulk_add_users_skips_dupes_and_invalid_and_existing(client, app):
    with app.app_context():
        sa_user = create_user(username="super-bulk-skip", role="superadmin")
        # Pre-existing user with one of the requested usernames.
        existing = create_user(username="already-there", role="public")
        login_as(client, sa_user, None)

        resp = client.post(
            "/superadmin/users/bulk-add",
            data={
                # already-there: exists; dupe-row: appears twice; bad!name:
                # invalid chars; fresh1/fresh2: should be created.
                "usernames": "already-there\nfresh1\ndupe-row\ndupe-row\nbad!name\nfresh2",
                "role": "public",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 200, resp.data[:300]
        # Only the two fresh names land in the DB.
        assert User.query.filter_by(username="fresh1").first() is not None
        assert User.query.filter_by(username="fresh2").first() is not None
        # dupe-row appears once (the first occurrence), not twice.
        assert User.query.filter_by(username="dupe-row").count() == 1
        # bad!name never lands.
        assert User.query.filter_by(username="bad!name").first() is None
        # Existing user untouched (same id, same hash).
        still = User.query.filter_by(username="already-there").first()
        assert still is not None and still.id == existing.id


def test_bulk_add_users_requires_superadmin(client, app):
    """Per-competition admins are not allowed to use this console."""
    with app.app_context():
        admin = create_user(username="bulk-not-super", role="admin")
        comp = create_competition(name="Bulk Gate")
        add_membership(admin, comp, role="admin")
        login_as(client, admin, comp)

        resp = client.post(
            "/superadmin/users/bulk-add",
            data={"usernames": "shouldnt-land", "role": "public"},
        )
        assert resp.status_code == 403
        assert User.query.filter_by(username="shouldnt-land").first() is None


def test_delete_user_removes_row_and_cascades_memberships(client, app):
    with app.app_context():
        sa_user = create_user(username="super-del", role="superadmin")
        victim = create_user(username="victim-1", role="public")
        comp = create_competition(name="Del Cascade")
        add_membership(victim, comp, role="judge")
        victim_id = victim.id

        login_as(client, sa_user, None)
        resp = client.post(
            f"/superadmin/users/{victim_id}/delete",
            follow_redirects=False,
        )
        assert resp.status_code in (301, 302)

        assert db.session.get(User, victim_id) is None
        assert (
            CompetitionMember.query.filter_by(user_id=victim_id).count() == 0
        ), "memberships should cascade"


def test_delete_user_blocks_self_delete(client, app):
    with app.app_context():
        sa_user = create_user(username="super-self", role="superadmin")
        sa_id = sa_user.id
        login_as(client, sa_user, None)

        resp = client.post(
            f"/superadmin/users/{sa_id}/delete",
            follow_redirects=False,
        )
        # Redirect back to the console with a flash, not a hard error.
        assert resp.status_code in (301, 302)
        # User still exists.
        assert db.session.get(User, sa_id) is not None


def test_delete_user_requires_superadmin(client, app):
    with app.app_context():
        admin = create_user(username="del-not-super", role="admin")
        target = create_user(username="del-target", role="public")
        comp = create_competition(name="Del Gate")
        add_membership(admin, comp, role="admin")
        login_as(client, admin, comp)

        resp = client.post(f"/superadmin/users/{target.id}/delete")
        assert resp.status_code == 403
        assert db.session.get(User, target.id) is not None


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
