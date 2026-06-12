"""Timestamp parsing on the check-in API.

The Checkin.timestamp column stores naive UTC. The POST endpoint accepts
either a bare `timestamp` ISO string or `timestamp_local` + `timezone`.
Offset-bearing ISO strings (e.g. "...+02:00") must be converted to UTC
before the offset is dropped, not stored verbatim.
"""

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


def _setup(client):
    user = create_user(username=None, role="public")
    comp = create_competition(name=None)
    add_membership(user, comp, role="admin")
    team = create_team(comp, number=1)
    checkpoint = create_checkpoint(comp)
    login_as(client, user, comp)
    return comp, team, checkpoint


def _post_checkin(client, team, checkpoint, **extra):
    return client.post(
        "/api/checkins",
        json={"team_id": team.id, "checkpoint_id": checkpoint.id, **extra},
    )


def _stored_timestamp(comp, team, checkpoint) -> datetime:
    checkin = Checkin.query.filter_by(
        competition_id=comp.id, team_id=team.id, checkpoint_id=checkpoint.id
    ).one()
    return checkin.timestamp


def test_offset_bearing_timestamp_is_converted_to_utc(client):
    comp, team, checkpoint = _setup(client)

    resp = _post_checkin(client, team, checkpoint, timestamp="2026-05-29T04:36:00+02:00")
    assert resp.status_code == 201

    stored = _stored_timestamp(comp, team, checkpoint)
    assert stored == datetime(2026, 5, 29, 2, 36, 0)
    assert stored.tzinfo is None


def test_naive_timestamp_is_stored_unchanged(client):
    comp, team, checkpoint = _setup(client)

    resp = _post_checkin(client, team, checkpoint, timestamp="2026-05-29T04:36:00")
    assert resp.status_code == 201

    stored = _stored_timestamp(comp, team, checkpoint)
    assert stored == datetime(2026, 5, 29, 4, 36, 0)
    assert stored.tzinfo is None


def test_timestamp_local_with_timezone_field_still_works(client):
    comp, team, checkpoint = _setup(client)

    # Europe/Ljubljana is UTC+2 in late May (CEST).
    resp = _post_checkin(
        client,
        team,
        checkpoint,
        timestamp_local="2026-05-29T04:36",
        timezone="Europe/Ljubljana",
    )
    assert resp.status_code == 201

    stored = _stored_timestamp(comp, team, checkpoint)
    assert stored == datetime(2026, 5, 29, 2, 36, 0)
    assert stored.tzinfo is None
