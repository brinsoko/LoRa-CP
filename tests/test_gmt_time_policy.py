from __future__ import annotations

from datetime import datetime

from app.models import Checkin
from tests.support import (
    add_membership,
    create_checkpoint,
    create_competition,
    create_team,
    create_user,
    login_as,
)


def test_checkin_forms_use_gmt_time_policy(client, app):
    admin = create_user(username="gmt-admin")
    competition = create_competition(name="GMT Race")
    add_membership(admin, competition, role="admin")
    login_as(client, admin, competition)
    team = create_team(competition, name="GMT Team", number=1)
    checkpoint = create_checkpoint(competition, name="GMT Start")

    add_page = client.get("/checkins/add")
    add_html = add_page.data.decode("utf-8", errors="replace")
    assert add_page.status_code == 200
    assert 'name="timezone" value="UTC"' in add_html
    assert 'value="GMT" disabled' in add_html
    assert "Europe/Ljubljana" not in add_html

    response = client.post(
        "/checkins/add",
        data={
            "team_id": str(team.id),
            "checkpoint_id": str(checkpoint.id),
            "timestamp_local": "2026-05-08T12:00:00",
            "timezone": "UTC",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302

    checkin = Checkin.query.filter_by(team_id=team.id, checkpoint_id=checkpoint.id).one()
    assert checkin.timestamp == datetime(2026, 5, 8, 12, 0, 0)

    edit_page = client.get(f"/checkins/{checkin.id}/edit")
    edit_html = edit_page.data.decode("utf-8", errors="replace")
    assert edit_page.status_code == 200
    assert 'value="2026-05-08T12:00:00"' in edit_html
    assert 'name="timezone" value="UTC"' in edit_html
    assert 'value="GMT" disabled' in edit_html
