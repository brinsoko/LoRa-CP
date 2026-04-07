from __future__ import annotations

import json
from dataclasses import dataclass

import pytest

from app.extensions import db
from app.models import (
    Checkin,
    Checkpoint,
    CheckpointGroupLink,
    Competition,
    GlobalScoreRule,
    LoRaMessage,
    ScoreEntry,
    ScoreRule,
    SheetConfig,
    Team,
    User,
)
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


@dataclass
class SeededState:
    competition_id: int
    public_competition_id: int
    admin_id: int
    viewer_id: int
    judge_id: int
    extra_user_id: int
    team_id: int
    second_team_id: int
    group_id: int
    second_group_id: int
    checkpoint_id: int
    second_checkpoint_id: int
    third_checkpoint_id: int
    device_id: int
    device_dev_num: int
    card_id: int
    checkin_id: int
    score_rule_id: int
    global_rule_id: int


@pytest.fixture
def seeded_state(app):
    admin = create_user(username="matrix-admin")
    viewer = create_user(username="matrix-viewer")
    judge = create_user(username="matrix-judge")
    extra_user = create_user(username="matrix-extra")

    competition = create_competition(name="Matrix Race")
    public_competition = create_competition(name="Public Matrix Race", public_results=True)

    add_membership(admin, competition, role="admin")
    add_membership(viewer, competition, role="viewer")
    add_membership(judge, competition, role="judge")

    first_group = create_group(competition, name="Alpha Group", prefix="1xx")
    second_group = create_group(competition, name="Beta Group", prefix="2xx")

    device = create_device(competition, dev_num=17, name="Matrix Device")
    checkpoint = create_checkpoint(competition, name="Matrix CP", lora_device=device)
    second_checkpoint = create_checkpoint(competition, name="Second CP")
    third_checkpoint = create_checkpoint(competition, name="Delete CP")

    db.session.add(CheckpointGroupLink(group_id=first_group.id, checkpoint_id=checkpoint.id, position=0))
    db.session.add(CheckpointGroupLink(group_id=first_group.id, checkpoint_id=second_checkpoint.id, position=1))
    db.session.add(CheckpointGroupLink(group_id=second_group.id, checkpoint_id=second_checkpoint.id, position=0))

    team = create_team(competition, name="Matrix Team", number=10, organization="Org A")
    second_team = create_team(competition, name="Matrix Team 2", number=11, organization="Org B")
    assign_team_group(team, first_group, active=True)
    assign_team_group(second_team, first_group, active=True)

    card = create_rfid_card(team, uid="MATRIX01", number=99)
    checkin = create_checkin(competition, team, checkpoint, created_by_user=admin)
    assign_judge_checkpoint(judge, checkpoint, is_default=True)

    db.session.add(
        LoRaMessage(
            competition_id=competition.id,
            dev_id=str(device.dev_num),
            payload="pos,46.051000,14.505000,320.0,500",
        )
    )
    db.session.add(
        SheetConfig(
            competition_id=competition.id,
            spreadsheet_id="local:matrix",
            spreadsheet_name="Local Matrix",
            tab_name="Matrix CP",
            tab_type="checkpoint",
            checkpoint_id=checkpoint.id,
            config={
                "dead_time_enabled": True,
                "dead_time_header": "Dead Time",
                "time_enabled": True,
                "time_header": "Time",
                "points_header": "Points",
                "groups": [
                    {"group_id": first_group.id, "name": first_group.name, "fields": ["accuracy"]},
                    {"group_id": second_group.id, "name": second_group.name, "fields": ["accuracy"]},
                ],
            },
        )
    )
    score_rule = ScoreRule(
        competition_id=competition.id,
        checkpoint_id=checkpoint.id,
        group_id=first_group.id,
        rules={"field_rules": {"accuracy": {"type": "multiplier", "factor": 2}}, "total_fields": ["accuracy"]},
    )
    global_rule = GlobalScoreRule(
        competition_id=competition.id,
        group_id=first_group.id,
        rules={"found": {"points_per": 5}},
    )
    db.session.add(score_rule)
    db.session.add(global_rule)
    db.session.commit()

    return SeededState(
        competition_id=competition.id,
        public_competition_id=public_competition.id,
        admin_id=admin.id,
        viewer_id=viewer.id,
        judge_id=judge.id,
        extra_user_id=extra_user.id,
        team_id=team.id,
        second_team_id=second_team.id,
        group_id=first_group.id,
        second_group_id=second_group.id,
        checkpoint_id=checkpoint.id,
        second_checkpoint_id=second_checkpoint.id,
        third_checkpoint_id=third_checkpoint.id,
        device_id=device.id,
        device_dev_num=device.dev_num,
        card_id=card.id,
        checkin_id=checkin.id,
        score_rule_id=score_rule.id,
        global_rule_id=global_rule.id,
    )


def _user(state: SeededState, user_id: int) -> User:
    return db.session.get(User, user_id)


def _login(client, state: SeededState, role: str) -> None:
    mapping = {
        "admin": state.admin_id,
        "viewer": state.viewer_id,
        "judge": state.judge_id,
    }
    login_as(client, _user(state, mapping[role]), db.session.get(Competition, state.competition_id))


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("GET", "/teams/", 200),
        ("GET", "/checkpoints/", 403),
        ("GET", "/groups/", 403),
        ("GET", "/users/", 403),
        ("GET", "/messages/", 403),
        ("GET", "/audit/", 403),
        ("GET", "/scores/rules", 403),
        ("GET", "/sheets/", 403),
        ("GET", "/competition/settings", 403),
        ("GET", "/lora/", 403),
        ("GET", "/rfid/", 403),
        ("GET", "/map/", 403),
        ("GET", "/scores/judge", 403),
        ("GET", "/api/auth/me", 200),
        ("GET", "/api/teams", 200),
        ("GET", "/api/checkpoints", 200),
        ("GET", "/api/groups", 200),
        ("GET", "/api/devices", 200),
        ("GET", "/api/rfid/cards", 200),
        ("GET", "/api/users", 403),
        ("GET", "/api/checkins", 403),
        ("GET", "/api/map/checkpoints", 403),
        ("GET", "/api/map/lora-points", 403),
        ("GET", "/api/devices/messages", 403),
        ("GET", "/api/score-rules", 403),
    ],
)
def test_viewer_endpoint_matrix(client, app, seeded_state, method, path, expected):
    _login(client, seeded_state, "viewer")
    response = client.open(path, method=method)
    assert response.status_code == expected


@pytest.mark.parametrize(
    ("method", "path", "expected"),
    [
        ("GET", "/teams/", 200),
        ("GET", "/checkpoints/", 200),
        ("GET", "/groups/", 200),
        ("GET", "/lora/", 200),
        ("GET", "/rfid/", 200),
        ("GET", "/map/", 200),
        ("GET", "/map/devices", 200),
        ("GET", "/scores/judge", 200),
        ("GET", "/rfid/judge-console", 200),
        ("GET", "/rfid/finish", 200),
        ("GET", "/users/", 403),
        ("GET", "/messages/", 403),
        ("GET", "/audit/", 403),
        ("GET", "/scores/rules", 403),
        ("GET", "/sheets/", 403),
        ("GET", "/competition/settings", 403),
        ("GET", "/api/auth/me", 200),
        ("GET", "/api/teams", 200),
        ("GET", "/api/checkpoints", 200),
        ("GET", "/api/groups", 200),
        ("GET", "/api/devices", 200),
        ("GET", "/api/rfid/cards", 200),
        ("GET", "/api/checkins", 200),
        ("GET", "/api/map/checkpoints", 200),
        ("GET", "/api/map/lora-points", 200),
        ("GET", "/api/users", 403),
        ("GET", "/api/devices/messages", 403),
        ("GET", "/api/score-rules", 403),
    ],
)
def test_judge_endpoint_matrix(client, app, seeded_state, method, path, expected):
    _login(client, seeded_state, "judge")
    response = client.open(path, method=method)
    assert response.status_code == expected


def test_judge_can_create_assigned_checkin(client, app, seeded_state):
    _login(client, seeded_state, "judge")
    response = client.post(
        "/api/checkins",
        json={"team_id": seeded_state.second_team_id, "checkpoint_id": seeded_state.checkpoint_id},
    )
    assert response.status_code == 201
    assert response.get_json()["created"] is True


def test_remaining_auth_api_endpoints(client, app, seeded_state):
    _login(client, seeded_state, "admin")

    me = client.get("/api/auth/me")
    changed = client.post(
        "/api/auth/password",
        json={
            "current_password": "password123",
            "new_password": "newsecret123",
            "confirm_password": "newsecret123",
        },
    )
    logout = client.post("/api/auth/logout")
    after = client.get("/api/auth/me")

    assert me.status_code == 200
    assert changed.status_code == 200
    assert logout.status_code == 200
    assert after.status_code == 401


def test_users_api_crud_endpoints(client, app, seeded_state):
    _login(client, seeded_state, "admin")

    listed = client.get("/api/users")
    created = client.post(
        "/api/users",
        json={"username": "api-user", "password": "secret123", "role": "judge", "email": "api-user@example.com"},
    )
    user_id = created.get_json()["user"]["id"]
    fetched = client.get(f"/api/users/{user_id}")
    patched = client.patch(f"/api/users/{user_id}", json={"username": "api-user-2", "role": "viewer"})
    deleted = client.delete(f"/api/users/{user_id}")

    assert listed.status_code == 200
    assert created.status_code == 201
    assert fetched.status_code == 200
    assert patched.status_code == 200
    assert deleted.status_code == 200


def test_team_api_remaining_endpoints(client, app, seeded_state):
    _login(client, seeded_state, "admin")

    fetched = client.get(f"/api/teams/{seeded_state.team_id}")
    patched = client.patch(
        f"/api/teams/{seeded_state.team_id}",
        json={"organization": "Updated Org", "group_id": seeded_state.second_group_id},
    )
    put_team = create_team(db.session.get(Team, seeded_state.team_id).competition, name="Put Team", number=44)
    put_response = client.put(
        f"/api/teams/{put_team.id}",
        json={"name": "Put Team Updated", "number": 45, "organization": "Put Org", "group_id": seeded_state.group_id},
    )
    active_group = client.post(
        f"/api/teams/{seeded_state.team_id}/active-group",
        json={"group_id": seeded_state.group_id},
    )
    randomized = client.post("/api/teams/randomize", json={"group_id": seeded_state.group_id})
    delete_team = create_team(db.session.get(Team, seeded_state.team_id).competition, name="Delete Team", number=77)
    deleted = client.delete(f"/api/teams/{delete_team.id}", json={"force": True, "confirm_text": "Delete"})

    assert fetched.status_code == 200
    assert patched.status_code == 200
    assert put_response.status_code == 200
    assert active_group.status_code == 200
    assert randomized.status_code == 200
    assert deleted.status_code == 200


def test_group_api_remaining_endpoints(client, app, seeded_state):
    _login(client, seeded_state, "admin")

    fetched = client.get(f"/api/groups/{seeded_state.group_id}")
    patched = client.patch(
        f"/api/groups/{seeded_state.second_group_id}",
        json={"description": "Updated description", "checkpoint_ids": [seeded_state.second_checkpoint_id]},
    )
    ordered = client.post(
        "/api/groups/order",
        json={"group_ids": [seeded_state.second_group_id, seeded_state.group_id]},
    )
    delete_group = create_group(db.session.get(Team, seeded_state.team_id).competition, name="Delete Group", prefix="3xx")
    deleted = client.delete(f"/api/groups/{delete_group.id}")

    assert fetched.status_code == 200
    assert patched.status_code == 200
    assert ordered.status_code == 200
    assert deleted.status_code == 200


def test_checkpoint_api_remaining_endpoints(client, app, seeded_state):
    _login(client, seeded_state, "admin")

    fetched = client.get(f"/api/checkpoints/{seeded_state.checkpoint_id}")
    patched = client.patch(
        f"/api/checkpoints/{seeded_state.second_checkpoint_id}",
        json={"location": "Updated location", "group_ids": [seeded_state.group_id]},
    )
    put_response = client.put(
        f"/api/checkpoints/{seeded_state.third_checkpoint_id}",
        json={"name": "Delete CP Updated", "location": "Loc", "description": "Desc", "group_ids": [seeded_state.group_id]},
    )
    imported = client.post(
        "/api/checkpoints/import",
        json={"items": [{"name": "Imported CP", "location": "Import", "group_ids": [seeded_state.group_id]}]},
    )
    delete_checkpoint = create_checkpoint(db.session.get(Team, seeded_state.team_id).competition, name="Temp Delete CP")
    deleted = client.delete(f"/api/checkpoints/{delete_checkpoint.id}")

    assert fetched.status_code == 200
    assert patched.status_code == 200
    assert put_response.status_code == 200
    assert imported.status_code == 200
    assert deleted.status_code == 200


def test_rfid_api_remaining_endpoints(client, app, seeded_state, monkeypatch):
    _login(client, seeded_state, "admin")

    listed = client.get("/api/rfid/cards")
    fetched = client.get(f"/api/rfid/cards/{seeded_state.card_id}")
    patched = client.patch(f"/api/rfid/cards/{seeded_state.card_id}", json={"uid": "MATRIX01", "number": 101})
    imported = client.post(
        "/api/rfid/import",
        json={"rows": [{"uid": "NEWCARD01", "team_id": str(seeded_state.second_team_id), "number": "55"}]},
    )
    monkeypatch.setattr("app.resources.rfid.read_uid_once", lambda *args, **kwargs: "SCAN1234")
    scanned = client.post("/api/rfid/scan")
    delete_team = create_team(db.session.get(Team, seeded_state.team_id).competition, name="RFID Delete Team", number=123)
    delete_card = create_rfid_card(delete_team, uid="DELETE01")
    deleted = client.delete(f"/api/rfid/cards/{delete_card.id}")

    assert listed.status_code == 200
    assert fetched.status_code == 200
    assert patched.status_code == 200
    assert imported.status_code == 200
    assert scanned.status_code == 200
    assert deleted.status_code == 200


def test_device_message_and_map_alias_endpoints(client, app, seeded_state):
    _login(client, seeded_state, "admin")

    lora_alias = client.get("/api/lora/devices")
    device_item = client.get(f"/api/lora/devices/{seeded_state.device_id}")
    patched = client.patch(f"/api/devices/{seeded_state.device_id}", json={"name": "Renamed Device"})
    message_primary = client.get("/api/devices/messages")
    message_alias = client.get("/api/lora/messages")
    map_checkpoints = client.get(f"/api/map/checkpoints?team_id={seeded_state.team_id}")
    map_primary = client.get("/api/map/lora-points")
    map_alias = client.get("/api/map/device-points")
    delete_device = create_device(db.session.get(Team, seeded_state.team_id).competition, dev_num=88, name="Delete Device")
    deleted = client.delete(f"/api/devices/{delete_device.id}")

    assert lora_alias.status_code == 200
    assert device_item.status_code == 200
    assert patched.status_code == 200
    assert message_primary.status_code == 200
    assert message_alias.status_code == 200
    assert map_checkpoints.status_code == 200
    assert map_primary.status_code == 200
    assert map_alias.status_code == 200
    assert deleted.status_code == 200


def test_checkin_item_endpoints(client, app, seeded_state):
    _login(client, seeded_state, "admin")

    fetched = client.get(f"/api/checkins/{seeded_state.checkin_id}")
    patched = client.patch(
        f"/api/checkins/{seeded_state.checkin_id}",
        json={"checkpoint_id": seeded_state.second_checkpoint_id},
    )
    put_checkin = create_checkin(
        db.session.get(Team, seeded_state.team_id).competition,
        db.session.get(Team, seeded_state.second_team_id),
        db.session.get(Checkpoint, seeded_state.checkpoint_id),
    )
    put_response = client.put(
        f"/api/checkins/{put_checkin.id}",
        json={"team_id": seeded_state.second_team_id, "checkpoint_id": seeded_state.checkpoint_id},
    )
    delete_checkin = create_checkin(
        db.session.get(Team, seeded_state.team_id).competition,
        db.session.get(Team, seeded_state.second_team_id),
        db.session.get(Checkpoint, seeded_state.second_checkpoint_id),
    )
    deleted = client.delete(f"/api/checkins/{delete_checkin.id}")

    assert fetched.status_code == 200
    assert patched.status_code == 200
    assert put_response.status_code == 200
    assert deleted.status_code == 200


def test_score_api_and_rule_endpoints(client, app, seeded_state):
    _login(client, seeded_state, "admin")

    resolved = client.post(
        "/api/scores/resolve",
        json={"team_id": seeded_state.team_id, "checkpoint_id": seeded_state.checkpoint_id},
    )
    submitted = client.post(
        "/api/scores/submit",
        json={
            "team_id": seeded_state.team_id,
            "checkpoint_id": seeded_state.checkpoint_id,
            "fields": {"accuracy": 3, "dead_time": 1},
        },
    )
    listed = client.get("/api/score-rules")
    fields = client.get(
        f"/api/score-rules/fields?checkpoint_id={seeded_state.checkpoint_id}&group_id={seeded_state.group_id}"
    )
    updated_rule = client.post(
        "/api/score-rules",
        json={"checkpoint_id": seeded_state.checkpoint_id, "group_id": seeded_state.group_id, "rules": {"field_rules": {"accuracy": {"type": "multiplier", "factor": 3}}}},
    )
    delete_rule = client.delete(f"/api/score-rules/{seeded_state.score_rule_id}")

    assert resolved.status_code == 200
    assert submitted.status_code == 201
    assert listed.status_code == 200
    assert fields.status_code == 200
    assert updated_rule.status_code == 200
    assert delete_rule.status_code == 200
    assert ScoreEntry.query.count() >= 1


@pytest.mark.parametrize(
    "path_template",
    [
        "/docs/specs",
        "/docs/",
        "/users/",
        "/users/add",
        "/messages/",
        "/map/",
        "/map/devices",
        "/scores/judge",
        "/scores/rules",
        "/scores/view",
        "/scores/submissions",
        "/scores/stats",
        "/scores/public/{public_competition_id}",
        "/scores/public/{public_competition_id}/stats",
        "/sheets/",
        "/sheets/lang",
        "/audit/",
        "/checkins/import_json",
        "/checkpoints/import_json",
        "/create_admin",
    ],
)
def test_admin_remaining_html_get_endpoints(client, app, seeded_state, path_template):
    _login(client, seeded_state, "admin")
    path = path_template.format(**seeded_state.__dict__)
    response = client.get(path)
    assert response.status_code in (200, 302)


def test_admin_remaining_html_post_endpoints(client, app, seeded_state):
    _login(client, seeded_state, "admin")

    attach = client.post("/users/attach", data={"identifier": _user(seeded_state, seeded_state.extra_user_id).username, "role": "judge"})
    add_user = client.post("/users/add", data={"username": "html-added-user", "password": "secret123", "role": "viewer"})
    edit_user = client.post(
        f"/users/{seeded_state.extra_user_id}/edit",
        data={"username": "matrix-extra-edited", "role": "viewer", "new_password": "", "confirm_password": ""},
    )
    delete_user = client.post(f"/users/{seeded_state.extra_user_id}/delete")
    randomize = client.post("/teams/randomize", data={"group_id": str(seeded_state.group_id)})
    save_lang = client.post("/sheets/save-lang", data={"teams_tab": "Teams"})
    save_settings = client.post("/sheets/save-settings", data={"sheets_sync_enabled": ""})
    build_arrivals = client.post("/sheets/build-arrivals", data={"spreadsheet_id": "local:matrix", "tab_name": "Arrivals"})
    build_teams = client.post("/sheets/build-teams", data={"spreadsheet_id": "local:matrix", "tab_name": "Teams"})
    build_score = client.post("/sheets/build-score", data={"spreadsheet_id": "local:matrix", "tab_name": "Score"})
    wizard = client.post("/sheets/wizard/checkpoints", data={"spreadsheet_id": "local:matrix"})
    add_tab = client.post("/sheets/add-tab", data={"spreadsheet_id": "local:matrix", "tab_title": "CP Sheet"})
    global_rules = client.post("/scores/global-rules", data={"global_group_id": str(seeded_state.group_id), "global_found_enabled": "on", "global_found_points": "3"})
    delete_global_rule = client.post(f"/scores/global-rules/{seeded_state.global_rule_id}/delete")
    delete_score_rule = client.post(f"/scores/rules/{seeded_state.score_rule_id}/delete")

    for response in (
        attach,
        add_user,
        edit_user,
        delete_user,
        randomize,
        save_lang,
        save_settings,
        build_arrivals,
        build_teams,
        build_score,
        wizard,
        add_tab,
        global_rules,
        delete_global_rule,
        delete_score_rule,
    ):
        assert response.status_code in (200, 302)
