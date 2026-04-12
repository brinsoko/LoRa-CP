"""Test suite 4: Export/Import/Merge."""
from __future__ import annotations

import json
import io

import pytest

from app.extensions import db
from app.models import Competition, Team
from tests.support import (
    add_membership,
    create_checkpoint,
    create_checkin,
    create_competition,
    create_group,
    create_team,
    create_user,
    assign_team_group,
    login_as,
)


@pytest.fixture
def _seeded(app, client):
    user = create_user(username="export-admin", role="admin")
    comp = create_competition(name="Export Race")
    add_membership(user, comp, role="admin")

    group = create_group(comp, name="Grp-A", prefix="3xx")
    cp = create_checkpoint(comp, name="CP-Export-1")

    t1 = create_team(comp, name="Team-Alpha", number=301)
    t2 = create_team(comp, name="Team-Beta", number=302)
    assign_team_group(t1, group)
    assign_team_group(t2, group)

    create_checkin(comp, t1, cp)

    login_as(client, user, comp)
    return comp, user, group, cp, t1, t2


class TestExport:
    def test_export_returns_valid_json(self, client, _seeded):
        comp, *_ = _seeded
        resp = client.get(f"/api/competition/{comp.id}/export")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["schema_version"] == "1.0.0"
        assert "competition" in data
        assert "teams" in data
        assert "groups" in data
        assert "checkpoints" in data
        assert "checkins" in data
        assert len(data["teams"]) == 2
        assert len(data["groups"]) == 1
        assert len(data["checkpoints"]) == 1

    def test_export_admin_only(self, app, client):
        viewer = create_user(username="viewer-exp", role="public")
        comp = create_competition(name="Viewer Comp")
        add_membership(viewer, comp, role="viewer")
        login_as(client, viewer, comp)

        resp = client.get(f"/api/competition/{comp.id}/export")
        assert resp.status_code == 403


class TestImport:
    def test_import_creates_new_competition(self, client, _seeded):
        comp, *_ = _seeded
        # Export first
        export_resp = client.get(f"/api/competition/{comp.id}/export")
        export_data = export_resp.get_json()

        # Import
        resp = client.post(
            "/api/competition/import",
            json=export_data,
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["ok"] is True
        new_id = data["competition_id"]
        assert new_id != comp.id

        # Verify the new competition has the same number of teams
        new_comp = db.session.get(Competition, new_id)
        assert new_comp is not None
        new_teams = Team.query.filter_by(competition_id=new_id).count()
        assert new_teams == 2

    def test_import_round_trip_data_integrity(self, client, _seeded):
        comp, *_ = _seeded
        export1 = client.get(f"/api/competition/{comp.id}/export").get_json()

        # Import
        import_resp = client.post("/api/competition/import", json=export1)
        new_id = import_resp.get_json()["competition_id"]

        # Switch to new competition and export again
        with client.session_transaction() as sess:
            sess["competition_id"] = new_id
        export2 = client.get(f"/api/competition/{new_id}/export").get_json()

        # Compare semantically (ignore IDs and timestamps)
        assert len(export1["teams"]) == len(export2["teams"])
        assert len(export1["groups"]) == len(export2["groups"])
        assert len(export1["checkpoints"]) == len(export2["checkpoints"])

        names1 = sorted(t["name"] for t in export1["teams"])
        names2 = sorted(t["name"] for t in export2["teams"])
        assert names1 == names2

    def test_import_invalid_json_rejected(self, client, _seeded):
        # Malformed JSON via file upload
        bad_file = io.BytesIO(b"not json at all {{{")
        resp = client.post(
            "/api/competition/import",
            data={"file": (bad_file, "bad.json")},
            content_type="multipart/form-data",
        )
        assert resp.status_code == 400

    def test_import_missing_fields_rejected(self, client, _seeded):
        resp = client.post(
            "/api/competition/import",
            json={"schema_version": "1.0.0"},  # missing competition, teams, etc.
        )
        assert resp.status_code == 400

    def test_import_version_mismatch_warning(self, client, _seeded):
        comp, *_ = _seeded
        export_data = client.get(f"/api/competition/{comp.id}/export").get_json()
        export_data["schema_version"] = "99.0.0"

        resp = client.post("/api/competition/import", json=export_data)
        assert resp.status_code == 201
        data = resp.get_json()
        assert any("mismatch" in w.lower() for w in data.get("warnings", []))


class TestMerge:
    def test_merge_no_conflicts(self, client, _seeded):
        comp, *_ = _seeded
        export_data = client.get(f"/api/competition/{comp.id}/export").get_json()

        # Add a new team to the export data
        export_data["teams"].append({"name": "Team-New", "number": 999})
        export_data["resolutions"] = {}

        resp = client.post(
            f"/api/competition/{comp.id}/merge",
            json=export_data,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["dry_run"] is False
        assert data["summary"]["added"]["teams"] >= 1

    def test_merge_dry_run_detects_conflicts(self, client, _seeded):
        comp, _, _, _, t1, _ = _seeded
        export_data = client.get(f"/api/competition/{comp.id}/export").get_json()

        # Modify a team name but keep the same natural key (name)
        for t in export_data["teams"]:
            if t["name"] == "Team-Alpha":
                t["number"] = 999  # different number

        # Dry run (no resolutions key)
        resp = client.post(
            f"/api/competition/{comp.id}/merge",
            json=export_data,
        )
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["dry_run"] is True
        assert len(data["conflicts"]) >= 1
        conflict = data["conflicts"][0]
        assert conflict["entity_type"] == "team"
        assert conflict["identifier"] == "Team-Alpha"

    def test_merge_apply_keep_local(self, client, _seeded):
        comp, _, _, _, t1, _ = _seeded
        export_data = client.get(f"/api/competition/{comp.id}/export").get_json()

        for t in export_data["teams"]:
            if t["name"] == "Team-Alpha":
                t["number"] = 999

        export_data["resolutions"] = {"team:Team-Alpha": "keep_local"}

        resp = client.post(f"/api/competition/{comp.id}/merge", json=export_data)
        assert resp.status_code == 200
        # Team-Alpha should still have original number
        refreshed = Team.query.get(t1.id)
        assert refreshed.number == 301

    def test_merge_apply_use_imported(self, client, _seeded):
        comp, _, _, _, t1, _ = _seeded
        export_data = client.get(f"/api/competition/{comp.id}/export").get_json()

        for t in export_data["teams"]:
            if t["name"] == "Team-Alpha":
                t["number"] = 999

        export_data["resolutions"] = {"team:Team-Alpha": "use_imported"}

        resp = client.post(f"/api/competition/{comp.id}/merge", json=export_data)
        assert resp.status_code == 200
        refreshed = Team.query.get(t1.id)
        assert refreshed.number == 999

    def test_merge_apply_skip(self, client, _seeded):
        comp, _, _, _, t1, _ = _seeded
        original_number = t1.number
        export_data = client.get(f"/api/competition/{comp.id}/export").get_json()

        for t in export_data["teams"]:
            if t["name"] == "Team-Alpha":
                t["number"] = 999

        export_data["resolutions"] = {"team:Team-Alpha": "skip"}

        resp = client.post(f"/api/competition/{comp.id}/merge", json=export_data)
        assert resp.status_code == 200
        refreshed = Team.query.get(t1.id)
        assert refreshed.number == original_number

    def test_merge_version_mismatch_warning(self, client, _seeded):
        comp, *_ = _seeded
        export_data = client.get(f"/api/competition/{comp.id}/export").get_json()
        export_data["schema_version"] = "99.0.0"

        # Dry run
        resp = client.post(f"/api/competition/{comp.id}/merge", json=export_data)
        assert resp.status_code == 200
        data = resp.get_json()
        assert any("mismatch" in w.lower() for w in data.get("warnings", []))

    def test_merge_admin_only(self, app, client):
        viewer = create_user(username="viewer-merge", role="public")
        comp = create_competition(name="Viewer Merge")
        add_membership(viewer, comp, role="viewer")
        login_as(client, viewer, comp)

        resp = client.post(
            f"/api/competition/{comp.id}/merge",
            json={"schema_version": "1.0.0", "competition": {"name": "x"}, "teams": [], "groups": [], "checkpoints": []},
        )
        assert resp.status_code == 403
