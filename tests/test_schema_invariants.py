"""Regression tests for schema invariants added in
e1f2a3b4c5d6_rfid_competition_scoped_uid_and_team_group_active_partial_unique.

Two model facts that are now enforced at the DB level:
  #6 RFID UID is unique per (competition, uid), not globally — so the
     same physical scout card can be reused across events.
  #7 At most one active TeamGroup per team — previously a comment-only
     invariant which the live arrivals view assumed by picking [0]."""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import TeamGroup
from tests.support import (
    add_membership,
    assign_team_group,
    create_competition,
    create_group,
    create_rfid_card,
    create_team,
    create_user,
    login_as,
)


def test_rfid_uid_can_repeat_across_competitions(client):
    """Same UID + different competition = two rows that both insert OK."""
    comp_a = create_competition(name="Year2025")
    comp_b = create_competition(name="Year2026")
    team_a = create_team(comp_a, name="A")
    team_b = create_team(comp_b, name="B")

    create_rfid_card(team_a, uid="SHAREDCARD")
    # Reusing the same physical card UID in a new competition must NOT
    # collide with the previous year's mapping.
    create_rfid_card(team_b, uid="SHAREDCARD")
    # No exception → invariant respected.


def test_rfid_uid_still_unique_within_a_competition(client):
    """Within one competition, the same UID still cannot map to two
    different teams."""
    comp = create_competition(name="UniqueWithin")
    team1 = create_team(comp, name="T1")
    team2 = create_team(comp, name="T2")

    create_rfid_card(team1, uid="ONLYHERE")
    with pytest.raises(IntegrityError):
        create_rfid_card(team2, uid="ONLYHERE")
    db.session.rollback()


def test_team_cannot_have_two_active_groups(client):
    """The partial unique index uq_team_group_one_active blocks a
    second TeamGroup row with active=True for the same team."""
    comp = create_competition(name="OneActive")
    team = create_team(comp, name="MultiGroup")
    g_a = create_group(comp, name="GroupA")
    g_b = create_group(comp, name="GroupB")

    assign_team_group(team, g_a, active=True)
    with pytest.raises(IntegrityError):
        assign_team_group(team, g_b, active=True)
    db.session.rollback()


def test_team_can_have_multiple_inactive_groups(client):
    """Inactive history rows must still be allowed — the index is
    partial (active = 1)."""
    comp = create_competition(name="HistoryOk")
    team = create_team(comp, name="Switcher")
    g_a = create_group(comp, name="GA")
    g_b = create_group(comp, name="GB")

    assign_team_group(team, g_a, active=False)
    assign_team_group(team, g_b, active=False)
    # Two inactive rows are fine.
    rows = TeamGroup.query.filter_by(team_id=team.id).all()
    assert len(rows) == 2
    assert all(not r.active for r in rows)


def test_active_group_switch_via_api_does_not_violate_constraint(client):
    """End-to-end: PATCH /api/teams/<id> changing group_id must not
    trip the new partial unique index, even when the team already has
    a different active group. The team_patch path must drop the old
    active row before inserting the new one."""
    user = create_user(username="grp-switch-admin", role="public")
    comp = create_competition(name="SwitchComp")
    add_membership(user, comp, role="admin")
    g_a = create_group(comp, name="From")
    g_b = create_group(comp, name="To")
    team = create_team(comp, name="Movable")
    assign_team_group(team, g_a, active=True)

    login_as(client, user, comp)
    resp = client.patch(
        f"/api/teams/{team.id}",
        json={"group_id": g_b.id},
    )
    assert resp.status_code == 200, resp.get_json()

    actives = TeamGroup.query.filter_by(team_id=team.id, active=True).all()
    assert len(actives) == 1
    assert actives[0].group_id == g_b.id
