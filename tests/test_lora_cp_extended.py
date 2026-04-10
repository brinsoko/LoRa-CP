from __future__ import annotations

import csv
import importlib
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.extensions import db
from app.models import Checkin, LoRaMessage
from app.utils import serial_helpers, sheets_sync
from app.utils.card_tokens import compute_card_digest, match_digests
from tests.support import (
    add_membership,
    assign_judge_checkpoint,
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


class TestCardDigestHelpers:
    def test_compute_card_digest_uses_current_format(self, app):
        with app.app_context():
            digest = compute_card_digest("AABBCCDD", 7)
        assert digest is not None
        assert len(digest) == 8
        assert all(ch in "0123456789abcdef" for ch in digest)

    def test_compute_card_digest_is_stable(self, app):
        with app.app_context():
            first = compute_card_digest("AABBCCDD", 7)
            second = compute_card_digest("AABBCCDD", 7)
        assert first == second

    def test_match_digests_returns_matching_device_ids(self, app):
        with app.app_context():
            digest = compute_card_digest("UID12345", 11)
            rows = match_digests("UID12345", [digest], [9, 11, 15])
        assert rows == [{"digest": digest, "matches": [11]}]


class TestSerialHelpers:
    def test_find_serial_port_prefers_hint(self, monkeypatch):
        ports = [
            SimpleNamespace(device="/dev/cu.usbmodem-a", description="USB Modem A", manufacturer="Acme"),
            SimpleNamespace(device="/dev/cu.reader-b", description="RFID Reader B", manufacturer="Brin"),
        ]
        monkeypatch.setattr(serial_helpers.list_ports, "comports", lambda: ports)

        assert serial_helpers.find_serial_port("reader b") == "/dev/cu.reader-b"

    def test_read_uid_once_normalizes_reader_output(self, monkeypatch):
        class FakeSerial:
            def __init__(self, port, baudrate, timeout):
                self.port = port
                self.baudrate = baudrate
                self.timeout = timeout

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def readline(self):
                return b"tag=aa-bb-cc-dd\r\n"

        monkeypatch.setattr(serial_helpers, "find_serial_port", lambda hint="": "/dev/ttyUSB0")
        monkeypatch.setattr(serial_helpers.serial, "Serial", FakeSerial)

        assert serial_helpers.read_uid_once(9600, "rfid", 1.0) == "AABBCCDD"


class TestConfigHardening:
    def test_default_secret_key_is_dev_only(self, monkeypatch):
        import config as config_module

        monkeypatch.setenv("FLASK_ENV", "development")
        monkeypatch.delenv("SECRET_KEY", raising=False)
        monkeypatch.delenv("LORA_WEBHOOK_SECRET", raising=False)
        reloaded = importlib.reload(config_module)

        assert reloaded.Config.SECRET_KEY == "dev-secret"
        assert reloaded.Config.LORA_WEBHOOK_SECRET == "CHANGE_LATER"

    def test_production_requires_secret_key(self, monkeypatch):
        import config as config_module

        monkeypatch.setenv("FLASK_ENV", "production")
        monkeypatch.delenv("SECRET_KEY", raising=False)
        monkeypatch.setenv("LORA_WEBHOOK_SECRET", "webhook-secret")
        with pytest.raises(SystemExit):
            importlib.reload(config_module)

        monkeypatch.setenv("FLASK_ENV", "development")
        importlib.reload(config_module)

    def test_production_requires_non_default_webhook_secret(self, monkeypatch):
        import config as config_module

        monkeypatch.setenv("FLASK_ENV", "production")
        monkeypatch.setenv("SECRET_KEY", "prod-secret")
        monkeypatch.setenv("LORA_WEBHOOK_SECRET", "CHANGE_LATER")
        with pytest.raises(SystemExit):
            importlib.reload(config_module)

        monkeypatch.setenv("FLASK_ENV", "development")
        monkeypatch.delenv("SECRET_KEY", raising=False)
        monkeypatch.delenv("LORA_WEBHOOK_SECRET", raising=False)
        importlib.reload(config_module)


class TestDocsAndConfig:
    def test_config_exposes_supported_languages(self, app):
        assert app.config["BABEL_DEFAULT_LOCALE"] == "en"
        assert "sl" in app.config["LANGUAGES"]

    def test_translation_directory_is_absolute(self, app):
        assert app.config["BABEL_TRANSLATION_DIRECTORIES"].startswith("/")

    def test_api_docs_list_exposes_openapi(self, client, app):
        response = client.get("/api/docs")
        body = response.get_json()
        assert response.status_code == 200
        assert any(spec["name"] == "openapi.json" for spec in body["specs"])

    def test_docs_openapi_proxy_returns_json(self, client, app):
        response = client.get("/docs/openapi.json")
        spec = response.get_json()
        assert response.status_code == 200
        assert spec["openapi"].startswith("3.")
        assert "/api/ingest" in spec["paths"]

    def test_docs_swagger_page_renders(self, client, app):
        response = client.get("/docs/")
        assert response.status_code == 200
        data = response.data.lower()
        assert b"swagger" in data or b"openapi" in data

    def test_openapi_structure_includes_core_paths_and_schemas(self, client, app):
        response = client.get("/docs/openapi.json")
        spec = response.get_json()

        assert response.status_code == 200
        assert "/api/ingest" in spec["paths"]
        assert "/api/rfid/verify" in spec["paths"]
        assert "/api/devices" in spec["paths"]
        assert spec["components"]["schemas"]


class TestLocaleAndRedirectSecurity:
    def test_set_language_to_slovenian(self, client, app):
        response = client.get("/lang/sl")
        assert response.status_code == 302
        with client.session_transaction() as sess:
            assert sess["lang"] == "sl"

    def test_unknown_language_returns_404(self, client, app):
        response = client.get("/lang/xx")
        assert response.status_code == 404

    def test_language_switch_blocks_external_redirects(self, client, app):
        response = client.get("/lang/sl?next=https://attacker.example", follow_redirects=False)

        assert response.status_code == 302
        assert response.headers["Location"] == "/"

    def test_google_oauth_callback_blocks_external_redirects(self, app_factory, monkeypatch):
        application = app_factory(
            GOOGLE_OAUTH_CLIENT_ID="client-id",
            GOOGLE_OAUTH_CLIENT_SECRET="client-secret",
        )
        client = application.test_client()

        class FakeTokenResponse:
            status_code = 200

            def json(self):
                return {"id_token": "signed-token"}

        monkeypatch.setattr(
            "app.blueprints.auth.routes.requests.post",
            lambda *args, **kwargs: FakeTokenResponse(),
        )
        monkeypatch.setattr(
            "app.blueprints.auth.routes.id_token.verify_oauth2_token",
            lambda *args, **kwargs: {"sub": "google-sub-1", "email": "oauth@example.com"},
        )

        with client.session_transaction() as sess:
            sess["google_oauth_state"] = "state-123"
            sess["google_oauth_next"] = "https://attacker.example/path"

        response = client.get(
            "/login/google/callback?state=state-123&code=oauth-code",
            follow_redirects=False,
        )

        assert response.status_code == 302
        assert response.headers["Location"].endswith("/competitions")
        assert "attacker.example" not in response.headers["Location"]


class TestWebhookSecurity:
    def test_ingest_rejects_missing_webhook_secret_header(self, app_factory):
        application = app_factory(LORA_WEBHOOK_SECRET="webhook-secret")
        client = application.test_client()
        with application.app_context():
            competition_id = create_competition(name="Webhook Race").id

        response = client.post(
            "/api/ingest",
            json={"competition_id": competition_id, "dev_id": 1, "payload": "AABBCCDD"},
        )
        body = response.get_json()

        assert response.status_code == 403
        assert body["error"] == "forbidden"

    def test_ingest_rejects_wrong_webhook_secret_header(self, app_factory):
        application = app_factory(LORA_WEBHOOK_SECRET="webhook-secret")
        client = application.test_client()
        with application.app_context():
            competition_id = create_competition(name="Webhook Race 2").id

        response = client.post(
            "/api/ingest",
            json={"competition_id": competition_id, "dev_id": 1, "payload": "AABBCCDD"},
            headers={"X-Webhook-Secret": "wrong-secret"},
        )

        assert response.status_code == 403

    def test_ingest_accepts_correct_webhook_secret_header(self, app_factory):
        application = app_factory(LORA_WEBHOOK_SECRET="webhook-secret")
        client = application.test_client()
        with application.app_context():
            competition_id = create_competition(name="Webhook Allowed Race").id

        response = client.post(
            "/api/ingest",
            json={"competition_id": competition_id, "dev_id": 33, "payload": "AABBCCDD"},
            headers={"X-Webhook-Secret": "webhook-secret"},
        )

        assert response.status_code == 201
        assert response.get_json()["ok"] is True


class TestVerificationAndMessages:
    def test_rfid_verify_matches_known_device(self, client, app):
        admin = create_user(username="verify-admin")
        competition = create_competition(name="Verify Race")
        add_membership(admin, competition, role="admin")
        login_as(client, admin, competition)

        device = create_device(competition, dev_num=10, name="Device 10")
        checkpoint = create_checkpoint(competition, name="Verify CP", lora_device=device)
        team = create_team(competition, name="Verify Team", number=10)
        create_rfid_card(team, uid="VERIFY01")
        create_checkin(competition, team, checkpoint, created_by_device=device)

        with app.app_context():
            digest = compute_card_digest("VERIFY01", 10)

        response = client.post(
            "/api/rfid/verify",
            json={"uid": "VERIFY01", "digests": [digest], "device_ids": [10]},
        )
        body = response.get_json()

        assert response.status_code == 200
        assert body["team"]["name"] == team.name
        assert body["results"][0]["matches"][0]["device_id"] == 10
        assert body["results"][0]["matches"][0]["checked_in"] is True

    def test_rfid_verify_marks_unknown_digest(self, client, app):
        admin = create_user(username="verify-admin-2")
        competition = create_competition(name="Unknown Verify Race")
        add_membership(admin, competition, role="admin")
        login_as(client, admin, competition)
        create_device(competition, dev_num=12, name="Device 12")
        create_checkpoint(competition, name="Verify Unknown CP")

        response = client.post(
            "/api/rfid/verify",
            json={"uid": "NOPE0001", "digests": ["deadbeef"], "device_ids": [12]},
        )
        body = response.get_json()

        assert response.status_code == 200
        assert body["unknown"] == ["deadbeef"]
        assert body["team"] is None

    def test_rfid_verify_rejects_invalid_checkpoint_id_list(self, client, app):
        admin = create_user(username="verify-invalid-admin")
        competition = create_competition(name="Verify Invalid Race")
        add_membership(admin, competition, role="admin")
        login_as(client, admin, competition)
        create_checkpoint(competition, name="Any CP")

        response = client.post(
            "/api/rfid/verify",
            json={"uid": "VERIFY99", "digests": [], "checkpoint_ids": ["abc"]},
        )

        assert response.status_code == 400
        assert response.get_json()["detail"] == "checkpoint_ids must be integers"

    def test_finish_console_shows_only_assigned_checkpoints(self, client, app):
        judge = create_user(username="finish-judge")
        competition = create_competition(name="Finish Assignment Race")
        add_membership(judge, competition, role="judge")
        first = create_checkpoint(competition, name="Finish A")
        second = create_checkpoint(competition, name="Finish B")
        assign_judge_checkpoint(judge, first, is_default=True)
        login_as(client, judge, competition)

        response = client.get("/rfid/finish")
        html = response.data.decode("utf-8", errors="replace")

        assert response.status_code == 200
        assert "Finish A" in html
        assert "Finish B" not in html

    def test_messages_endpoint_returns_pagination_meta(self, client, app):
        admin = create_user(username="message-admin")
        competition = create_competition(name="Message Race")
        add_membership(admin, competition, role="admin")
        login_as(client, admin, competition)

        device = create_device(competition, dev_num=15, name="Msg Device")
        create_checkpoint(competition, name="Msg CP", lora_device=device)
        client.post(
            "/api/ingest",
            json={
                "competition_id": competition.id,
                "dev_id": 15,
                "gps_lat": 46.051,
                "gps_lon": 14.505,
            },
        )

        response = client.get("/api/devices/messages?per_page=10")
        body = response.get_json()

        assert response.status_code == 200
        assert body["meta"]["page"] == 1
        assert body["meta"]["per_page"] == 10
        assert len(body["messages"]) >= 1

    def test_map_lora_points_returns_latest_gps_point(self, client, app):
        admin = create_user(username="map-admin")
        competition = create_competition(name="Map Race")
        add_membership(admin, competition, role="admin")
        login_as(client, admin, competition)

        device = create_device(competition, dev_num=21, name="Map Device")
        create_checkpoint(competition, name="Map CP", lora_device=device)
        client.post(
            "/api/ingest",
            json={
                "competition_id": competition.id,
                "dev_id": 21,
                "gps_lat": 46.052,
                "gps_lon": 14.506,
                "gps_alt": 321.0,
                "gps_age_ms": 1200,
            },
        )

        response = client.get("/api/map/lora-points")
        body = response.get_json()

        assert response.status_code == 200
        assert any(point["dev_id"] == "21" for point in body)


class TestCsvAndIsolation:
    def test_csv_export_header_only_when_empty(self, client, app):
        admin = create_user(username="csv-admin")
        competition = create_competition(name="CSV Empty Race")
        add_membership(admin, competition, role="admin")
        login_as(client, admin, competition)

        response = client.get("/checkins/export.csv")
        lines = [line for line in response.data.decode("utf-8").splitlines() if line]

        assert response.status_code == 200
        assert lines == ["timestamp_utc,team_id,team_name,team_number,checkpoint_id,checkpoint_name"]

    def test_api_checkin_export_orders_oldest_first(self, client, app):
        admin = create_user(username="csv-order-admin")
        competition = create_competition(name="CSV Order Race")
        add_membership(admin, competition, role="admin")
        login_as(client, admin, competition)

        checkpoint = create_checkpoint(competition, name="Order CP")
        older_team = create_team(competition, name="Older Team", number=1)
        newer_team = create_team(competition, name="Newer Team", number=2)
        create_checkin(
            competition,
            older_team,
            checkpoint,
            timestamp=datetime.utcnow() - timedelta(hours=1),
        )
        create_checkin(
            competition,
            newer_team,
            checkpoint,
            timestamp=datetime.utcnow(),
        )

        response = client.get("/api/checkins/export.csv?sort=old")
        rows = list(csv.reader(response.data.decode("utf-8").splitlines()))

        assert response.status_code == 200
        assert rows[0] == [
            "timestamp_utc",
            "team_id",
            "team_name",
            "team_number",
            "checkpoint_id",
            "checkpoint_name",
        ]
        assert rows[1][2] == "Older Team"
        assert rows[2][2] == "Newer Team"

    def test_csv_export_escapes_formula_cells(self, client, app):
        admin = create_user(username="csv-escape-admin")
        competition = create_competition(name="CSV Escape Race")
        add_membership(admin, competition, role="admin")
        login_as(client, admin, competition)

        checkpoint = create_checkpoint(competition, name="=Danger")
        team = create_team(competition, name="+Malicious", number=4)
        create_checkin(competition, team, checkpoint)

        response = client.get("/api/checkins/export.csv")
        rows = list(csv.reader(response.data.decode("utf-8").splitlines()))

        assert response.status_code == 200
        assert rows[1][2] == "'+Malicious"
        assert rows[1][5] == "'=Danger"

    def test_api_checkins_list_is_paginated(self, client, app):
        admin = create_user(username="pagination-admin")
        competition = create_competition(name="Pagination Race")
        add_membership(admin, competition, role="admin")
        login_as(client, admin, competition)

        checkpoint = create_checkpoint(competition, name="Pagination CP")
        teams = [
            create_team(competition, name="Team One", number=1),
            create_team(competition, name="Team Two", number=2),
            create_team(competition, name="Team Three", number=3),
        ]
        create_checkin(competition, teams[0], checkpoint, timestamp=datetime.utcnow() - timedelta(hours=2))
        create_checkin(competition, teams[1], checkpoint, timestamp=datetime.utcnow() - timedelta(hours=1))
        create_checkin(competition, teams[2], checkpoint, timestamp=datetime.utcnow())

        response = client.get("/api/checkins?sort=old&page=2&per_page=2")
        body = response.get_json()

        assert response.status_code == 200
        assert body["pagination"] == {
            "page": 2,
            "per_page": 2,
            "pages": 2,
            "total": 3,
            "has_prev": True,
            "has_next": False,
        }
        assert [row["team"]["name"] for row in body["checkins"]] == ["Team Three"]

    def test_api_checkin_export_is_paginated(self, client, app):
        admin = create_user(username="csv-page-admin")
        competition = create_competition(name="CSV Page Race")
        add_membership(admin, competition, role="admin")
        login_as(client, admin, competition)

        checkpoint = create_checkpoint(competition, name="CSV Page CP")
        teams = [
            create_team(competition, name="CSV Team One", number=1),
            create_team(competition, name="CSV Team Two", number=2),
            create_team(competition, name="CSV Team Three", number=3),
        ]
        create_checkin(competition, teams[0], checkpoint, timestamp=datetime.utcnow() - timedelta(hours=2))
        create_checkin(competition, teams[1], checkpoint, timestamp=datetime.utcnow() - timedelta(hours=1))
        create_checkin(competition, teams[2], checkpoint, timestamp=datetime.utcnow())

        response = client.get("/api/checkins/export.csv?sort=old&page=2&per_page=2")
        rows = list(csv.reader(response.data.decode("utf-8").splitlines()))

        assert response.status_code == 200
        assert len(rows) == 2
        assert rows[1][2] == "CSV Team Three"

    def test_viewer_can_read_but_not_write(self, client, app):
        viewer = create_user(username="viewer-user")
        competition = create_competition(name="Viewer Race")
        add_membership(viewer, competition, role="viewer")
        login_as(client, viewer, competition)

        read_response = client.get("/teams/")
        write_response = client.post("/teams/add", data={"name": "Blocked", "number": "1"})

        assert read_response.status_code == 200
        assert write_response.status_code in (302, 403)

    def test_active_competition_scopes_visible_teams(self, client, app):
        user = create_user(username="scoped-user")
        first = create_competition(name="Scope A")
        second = create_competition(name="Scope B")
        add_membership(user, first, role="admin")
        add_membership(user, second, role="admin")
        create_team(first, name="Visible Team", number=1)
        create_team(second, name="Hidden Team", number=2)
        login_as(client, user, first)

        response = client.get("/teams/")
        html = response.data.decode("utf-8", errors="replace")

        assert response.status_code == 200
        assert "Visible Team" in html
        assert "Hidden Team" not in html

    def test_multi_competition_role_isolation_blocks_viewer_writes(self, client, app):
        user = create_user(username="isolated-user")
        admin_comp = create_competition(name="Writable Race")
        viewer_comp = create_competition(name="Read Only Race")
        add_membership(user, admin_comp, role="admin")
        add_membership(user, viewer_comp, role="viewer")
        admin_team = create_team(admin_comp, name="Writable Team", number=1)
        admin_cp = create_checkpoint(admin_comp, name="Writable CP")
        viewer_team = create_team(viewer_comp, name="Read Team", number=2)
        viewer_cp = create_checkpoint(viewer_comp, name="Read CP")

        login_as(client, user, viewer_comp)
        denied = client.post(
            "/api/checkins",
            json={"team_id": viewer_team.id, "checkpoint_id": viewer_cp.id},
        )

        login_as(client, user, admin_comp)
        allowed = client.post(
            "/api/checkins",
            json={"team_id": admin_team.id, "checkpoint_id": admin_cp.id},
        )

        assert denied.status_code == 403
        assert allowed.status_code == 201


class TestJudgeAssignments:
    def test_judge_console_shows_only_assigned_checkpoints(self, client, app):
        admin = create_user(username="assign-admin")
        judge = create_user(username="assign-judge")
        competition = create_competition(name="Judge Console Race")
        add_membership(admin, competition, role="admin")
        add_membership(judge, competition, role="judge")
        first = create_checkpoint(competition, name="Gate A")
        second = create_checkpoint(competition, name="Gate B")
        assign_judge_checkpoint(judge, first, is_default=True)
        login_as(client, judge, competition)

        response = client.get("/rfid/judge-console")
        html = response.data.decode("utf-8", errors="replace")

        assert response.status_code == 200
        assert "Gate A" in html
        assert "Gate B" not in html

    def test_checkin_add_prefills_judge_default_checkpoint(self, client, app):
        admin = create_user(username="default-admin")
        judge = create_user(username="default-judge")
        competition = create_competition(name="Judge Default Race")
        add_membership(admin, competition, role="admin")
        add_membership(judge, competition, role="judge")
        checkpoint = create_checkpoint(competition, name="Default Gate")
        assign_judge_checkpoint(judge, checkpoint, is_default=True)
        login_as(client, judge, competition)

        response = client.get("/checkins/add")
        html = response.data.decode("utf-8", errors="replace")

        assert response.status_code == 200
        assert "Default Gate" in html

    def test_judge_cannot_submit_unassigned_checkpoint(self, client, app):
        judge = create_user(username="guarded-judge")
        competition = create_competition(name="Judge Guard Race")
        add_membership(judge, competition, role="judge")
        allowed_cp = create_checkpoint(competition, name="Allowed CP")
        denied_cp = create_checkpoint(competition, name="Denied CP")
        team = create_team(competition, name="Assigned Team", number=9)
        assign_judge_checkpoint(judge, allowed_cp, is_default=True)
        login_as(client, judge, competition)

        response = client.post(
            "/api/checkins",
            json={"team_id": team.id, "checkpoint_id": denied_cp.id},
        )

        assert response.status_code == 403
        assert response.get_json()["detail"] == "Checkpoint is not assigned to the current judge."


class TestValidationAndSecurity:
    def test_create_user_rejects_invalid_username(self, client, app):
        admin = create_user(username="user-admin")
        competition = create_competition(name="User Validation Race")
        add_membership(admin, competition, role="admin")
        login_as(client, admin, competition)

        response = client.post(
            "/api/users",
            json={"username": "bad user!", "password": "secret123", "role": "judge"},
        )

        assert response.status_code == 400
        assert "username may contain only" in response.get_json()["error"]

    def test_team_api_rejects_overlong_name(self, client, app):
        admin = create_user(username="team-validation-admin")
        competition = create_competition(name="Team Validation Race")
        add_membership(admin, competition, role="admin")
        login_as(client, admin, competition)

        response = client.post("/api/teams", json={"name": "T" * 101})

        assert response.status_code == 400
        assert response.get_json()["detail"] == "name must be at most 100 characters"

    def test_group_api_rejects_control_characters(self, client, app):
        admin = create_user(username="group-validation-admin")
        competition = create_competition(name="Group Validation Race")
        add_membership(admin, competition, role="admin")
        login_as(client, admin, competition)

        response = client.post(
            "/api/groups",
            json={"name": "Group Name", "description": "bad\x01value"},
        )

        assert response.status_code == 400
        assert response.get_json()["detail"] == "description contains invalid control characters"

    def test_checkpoint_api_rejects_overlong_location(self, client, app):
        admin = create_user(username="checkpoint-validation-admin")
        competition = create_competition(name="Checkpoint Validation Race")
        add_membership(admin, competition, role="admin")
        login_as(client, admin, competition)

        response = client.post(
            "/api/checkpoints",
            json={"name": "Valid Name", "location": "L" * 256},
        )

        assert response.status_code == 400
        assert response.get_json()["detail"] == "location must be at most 255 characters"

    def test_api_404_uses_json_error_envelope(self, client, app):
        response = client.get("/api/nope")
        body = response.get_json()

        assert response.status_code == 404
        assert body["error"] == "not_found"
        assert body["code"] == 404

    def test_api_405_uses_json_error_envelope(self, client, app):
        response = client.open("/api/auth/me", method="TRACE")
        body = response.get_json()

        assert response.status_code == 405
        assert body["error"] == "method_not_allowed"
        assert body["code"] == 405

    def test_html_post_requires_csrf_token(self, app_factory):
        application = app_factory(WTF_CSRF_ENABLED=True)
        client = application.test_client()
        with application.app_context():
            admin = create_user(username="csrf-form-admin")
            competition = create_competition(name="CSRF Form Race")
            add_membership(admin, competition, role="admin")
            admin_id = admin.id
            competition_id = competition.id

        with application.app_context():
            login_as(client, db.session.get(type(admin), admin_id), db.session.get(type(competition), competition_id))
        seed = client.get("/teams/")
        assert seed.status_code == 200

        denied = client.post("/logout")
        with client.session_transaction() as sess:
            token = sess["_csrf_token"]
        allowed = client.post("/logout", data={"csrf_token": token}, follow_redirects=False)

        assert denied.status_code == 400
        assert allowed.status_code == 302

    def test_api_post_requires_csrf_token_header(self, app_factory):
        application = app_factory(WTF_CSRF_ENABLED=True)
        client = application.test_client()
        with application.app_context():
            admin = create_user(username="csrf-api-admin")
            competition = create_competition(name="CSRF API Race")
            add_membership(admin, competition, role="admin")
            team = create_team(competition, name="CSRF Team", number=7)
            checkpoint = create_checkpoint(competition, name="CSRF CP")
            admin_id = admin.id
            competition_id = competition.id
            team_id = team.id
            checkpoint_id = checkpoint.id

        with application.app_context():
            login_as(client, db.session.get(type(admin), admin_id), db.session.get(type(competition), competition_id))
        seed = client.get("/teams/")
        assert seed.status_code == 200
        with client.session_transaction() as sess:
            token = sess["_csrf_token"]

        denied = client.post("/api/checkins", json={"team_id": team_id, "checkpoint_id": checkpoint_id})
        allowed = client.post(
            "/api/checkins",
            json={"team_id": team_id, "checkpoint_id": checkpoint_id},
            headers={"X-CSRF-Token": token},
        )

        assert denied.status_code == 400
        assert denied.get_json()["detail"] == "CSRF token missing or invalid."
        assert allowed.status_code == 201

    def test_device_api_rejects_control_characters_in_note(self, client, app):
        admin = create_user(username="device-validation-admin")
        competition = create_competition(name="Device Validation Race")
        add_membership(admin, competition, role="admin")
        login_as(client, admin, competition)

        response = client.post(
            "/api/devices",
            json={"dev_num": 77, "name": "Valid Device", "note": "bad\x01note"},
        )

        assert response.status_code == 400
        assert "note contains invalid control characters" in response.get_json()["detail"]

    def test_checkpoint_bulk_import_rejects_invalid_name(self, client, app):
        admin = create_user(username="checkpoint-bulk-admin")
        competition = create_competition(name="Checkpoint Bulk Validation Race")
        add_membership(admin, competition, role="admin")
        login_as(client, admin, competition)

        response = client.post(
            "/api/checkpoints/import",
            json={"items": [{"name": "N" * 121}]},
        )
        body = response.get_json()

        assert response.status_code == 200
        assert body["summary"]["skipped"] == 1
        assert body["errors"][0]["detail"] == "name must be at most 120 characters"


class TestGroupsAndSheets:
    def test_group_page_uses_current_group_model(self, client, app):
        admin = create_user(username="group-page-admin")
        competition = create_competition(name="Group Page Race")
        add_membership(admin, competition, role="admin")
        login_as(client, admin, competition)

        group = create_group(competition, name="Masters")
        team = create_team(competition, name="Grouped Team", number=9)
        assign_team_group(team, group, active=True)

        response = client.post(
            "/groups/set_active",
            data={"team_id": str(team.id), "group_id": str(group.id)},
            follow_redirects=True,
        )

        assert response.status_code == 200

    def test_sheets_page_is_stable_when_sync_is_disabled(self, client, app):
        admin = create_user(username="sheets-admin")
        competition = create_competition(name="Sheets Race")
        add_membership(admin, competition, role="admin")
        login_as(client, admin, competition)

        response = client.get("/sheets/")
        html = response.data.decode("utf-8", errors="replace").lower()

        assert response.status_code == 200
        assert "traceback" not in html

    def test_mark_arrival_checkbox_is_noop_when_sync_disabled(self, app_factory, monkeypatch):
        application = app_factory(SHEETS_SYNC_ENABLED=False)

        def fail_if_called(*args, **kwargs):
            raise AssertionError("SheetsClient should not be constructed when sync is disabled")

        monkeypatch.setattr(sheets_sync, "SheetsClient", fail_if_called)

        with application.app_context():
            competition = create_competition(name="Sheets Guard Race")
            team = create_team(competition, name="Sheets Team", number=5)
            checkpoint = create_checkpoint(competition, name="Sheets CP")

            sheets_sync.mark_arrival_checkbox(team.id, checkpoint.id)


class TestRegressions:
    def test_ingest_creates_messages_in_target_competition_only(self, client, app):
        first = create_competition(name="Msg Race A")
        second = create_competition(name="Msg Race B")
        create_checkpoint(first, name="CP A", lora_device=create_device(first, dev_num=44, name="Dev44-A"))
        create_checkpoint(second, name="CP B", lora_device=create_device(second, dev_num=44, name="Dev44-B"))

        response = client.post(
            "/api/ingest",
            json={"competition_id": second.id, "dev_id": 44, "payload": "UNKNOWN"},
        )

        assert response.status_code == 201
        assert LoRaMessage.query.filter_by(competition_id=first.id).count() == 0
        assert LoRaMessage.query.filter_by(competition_id=second.id).count() == 1

    def test_competition_delete_cascades_checkins(self, client, app):
        user = create_user(username="cascade-super", role="superadmin")
        competition = create_competition(name="Cascade Race", created_by_user=user)
        add_membership(user, competition, role="admin")
        team = create_team(competition, name="Cascade Team", number=1)
        checkpoint = create_checkpoint(competition, name="Cascade CP")
        checkin = create_checkin(competition, team, checkpoint)
        checkin_id = checkin.id
        login_as(client, user, competition)

        response = client.post("/competition/delete", follow_redirects=True)

        assert response.status_code == 200
        assert db.session.get(Checkin, checkin_id) is None
