"""Test suite 1: Negative numbers in all number inputs."""
from __future__ import annotations

import pytest

from tests.support import (
    add_membership,
    create_checkpoint,
    create_competition,
    create_device,
    create_group,
    create_team,
    create_user,
    login_as,
)


@pytest.fixture
def _seeded(app, client):
    """Create a competition with an admin user logged in."""
    user = create_user(username="neg-admin", role="admin")
    comp = create_competition(name="NegNum Race")
    add_membership(user, comp, role="admin")
    login_as(client, user, comp)
    return comp, user


class TestNegativeTeamNumber:
    def test_negative_team_number_rejected(self, client, _seeded):
        comp, _ = _seeded
        team = create_team(comp, name="T-neg", number=5)
        resp = client.patch(
            f"/api/teams/{team.id}",
            json={"number": -1},
        )
        assert resp.status_code == 400
        assert resp.status_code != 500  # no crash

        resp2 = client.patch(
            f"/api/teams/{team.id}",
            json={"number": -100},
        )
        assert resp2.status_code == 400

    def test_zero_team_number_rejected(self, client, _seeded):
        comp, _ = _seeded
        team = create_team(comp, name="T-zero")
        resp = client.patch(
            f"/api/teams/{team.id}",
            json={"number": 0},
        )
        assert resp.status_code == 400

    def test_non_integer_team_number_rejected(self, client, _seeded):
        comp, _ = _seeded
        team = create_team(comp, name="T-nan")

        for bad in ["abc", "1.5"]:
            resp = client.patch(
                f"/api/teams/{team.id}",
                json={"number": bad},
            )
            assert resp.status_code == 400, f"Expected 400 for number={bad!r}, got {resp.status_code}"

    def test_empty_team_number_clears(self, client, _seeded):
        """Empty string clears the team number (sets to None) — this is valid."""
        comp, _ = _seeded
        team = create_team(comp, name="T-clear", number=5)
        resp = client.patch(f"/api/teams/{team.id}", json={"number": ""})
        assert resp.status_code == 200

    def test_negative_team_number_on_create(self, client, _seeded):
        resp = client.post("/api/teams", json={"name": "BadTeam", "number": -5})
        assert resp.status_code == 400

    def test_positive_team_number_accepted(self, client, _seeded):
        resp = client.post("/api/teams", json={"name": "GoodTeam", "number": 42})
        assert resp.status_code == 201


class TestNegativeDeviceId:
    def test_negative_device_id_does_not_crash(self, client, _seeded):
        """Devices with negative dev_num should not cause a 500 error."""
        resp = client.post(
            "/api/lora/devices",
            json={"dev_num": -1, "name": "bad-dev"},
        )
        # May be accepted (no DB constraint on dev_num sign) or rejected — but never 500
        assert resp.status_code != 500


class TestNegativeCheckpointCoordinates:
    def test_negative_easting_northing_accepted_or_rejected(self, client, _seeded):
        """Negative coordinates may be valid depending on projection.
        Test that the API handles them without crashing (no 500)."""
        resp = client.post(
            "/api/checkpoints",
            json={"name": "NegCoord-CP", "easting": -100.5, "northing": -200.3},
        )
        # Should either succeed or return a clean 4xx — never 500
        assert resp.status_code != 500
