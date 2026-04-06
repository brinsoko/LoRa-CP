from __future__ import annotations

import json

import pytest

from app.extensions import db
from app.models import Checkin, Checkpoint, JudgeCheckpoint, LoRaMessage, Team
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


class TestLocaleRoutes:
    def test_set_language_to_slovenian(self, client, app):
        response = client.get("/lang/sl")
        assert response.status_code == 302
        with client.session_transaction() as sess:
            assert sess["lang"] == "sl"

    def test_unknown_language_returns_404(self, client, app):
        response = client.get("/lang/xx")
        assert response.status_code == 404


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

    def test_csv_export_team_sort_orders_by_team_name(self, client, app):
        admin = create_user(username="csv-sort-admin")
        competition = create_competition(name="CSV Sort Race")
        add_membership(admin, competition, role="admin")
        login_as(client, admin, competition)

        checkpoint = create_checkpoint(competition, name="Sort CP")
        alpha = create_team(competition, name="Alpha", number=2)
        zulu = create_team(competition, name="Zulu", number=1)
        create_checkin(competition, zulu, checkpoint)
        create_checkin(competition, alpha, checkpoint)

        response = client.get("/checkins/export.csv?sort=team")
        lines = response.data.decode("utf-8").splitlines()

        assert response.status_code == 200
        assert "Alpha" in lines[1]
        assert "Zulu" in lines[2]

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

