"""Comprehensive tests for CSRF token enforcement.

Validates that the custom CSRF middleware in ``app/utils/csrf.py`` correctly
rejects mutating requests without a valid token, accepts requests that supply
one, and exempts the designated paths.
"""
from __future__ import annotations

import pytest

from app.extensions import db
from app.models import Checkpoint, Competition, Team, User
from tests.support import (
    add_membership,
    create_checkpoint,
    create_competition,
    create_group,
    create_team,
    create_user,
    login_as,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _csrf_app(app_factory, **overrides):
    """Return a CSRF-enabled application instance."""
    return app_factory(WTF_CSRF_ENABLED=True, **overrides)


def _seed_admin(app):
    """Create an admin user with a competition and return their integer IDs."""
    with app.app_context():
        admin = create_user(username="csrf-admin")
        competition = create_competition(name="CSRF Competition")
        add_membership(admin, competition, role="admin")
        return admin.id, competition.id


def _login_and_get_token(app, client, admin_id, competition_id, *, base_url: str | None = None, seed_path: str = "/teams/"):
    """Log in, seed the session (GET), and return the CSRF token."""
    session_kwargs = {"base_url": base_url} if base_url else {}
    with client.session_transaction(**session_kwargs) as sess:
        sess["_user_id"] = str(admin_id)
        sess["_fresh"] = True
        sess["competition_id"] = competition_id
    # A GET request seeds the CSRF token in the session.
    request_kwargs = {"base_url": base_url} if base_url else {}
    seed = client.get(seed_path, **request_kwargs)
    assert seed.status_code == 200
    with client.session_transaction(**session_kwargs) as sess:
        return sess["_csrf_token"]


# ---------------------------------------------------------------------------
# TestCsrfFormProtection
# ---------------------------------------------------------------------------

class TestCsrfFormProtection:
    """POST to HTML form endpoints must include a valid csrf_token."""

    def test_team_create_requires_csrf(self, app_factory):
        app = _csrf_app(app_factory)
        client = app.test_client()
        admin_id, competition_id = _seed_admin(app)
        token = _login_and_get_token(app, client, admin_id, competition_id)

        denied = client.post("/teams/add", data={"name": "Test"})
        assert denied.status_code == 400

        allowed = client.post(
            "/teams/add",
            data={"name": "Test", "csrf_token": token},
            follow_redirects=False,
        )
        assert allowed.status_code in (200, 302)

    def test_team_delete_requires_csrf(self, app_factory):
        app = _csrf_app(app_factory)
        client = app.test_client()
        with app.app_context():
            admin = create_user(username="csrf-del-admin")
            competition = create_competition(name="CSRF Del Race")
            add_membership(admin, competition, role="admin")
            team = create_team(competition, name="DeleteMe")
            admin_id = admin.id
            competition_id = competition.id
            team_id = team.id

        token = _login_and_get_token(app, client, admin_id, competition_id)

        denied = client.post(f"/teams/{team_id}/delete")
        assert denied.status_code == 400

        allowed = client.post(
            f"/teams/{team_id}/delete",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert allowed.status_code in (200, 302)

    def test_checkpoint_create_requires_csrf(self, app_factory):
        app = _csrf_app(app_factory)
        client = app.test_client()
        admin_id, competition_id = _seed_admin(app)
        token = _login_and_get_token(app, client, admin_id, competition_id)

        denied = client.post("/checkpoints/add", data={"name": "CP1"})
        assert denied.status_code == 400

        allowed = client.post(
            "/checkpoints/add",
            data={"name": "CP1", "csrf_token": token},
            follow_redirects=False,
        )
        assert allowed.status_code in (200, 302)

    def test_team_create_html_flow_persists_record_on_non_localhost_host(self, app_factory):
        app = _csrf_app(app_factory, SERVER_NAME=None)
        client = app.test_client()
        admin_id, competition_id = _seed_admin(app)
        base_url = "http://example.test"
        token = _login_and_get_token(
            app,
            client,
            admin_id,
            competition_id,
            base_url=base_url,
            seed_path="/teams/add",
        )

        allowed = client.post(
            "/teams/add",
            base_url=base_url,
            data={"name": "Browser Team", "csrf_token": token},
            follow_redirects=False,
        )

        assert allowed.status_code == 302
        with app.app_context():
            team = Team.query.filter_by(name="Browser Team").first()
            assert team is not None

    def test_checkpoint_create_html_flow_persists_record_on_non_localhost_host(self, app_factory):
        app = _csrf_app(app_factory, SERVER_NAME=None)
        client = app.test_client()
        admin_id, competition_id = _seed_admin(app)
        base_url = "http://example.test"
        token = _login_and_get_token(
            app,
            client,
            admin_id,
            competition_id,
            base_url=base_url,
            seed_path="/checkpoints/add",
        )

        allowed = client.post(
            "/checkpoints/add",
            base_url=base_url,
            data={"name": "Browser CP", "csrf_token": token},
            follow_redirects=False,
        )

        assert allowed.status_code == 302
        with app.app_context():
            checkpoint = Checkpoint.query.filter_by(name="Browser CP").first()
            assert checkpoint is not None

    def test_group_delete_requires_csrf(self, app_factory):
        app = _csrf_app(app_factory)
        client = app.test_client()
        with app.app_context():
            admin = create_user(username="csrf-grp-admin")
            competition = create_competition(name="CSRF Grp Race")
            add_membership(admin, competition, role="admin")
            group = create_group(competition, name="DeleteGroup")
            admin_id = admin.id
            competition_id = competition.id
            group_id = group.id

        token = _login_and_get_token(app, client, admin_id, competition_id)

        denied = client.post(f"/groups/{group_id}/delete")
        assert denied.status_code == 400

        allowed = client.post(
            f"/groups/{group_id}/delete",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert allowed.status_code in (200, 302)

    def test_logout_requires_csrf(self, app_factory):
        app = _csrf_app(app_factory)
        client = app.test_client()
        admin_id, competition_id = _seed_admin(app)
        token = _login_and_get_token(app, client, admin_id, competition_id)

        denied = client.post("/logout")
        assert denied.status_code == 400

        allowed = client.post(
            "/logout",
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert allowed.status_code == 302

    def test_change_password_requires_csrf(self, app_factory):
        app = _csrf_app(app_factory)
        client = app.test_client()
        admin_id, competition_id = _seed_admin(app)
        token = _login_and_get_token(app, client, admin_id, competition_id)

        form_data = {
            "current_password": "password123",
            "new_password": "NewSecure!456",
            "confirm_password": "NewSecure!456",
        }

        denied = client.post("/change_password", data=form_data)
        assert denied.status_code == 400

        form_data["csrf_token"] = token
        allowed = client.post(
            "/change_password",
            data=form_data,
            follow_redirects=False,
        )
        assert allowed.status_code in (200, 302)

    def test_lora_add_requires_csrf(self, app_factory):
        app = _csrf_app(app_factory)
        client = app.test_client()
        admin_id, competition_id = _seed_admin(app)
        token = _login_and_get_token(app, client, admin_id, competition_id)

        form_data = {"dev_num": "1", "name": "Dev1"}

        denied = client.post("/lora/add", data=form_data)
        assert denied.status_code == 400

        form_data["csrf_token"] = token
        allowed = client.post(
            "/lora/add",
            data=form_data,
            follow_redirects=False,
        )
        assert allowed.status_code in (200, 302)


# ---------------------------------------------------------------------------
# TestCsrfApiProtection
# ---------------------------------------------------------------------------

class TestCsrfApiProtection:
    """JSON API endpoints must include X-CSRF-Token header."""

    def test_api_team_create_requires_csrf(self, app_factory):
        app = _csrf_app(app_factory)
        client = app.test_client()
        admin_id, competition_id = _seed_admin(app)
        token = _login_and_get_token(app, client, admin_id, competition_id)

        denied = client.post("/api/teams", json={"name": "T"})
        assert denied.status_code == 400
        assert denied.get_json()["detail"] == "CSRF token missing or invalid."

        allowed = client.post(
            "/api/teams",
            json={"name": "T"},
            headers={"X-CSRF-Token": token},
        )
        assert allowed.status_code == 201

    def test_api_checkin_requires_csrf(self, app_factory):
        app = _csrf_app(app_factory)
        client = app.test_client()
        with app.app_context():
            admin = create_user(username="csrf-checkin-admin")
            competition = create_competition(name="CSRF Checkin Race")
            add_membership(admin, competition, role="admin")
            team = create_team(competition, name="CSRF Team", number=7)
            checkpoint = create_checkpoint(competition, name="CSRF CP")
            admin_id = admin.id
            competition_id = competition.id
            team_id = team.id
            checkpoint_id = checkpoint.id

        token = _login_and_get_token(app, client, admin_id, competition_id)

        payload = {"team_id": team_id, "checkpoint_id": checkpoint_id}

        denied = client.post("/api/checkins", json=payload)
        assert denied.status_code == 400
        assert denied.get_json()["detail"] == "CSRF token missing or invalid."

        allowed = client.post(
            "/api/checkins",
            json=payload,
            headers={"X-CSRF-Token": token},
        )
        assert allowed.status_code == 201


# ---------------------------------------------------------------------------
# TestCsrfExemptPaths
# ---------------------------------------------------------------------------

class TestCsrfExemptPaths:
    """Paths listed in ``_EXEMPT_PATHS`` must not be rejected for CSRF."""

    def test_ingest_exempt_from_csrf(self, app_factory):
        app = _csrf_app(app_factory)
        client = app.test_client()

        resp = client.post(
            "/api/ingest",
            json={"some": "payload"},
            headers={"Content-Type": "application/json"},
        )
        # The endpoint may fail for other reasons (e.g. auth, bad payload),
        # but it must NOT fail with 400 + CSRF message.
        if resp.status_code == 400:
            body = resp.get_json(silent=True) or {}
            assert body.get("detail") != "CSRF token missing or invalid."

    def test_login_api_exempt_from_csrf(self, app_factory):
        app = _csrf_app(app_factory)
        client = app.test_client()

        resp = client.post(
            "/api/auth/login",
            json={"username": "nobody", "password": "wrong"},
        )
        # Should not be rejected for CSRF -- may be 401/403 for bad creds.
        if resp.status_code == 400:
            body = resp.get_json(silent=True) or {}
            assert body.get("detail") != "CSRF token missing or invalid."


# ---------------------------------------------------------------------------
# TestCsrfTokenInRenderedForms
# ---------------------------------------------------------------------------

class TestCsrfTokenInRenderedForms:
    """Rendered HTML pages must contain a hidden csrf_token input."""

    def test_add_team_form_contains_csrf_input(self, app_factory):
        app = app_factory()
        client = app.test_client()
        with app.app_context():
            admin = create_user(username="csrf-html-admin")
            competition = create_competition(name="CSRF HTML Race")
            add_membership(admin, competition, role="admin")
            admin_id = admin.id
            competition_id = competition.id

        with app.app_context():
            login_as(
                client,
                db.session.get(User, admin_id),
                db.session.get(Competition, competition_id),
            )

        resp = client.get("/teams/add")
        assert resp.status_code == 200
        assert b'name="csrf_token"' in resp.data

    def test_login_form_contains_csrf_input(self, app_factory):
        app = app_factory()
        client = app.test_client()

        resp = client.get("/login")
        assert resp.status_code == 200
        assert b'name="csrf_token"' in resp.data

    def test_base_template_logout_form_contains_csrf_input(self, app_factory):
        app = app_factory()
        client = app.test_client()
        with app.app_context():
            admin = create_user(username="csrf-base-admin")
            competition = create_competition(name="CSRF Base Race")
            add_membership(admin, competition, role="admin")
            admin_id = admin.id
            competition_id = competition.id

        with app.app_context():
            login_as(
                client,
                db.session.get(User, admin_id),
                db.session.get(Competition, competition_id),
            )

        resp = client.get("/teams/")
        assert resp.status_code == 200
        assert b'name="csrf_token"' in resp.data
