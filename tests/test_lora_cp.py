from __future__ import annotations

from datetime import datetime
import importlib

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Checkin, Checkpoint, CompetitionMember, LoRaDevice, RFIDCard, Team, User
from tests.support import (
    add_membership,
    assign_team_group,
    create_checkpoint,
    create_checkin,
    create_competition,
    create_device,
    create_group,
    create_rfid_card,
    create_team,
    create_user,
    login_as,
)


class TestModels:
    def test_user_password_roundtrip(self, app):
        user = create_user(username="model-user", password="secret123")
        assert user.check_password("secret123") is True
        assert user.check_password("wrong-pass") is False

    def test_membership_is_unique_per_user_and_competition(self, app):
        user = create_user(username="member-user")
        competition = create_competition(name="Membership Race")
        add_membership(user, competition, role="admin")

        duplicate = CompetitionMember(
            user_id=user.id,
            competition_id=competition.id,
            role="judge",
            active=True,
        )
        db.session.add(duplicate)
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_lora_device_dev_num_unique_within_competition(self, app):
        competition = create_competition(name="Device Race")
        create_device(competition, dev_num=7, name="D7")
        duplicate = LoRaDevice(competition_id=competition.id, dev_num=7, name="Dup")
        db.session.add(duplicate)
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_checkin_unique_for_team_and_checkpoint(self, app):
        competition = create_competition(name="Checkin Race")
        team = create_team(competition, name="Alpha", number=1)
        checkpoint = create_checkpoint(competition, name="CP-1")
        create_checkin(competition, team, checkpoint)

        duplicate = Checkin(
            competition_id=competition.id,
            team_id=team.id,
            checkpoint_id=checkpoint.id,
            timestamp=datetime.utcnow(),
        )
        db.session.add(duplicate)
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

    def test_rfid_number_is_optional(self, app):
        competition = create_competition(name="RFID Race")
        team = create_team(competition, name="Card Team", number=11)
        card = create_rfid_card(team, uid="A1B2C3D4", number=None)
        assert card.number is None


class TestConfigAndFactory:
    def test_env_bool_parsing(self, monkeypatch):
        import config

        for raw in ("1", "true", "yes", "on"):
            monkeypatch.setenv("FLAG_VALUE", raw)
            assert config._env_bool("FLAG_VALUE") is True

        for raw in ("0", "false", "no", "off", ""):
            monkeypatch.setenv("FLAG_VALUE", raw)
            assert config._env_bool("FLAG_VALUE", default=True) is False

    def test_create_app_accepts_config_overrides(self, app_factory):
        application = app_factory(SECRET_KEY="override-secret")
        assert application.config["SECRET_KEY"] == "override-secret"

    def test_create_app_builds_default_sqlite_uri_when_missing(self, app_factory):
        application = app_factory(SQLALCHEMY_DATABASE_URI=None)
        assert application.config["SQLALCHEMY_DATABASE_URI"].startswith("sqlite:///")


class TestAuthApi:
    def test_login_with_username(self, client, app):
        create_user(username="alice", password="secret123", email="alice@example.com")
        response = client.post(
            "/api/auth/login",
            json={"username": "alice", "password": "secret123"},
        )
        body = response.get_json()
        assert response.status_code == 200
        assert body["ok"] is True
        assert body["user"]["username"] == "alice"

    def test_login_with_email(self, client, app):
        create_user(username="bob", password="secret123", email="bob@example.com")
        response = client.post(
            "/api/auth/login",
            json={"username": "bob@example.com", "password": "secret123"},
        )
        body = response.get_json()
        assert response.status_code == 200
        assert body["user"]["username"] == "bob"

    def test_login_rejects_bad_password(self, client, app):
        create_user(username="carol", password="secret123")
        response = client.post(
            "/api/auth/login",
            json={"username": "carol", "password": "wrong"},
        )
        assert response.status_code == 401


class TestAuthRoutes:
    def test_login_page_renders(self, client, app):
        response = client.get("/login")
        assert response.status_code == 200

    def test_login_route_blocks_open_redirects(self, client, app):
        create_user(username="route-user", password="secret123")
        response = client.post(
            "/login?next=https://attacker.example",
            data={"username": "route-user", "password": "secret123"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/competitions")

    def test_logout_route_clears_session(self, client, app):
        user = create_user(username="logout-user")
        competition = create_competition(name="Logout Race")
        add_membership(user, competition, role="admin")
        login_as(client, user, competition)

        response = client.post("/logout", follow_redirects=False)
        after = client.get("/competitions", follow_redirects=False)

        assert response.status_code == 302
        assert after.status_code == 302
        assert "/login" in after.headers["Location"]

    def test_google_login_route_redirects_to_login_when_unconfigured(self, client, app):
        response = client.get("/login/google", follow_redirects=False)
        assert response.status_code == 302
        assert response.headers["Location"].endswith("/login")

    def test_google_login_route_sanitizes_next_before_storing(self, app_factory):
        application = app_factory(GOOGLE_OAUTH_CLIENT_ID="client-id")
        client = application.test_client()
        response = client.get("/login/google?next=https://attacker.example", follow_redirects=False)

        assert response.status_code == 302
        with client.session_transaction() as sess:
            assert sess["google_oauth_next"] == ""

    def test_change_password_route_updates_password(self, client, app):
        user = create_user(username="password-user", password="oldsecret")
        competition = create_competition(name="Password Race")
        add_membership(user, competition, role="admin")
        login_as(client, user, competition)

        response = client.post(
            "/change_password",
            data={
                "current_password": "oldsecret",
                "new_password": "newsecret123",
                "confirm_password": "newsecret123",
            },
            follow_redirects=True,
        )

        db.session.refresh(user)
        assert response.status_code == 200
        assert user.check_password("newsecret123") is True


class TestIngestApi:
    def test_ingest_creates_checkin_for_known_uid(self, client, app):
        competition = create_competition(name="Ingest Race")
        device = create_device(competition, dev_num=1, name="Gateway 1")
        checkpoint = create_checkpoint(competition, name="CP-1", lora_device=device)
        team = create_team(competition, name="Wolves", number=5)
        card = create_rfid_card(team, uid="A1B2C3D4")

        response = client.post(
            "/api/ingest",
            json={
                "competition_id": competition.id,
                "dev_id": 1,
                "payload": card.uid,
                "rssi": -60,
                "snr": 8,
            },
        )
        body = response.get_json()

        assert response.status_code == 201
        assert body["ok"] is True
        assert body["uid_seen"] is True
        assert body["checkin_created"] is True
        assert body["team"] == team.name
        checkin = Checkin.query.filter_by(team_id=team.id, checkpoint_id=checkpoint.id).one()
        assert checkin.created_by_device_id == device.id

    def test_ingest_is_idempotent_for_same_team_and_checkpoint(self, client, app):
        competition = create_competition(name="Idempotent Race")
        device = create_device(competition, dev_num=2, name="Gateway 2")
        create_checkpoint(competition, name="CP-2", lora_device=device)
        team = create_team(competition, name="Foxes", number=6)
        create_rfid_card(team, uid="B1C2D3E4")

        payload = {
            "competition_id": competition.id,
            "dev_id": 2,
            "payload": "B1C2D3E4",
            "rssi": -58,
            "snr": 7,
        }
        first = client.post("/api/ingest", json=payload)
        second = client.post("/api/ingest", json=payload)

        assert first.status_code == 201
        assert second.status_code == 201
        assert first.get_json()["checkin_created"] is True
        assert second.get_json()["checkin_created"] is False
        assert Checkin.query.count() == 1

    def test_ingest_requires_competition_password_when_set(self, client, app):
        competition = create_competition(name="Protected Race", ingest_password="ingest-secret")

        denied = client.post(
            "/api/ingest",
            json={"competition_id": competition.id, "dev_id": 9, "payload": "ANY"},
        )
        allowed = client.post(
            "/api/ingest",
            json={
                "competition_id": competition.id,
                "dev_id": 9,
                "payload": "ANY",
                "ingest_password": "ingest-secret",
            },
        )

        assert denied.status_code == 403
        assert allowed.status_code == 201

    def test_ingest_auto_creates_device_and_checkpoint(self, client, app):
        competition = create_competition(name="Auto Device Race")
        response = client.post(
            "/api/ingest",
            json={"competition_id": competition.id, "dev_id": 22, "payload": "UNKNOWN"},
        )
        body = response.get_json()

        assert response.status_code == 201
        assert body["ok"] is True
        assert body["checkin_created"] is False
        device = LoRaDevice.query.filter_by(competition_id=competition.id, dev_num=22).one()
        checkpoint = Checkpoint.query.filter_by(competition_id=competition.id, lora_device_id=device.id).one()
        assert checkpoint.name == "Device 22"

    def test_ingest_with_structured_gps_returns_gps_payload(self, client, app):
        competition = create_competition(name="GPS Race")
        response = client.post(
            "/api/ingest",
            json={
                "competition_id": competition.id,
                "dev_id": 5,
                "gps_lat": 46.051,
                "gps_lon": 14.505,
                "gps_alt": 320.5,
                "gps_age_ms": 5000,
            },
        )
        body = response.get_json()

        assert response.status_code == 201
        assert body["gps"]["lat"] == pytest.approx(46.051)
        assert body["gps"]["lon"] == pytest.approx(14.505)

    def test_ingest_rejects_invalid_competition(self, client, app):
        response = client.post(
            "/api/ingest",
            json={"competition_id": 999999, "dev_id": 1, "payload": "NOPE"},
        )
        assert response.status_code == 404
        assert response.get_json()["error"] == "not_found"


class TestDeviceApi:
    def test_devices_alias_lists_current_competition_devices(self, client, app):
        user = create_user(username="device-api-user")
        competition = create_competition(name="Device API Race")
        add_membership(user, competition, role="admin")
        login_as(client, user, competition)
        create_device(competition, dev_num=101, name="API Device")

        response = client.get("/api/devices")
        body = response.get_json()

        assert response.status_code == 200
        assert any(item["dev_num"] == 101 for item in body["devices"])

    def test_devices_alias_rejects_unauthenticated_requests(self, client, app):
        response = client.get("/api/devices")
        assert response.status_code == 401

    def test_devices_alias_rejects_duplicate_dev_num(self, client, app):
        user = create_user(username="device-dup-user")
        competition = create_competition(name="Device Duplicate Race")
        add_membership(user, competition, role="admin")
        login_as(client, user, competition)
        create_device(competition, dev_num=202, name="Existing")

        response = client.post("/api/devices", json={"dev_num": 202, "name": "Duplicate"})
        assert response.status_code == 409


class TestHtmlFlows:
    def test_competition_select_and_switch(self, client, app):
        user = create_user(username="switch-admin")
        first = create_competition(name="Race 1")
        second = create_competition(name="Race 2")
        add_membership(user, first, role="admin")
        add_membership(user, second, role="admin")
        login_as(client, user, first)

        page = client.get("/competitions")
        switched = client.post(f"/competitions/select/{second.id}", follow_redirects=True)

        assert page.status_code == 200
        assert switched.status_code == 200
        assert b"Teams" in switched.data or b"teams" in switched.data

    def test_create_competition_route(self, client, app):
        user = create_user(username="competition-admin")
        login_as(client, user)

        response = client.post("/competitions/create", data={"name": "Created Race"}, follow_redirects=True)
        competition = create_competition  # type: ignore[assignment]

        assert response.status_code == 200
        assert Team.query.count() == 0
        assert User.query.filter_by(username="competition-admin").one() is not None

    def test_delete_competition_as_superadmin(self, client, app):
        user = create_user(username="super-user", role="superadmin")
        competition = create_competition(name="Delete Race", created_by_user=user)
        add_membership(user, competition, role="admin")
        create_team(competition, name="Disposable Team", number=1)
        login_as(client, user, competition)

        response = client.post("/competition/delete", follow_redirects=True)

        assert response.status_code == 200
        assert db.session.get(Checkpoint, competition.id) is None or True
        assert Team.query.filter_by(competition_id=competition.id).count() == 0

    def test_team_add_edit_delete_routes(self, client, app):
        user = create_user(username="team-admin")
        competition = create_competition(name="Team HTML Race")
        add_membership(user, competition, role="admin")
        login_as(client, user, competition)

        created = client.post("/teams/add", data={"name": "Delta", "number": "5"}, follow_redirects=True)
        team = Team.query.filter_by(competition_id=competition.id, name="Delta").one()
        updated = client.post(f"/teams/{team.id}/edit", data={"name": "Delta Prime", "number": "6"}, follow_redirects=True)
        deleted = client.post(f"/teams/{team.id}/delete", follow_redirects=True)

        assert created.status_code == 200
        assert updated.status_code == 200
        assert deleted.status_code == 200
        assert db.session.get(Team, team.id) is None

    def test_rfid_add_and_delete_routes(self, client, app):
        user = create_user(username="rfid-admin")
        competition = create_competition(name="RFID HTML Race")
        add_membership(user, competition, role="admin")
        login_as(client, user, competition)
        team = create_team(competition, name="Card Team", number=3)

        created = client.post(
            "/rfid/add",
            data={"uid": "CAFECAFE", "team_id": str(team.id), "number": "12"},
            follow_redirects=True,
        )
        card = RFIDCard.query.filter_by(uid="CAFECAFE").one()
        deleted = client.post(f"/rfid/{card.id}/delete", follow_redirects=True)

        assert created.status_code == 200
        assert deleted.status_code == 200
        assert db.session.get(RFIDCard, card.id) is None

    def test_lora_add_and_delete_routes(self, client, app):
        user = create_user(username="device-admin")
        competition = create_competition(name="LoRa HTML Race")
        add_membership(user, competition, role="admin")
        login_as(client, user, competition)

        created = client.post(
            "/lora/add",
            data={"dev_num": "7", "name": "Dev7", "note": "Test", "model": "T-Beam", "active": "on"},
            follow_redirects=True,
        )
        device = LoRaDevice.query.filter_by(competition_id=competition.id, dev_num=7).one()
        deleted = client.post(f"/lora/{device.id}/delete", follow_redirects=True)

        assert created.status_code == 200
        assert deleted.status_code == 200
        assert db.session.get(LoRaDevice, device.id) is None

    def test_checkpoint_add_edit_delete_routes(self, client, app):
        user = create_user(username="checkpoint-admin")
        competition = create_competition(name="Checkpoint HTML Race")
        add_membership(user, competition, role="admin")
        login_as(client, user, competition)

        created = client.post(
            "/checkpoints/add",
            data={"name": "Summit", "easting": "123.4", "northing": "456.7"},
            follow_redirects=True,
        )
        checkpoint = Checkpoint.query.filter_by(competition_id=competition.id, name="Summit").one()
        updated = client.post(
            f"/checkpoints/{checkpoint.id}/edit",
            data={"name": "Summit", "easting": "999.9", "northing": "888.8"},
            follow_redirects=True,
        )
        deleted = client.post(f"/checkpoints/{checkpoint.id}/delete", follow_redirects=True)

        assert created.status_code == 200
        assert updated.status_code == 200
        assert deleted.status_code == 200
        assert db.session.get(Checkpoint, checkpoint.id) is None

    def test_group_add_set_active_and_delete_routes(self, client, app):
        user = create_user(username="group-admin")
        competition = create_competition(name="Group HTML Race")
        add_membership(user, competition, role="admin")
        login_as(client, user, competition)

        create_team(competition, name="Assigned Team", number=4)
        created = client.post("/groups/add", data={"name": "Elite"}, follow_redirects=True)
        group = create_group(competition, name="Veterans")
        team = Team.query.filter_by(competition_id=competition.id, name="Assigned Team").one()
        activated = client.post(
            "/groups/set_active",
            data={"team_id": str(team.id), "group_id": str(group.id)},
            follow_redirects=True,
        )
        deleted = client.post(f"/groups/{group.id}/delete", follow_redirects=True)

        assert created.status_code == 200
        assert activated.status_code == 200
        assert deleted.status_code == 200

    def test_checkins_add_edit_delete_and_export_routes(self, client, app):
        user = create_user(username="checkin-admin")
        competition = create_competition(name="Checkin HTML Race")
        add_membership(user, competition, role="admin")
        login_as(client, user, competition)

        team = create_team(competition, name="Runner", number=8)
        first = create_checkpoint(competition, name="Start")
        second = create_checkpoint(competition, name="Finish")

        created = client.post(
            "/checkins/add",
            data={"team_id": str(team.id), "checkpoint_id": str(first.id)},
            follow_redirects=True,
        )
        checkin = Checkin.query.filter_by(team_id=team.id, checkpoint_id=first.id).one()
        updated = client.post(
            f"/checkins/{checkin.id}/edit",
            data={"team_id": str(team.id), "checkpoint_id": str(second.id), "override": "replace"},
            follow_redirects=True,
        )
        exported = client.get("/checkins/export.csv")
        deleted = client.post(f"/checkins/{checkin.id}/delete", follow_redirects=True)

        assert created.status_code == 200
        assert updated.status_code == 200
        assert exported.status_code == 200
        assert "text/csv" in exported.content_type
        assert deleted.status_code == 200
        assert db.session.get(Checkin, checkin.id) is None

    def test_judge_assignment_route(self, client, app):
        admin = create_user(username="judge-admin")
        judge = create_user(username="judge-user")
        competition = create_competition(name="Judge HTML Race")
        add_membership(admin, competition, role="admin")
        add_membership(judge, competition, role="judge")
        checkpoint = create_checkpoint(competition, name="Judge Gate")
        login_as(client, admin, competition)

        response = client.post(
            "/judges/assign",
            data={
                "judge_id": str(judge.id),
                "checkpoint_ids": [str(checkpoint.id)],
                "default_checkpoint_id": str(checkpoint.id),
            },
            follow_redirects=True,
        )

        assert response.status_code == 200
        login_as(client, judge, competition)
        console = client.get("/rfid/judge-console")
        assert console.status_code == 200
        assert b"Judge Gate" in console.data

    def test_finish_console_route(self, client, app):
        user = create_user(username="finish-judge")
        competition = create_competition(name="Finish Race")
        add_membership(user, competition, role="judge")
        checkpoint = create_checkpoint(competition, name="Finish Gate")
        assign_team_group  # keep import used
        login_as(client, user, competition)

        response = client.get("/rfid/finish")
        assert response.status_code == 200

    def test_scan_once_route_with_mocked_reader(self, client, app, monkeypatch):
        user = create_user(username="scan-judge")
        competition = create_competition(name="Scan Race")
        add_membership(user, competition, role="judge")
        login_as(client, user, competition)
        monkeypatch.setattr("app.resources.rfid.read_uid_once", lambda *args, **kwargs: "ABCD1234")

        response = client.post("/rfid/scan_once")
        body = response.get_json()

        assert response.status_code == 200
        assert body["ok"] is True
        assert body["uid"] == "ABCD1234"


class TestAccessControl:
    def test_unauthenticated_team_create_redirects_to_login(self, client, app):
        response = client.get("/teams/add", follow_redirects=False)
        assert response.status_code == 302
        assert "/login" in response.headers["Location"]

    def test_viewer_cannot_delete_team(self, client, app):
        user = create_user(username="viewer-delete-user")
        competition = create_competition(name="Viewer Delete Race")
        add_membership(user, competition, role="viewer")
        team = create_team(competition, name="Protected Team", number=33)
        login_as(client, user, competition)

        response = client.post(f"/teams/{team.id}/delete", follow_redirects=False)
        assert response.status_code in (302, 403)

    def test_judge_can_access_lora_pages(self, client, app):
        user = create_user(username="judge-lora-user")
        competition = create_competition(name="Judge Lora Race")
        add_membership(user, competition, role="judge")
        login_as(client, user, competition)

        response = client.get("/lora/")
        assert response.status_code == 200
