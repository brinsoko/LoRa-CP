from __future__ import annotations

from datetime import datetime, timedelta

from app.extensions import db
from tests.support import (
    add_membership,
    assign_team_group,
    create_checkin,
    create_checkpoint,
    create_competition,
    create_group,
    create_team,
    create_user,
    login_as,
    set_group_route,
)


def _seed_live_arrivals(client):
    user = create_user(username="live-admin")
    competition = create_competition(name="Live Arrivals Race")
    add_membership(user, competition, role="admin")
    login_as(client, user, competition)

    group = create_group(competition, name="Category A")
    start = create_checkpoint(competition, name="Start")
    middle = create_checkpoint(competition, name="CP 1")
    finish = create_checkpoint(competition, name="Finish")
    set_group_route(group, [start, middle, finish])

    alpha = create_team(competition, name="Alpha", number=1)
    bravo = create_team(competition, name="Bravo", number=2)
    charlie = create_team(competition, name="Charlie", number=3)
    delta = create_team(competition, name="Delta", number=4)
    for team in (alpha, bravo, charlie, delta):
        assign_team_group(team, group)

    t0 = datetime(2026, 5, 8, 8, 0, 0)
    create_checkin(competition, alpha, start, timestamp=t0)
    create_checkin(competition, alpha, middle, timestamp=t0 + timedelta(minutes=30))
    create_checkin(competition, alpha, finish, timestamp=t0 + timedelta(minutes=70))
    create_checkin(competition, bravo, start, timestamp=t0 + timedelta(minutes=5))
    create_checkin(competition, delta, start, timestamp=t0 + timedelta(minutes=10))
    delta.dnf = True
    db.session.commit()

    return {
        "competition": competition,
        "group": group,
        "start": start,
        "finish": finish,
        "alpha": alpha,
        "bravo": bravo,
        "charlie": charlie,
        "delta": delta,
    }


def test_live_arrivals_api_counts_expected_arrivals_and_team_timing(client, app):
    seeded = _seed_live_arrivals(client)

    response = client.get("/api/checkins/live-arrivals")
    body = response.get_json()

    assert response.status_code == 200
    assert body["summary"]["teams_count"] == 4
    assert body["summary"]["started_count"] == 3
    assert body["summary"]["finished_count"] == 1
    assert body["summary"]["not_finished_count"] == 2
    assert body["summary"]["dnf_count"] == 1
    assert body["summary"]["checkins_count"] == 5

    checkpoints = {row["id"]: row for row in body["checkpoints"]}
    assert checkpoints[seeded["start"].id]["expected_count"] == 4
    assert checkpoints[seeded["start"].id]["arrived_count"] == 3
    assert checkpoints[seeded["start"].id]["missing_count"] == 1
    assert checkpoints[seeded["finish"].id]["expected_count"] == 4
    assert checkpoints[seeded["finish"].id]["arrived_count"] == 1
    assert checkpoints[seeded["finish"].id]["missing_count"] == 3

    teams = {row["id"]: row for row in body["teams"]}
    assert teams[seeded["alpha"].id]["status"] == "finished"
    assert teams[seeded["alpha"].id]["elapsed_minutes"] == 70
    assert teams[seeded["bravo"].id]["status"] == "on_course"
    assert teams[seeded["charlie"].id]["status"] == "not_started"
    assert teams[seeded["delta"].id]["status"] == "dnf"


def test_live_arrivals_filters_by_group(client, app):
    seeded = _seed_live_arrivals(client)
    competition = seeded["competition"]
    second_group = create_group(competition, name="Category B")
    second_start = create_checkpoint(competition, name="B Start")
    second_finish = create_checkpoint(competition, name="B Finish")
    set_group_route(second_group, [second_start, second_finish])
    echo = create_team(competition, name="Echo", number=5)
    assign_team_group(echo, second_group)
    create_checkin(competition, echo, second_start, timestamp=datetime(2026, 5, 8, 9, 0, 0))
    create_checkin(competition, echo, second_finish, timestamp=datetime(2026, 5, 8, 10, 0, 0))

    first_response = client.get(f"/api/checkins/live-arrivals?group_id={seeded['group'].id}")
    first_body = first_response.get_json()
    second_response = client.get(f"/api/checkins/live-arrivals?group_id={second_group.id}")
    second_body = second_response.get_json()

    assert first_response.status_code == 200
    assert first_body["filters"]["group_id"] == seeded["group"].id
    assert first_body["summary"]["teams_count"] == 4
    assert first_body["summary"]["checkins_count"] == 5
    assert [row["name"] for row in first_body["checkpoints"]] == ["Start", "CP 1", "Finish"]

    assert second_response.status_code == 200
    assert second_body["filters"]["group_id"] == second_group.id
    assert second_body["summary"]["teams_count"] == 1
    assert second_body["summary"]["finished_count"] == 1
    assert second_body["summary"]["checkins_count"] == 2
    assert [row["name"] for row in second_body["checkpoints"]] == ["B Start", "B Finish"]


def test_live_arrivals_page_renders_dashboard(client, app):
    _seed_live_arrivals(client)

    response = client.get("/checkins/live")
    html = response.data.decode("utf-8", errors="replace")

    assert response.status_code == 200
    assert "Live Arrivals" in html
    assert "Checkpoint arrivals" in html
    assert "Team timing" in html
    assert "Alpha" in html


def test_live_arrivals_is_limited_to_judges_and_admins(client, app):
    viewer = create_user(username="live-viewer")
    competition = create_competition(name="Viewer Live Race")
    add_membership(viewer, competition, role="viewer")
    login_as(client, viewer, competition)

    page_response = client.get("/checkins/live")
    api_response = client.get("/api/checkins/live-arrivals")

    assert page_response.status_code == 403
    assert api_response.status_code == 403
