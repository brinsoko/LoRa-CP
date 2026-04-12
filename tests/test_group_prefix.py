"""Test suite 2: Group prefix validation."""
from __future__ import annotations

import pytest

from tests.support import (
    add_membership,
    assign_team_group,
    create_competition,
    create_group,
    create_team,
    create_user,
    login_as,
)


@pytest.fixture
def _seeded(app, client):
    user = create_user(username="prefix-admin", role="admin")
    comp = create_competition(name="Prefix Race")
    add_membership(user, comp, role="admin")
    login_as(client, user, comp)
    return comp, user


class TestDuplicatePrefix:
    def test_duplicate_prefix_rejected(self, client, _seeded):
        comp, _ = _seeded
        resp1 = client.post("/api/groups", json={"name": "G1", "prefix": "3xx"})
        assert resp1.status_code == 201

        resp2 = client.post("/api/groups", json={"name": "G2", "prefix": "3xx"})
        assert resp2.status_code == 409


class TestPrefixOverlap:
    def test_prefix_within_prefix_rejected_inner(self, client, _seeded):
        """1xxx exists → reject 1xx (digit '1' is prefix of '1')."""
        resp1 = client.post("/api/groups", json={"name": "Wide", "prefix": "1xxx"})
        assert resp1.status_code == 201

        resp2 = client.post("/api/groups", json={"name": "Narrow", "prefix": "1xx"})
        assert resp2.status_code in (400, 409)

    def test_prefix_within_prefix_rejected_outer(self, client, _seeded):
        """1xx exists → reject 1xxx."""
        resp1 = client.post("/api/groups", json={"name": "Narrow", "prefix": "1xx"})
        assert resp1.status_code == 201

        resp2 = client.post("/api/groups", json={"name": "Wide", "prefix": "1xxx"})
        assert resp2.status_code in (400, 409)


class TestInvalidPrefixFormat:
    @pytest.mark.parametrize("bad_prefix", [
        "abc", "xx3", "3x3x", "---", "", "x", "3", "xxx",
    ])
    def test_invalid_prefix_format_rejected(self, client, _seeded, bad_prefix):
        resp = client.post(
            "/api/groups",
            json={"name": f"Bad-{bad_prefix or 'empty'}", "prefix": bad_prefix},
        )
        # Empty prefix is allowed (means no prefix)
        if bad_prefix == "":
            assert resp.status_code == 201
        else:
            assert resp.status_code == 400, f"Expected 400 for prefix={bad_prefix!r}, got {resp.status_code}"


class TestLeadingZeroPrefix:
    def test_leading_zero_prefix_01xx(self, client, _seeded):
        resp = client.post("/api/groups", json={"name": "G-01xx", "prefix": "01xx"})
        assert resp.status_code == 201

    def test_leading_zero_prefix_02xx(self, client, _seeded):
        resp = client.post("/api/groups", json={"name": "G-02xx", "prefix": "02xx"})
        assert resp.status_code == 201

    def test_leading_zero_prefix_10xx(self, client, _seeded):
        resp = client.post("/api/groups", json={"name": "G-10xx", "prefix": "10xx"})
        assert resp.status_code == 201

    def test_leading_zero_range_boundary(self, client, _seeded):
        """01xx with 2 teams → range 101-102.
        Team 101 is in range (kept), team 102 is in range (kept) → no_op."""
        comp, _ = _seeded
        group = create_group(comp, name="BoundaryGrp", prefix="01xx")

        t_in_low = create_team(comp, name="T-101", number=101)
        assign_team_group(t_in_low, group)

        t_in_high = create_team(comp, name="T-102", number=102)
        assign_team_group(t_in_high, group)

        # Both teams have numbers in the 2-team range (101-102) → no_op
        resp = client.post("/api/teams/randomize", json={"group_id": group.id})
        assert resp.status_code == 200
        data = resp.get_json()
        for r in data.get("results", []):
            if r.get("group_id") == group.id:
                assert r["status"] == "no_op"


class TestPrefixNoDifferentCompetitionConflict:
    def test_prefix_no_conflict_different_competition(self, app, client):
        """Two groups in DIFFERENT competitions can have the same prefix."""
        user = create_user(username="multi-comp-admin", role="admin")
        comp_a = create_competition(name="CompA-prefix")
        comp_b = create_competition(name="CompB-prefix")
        add_membership(user, comp_a, role="admin")
        add_membership(user, comp_b, role="admin")

        # Create group in comp A
        login_as(client, user, comp_a)
        resp1 = client.post("/api/groups", json={"name": "G-same", "prefix": "3xx"})
        assert resp1.status_code == 201

        # Create group with same prefix in comp B — should succeed
        login_as(client, user, comp_b)
        resp2 = client.post("/api/groups", json={"name": "G-same", "prefix": "3xx"})
        assert resp2.status_code == 201


class TestTeamNumberOutsidePrefixOnRandomise:
    def test_team_outside_prefix_gets_new_number(self, client, _seeded):
        """Team with number 500 in a 3xx group needs a new number when randomised."""
        comp, _ = _seeded
        group = create_group(comp, name="Renum-Grp", prefix="3xx")
        t = create_team(comp, name="T-outside", number=500)
        assign_team_group(t, group)

        resp = client.post("/api/teams/randomize", json={"group_id": group.id})
        assert resp.status_code == 200
        data = resp.get_json()
        assert data.get("assigned_total", 0) >= 1

        # Verify team got a number in range 301-301 (only 1 team)
        from app.models import Team
        refreshed = Team.query.get(t.id)
        assert refreshed.number == 301
