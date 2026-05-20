"""Test suite 4: Export/Import/Merge."""

from __future__ import annotations

import io

import pytest

from app.extensions import db
from app.models import (
    CheckpointGroup,
    CheckpointGroupLink,
    Competition,
    GlobalScoreRule,
    LoRaDevice,
    RFIDCard,
    ScoreEntry,
    ScoreRule,
    SheetConfig,
    Team,
    TeamGroup,
)
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

    def test_import_round_trips_scoring_fields(self, client, _seeded):
        """Per-checkpoint scoring layout (SheetConfig.config) must survive an
        export -> import cycle. Without this the destination installation has
        no field list, no headers, and no group mapping for the score UI."""
        comp, _, group, cp, _, _ = _seeded
        db.session.add(
            SheetConfig(
                competition_id=comp.id,
                spreadsheet_id="local:src",
                spreadsheet_name="Local",
                tab_name=cp.name,
                tab_type="checkpoint",
                checkpoint_id=cp.id,
                config={
                    "arrived_header": "Arrived",
                    "points_header": "Points",
                    "dead_time_header": "Dead",
                    "dead_time_enabled": True,
                    "time_header": "Time",
                    "time_enabled": False,
                    "groups": [
                        {
                            "group_id": group.id,
                            "name": group.name,
                            "fields": ["task1", "task2", "topo"],
                        }
                    ],
                },
            )
        )
        db.session.commit()

        export = client.get(f"/api/competition/{comp.id}/export").get_json()
        assert "sheet_configs" in export
        assert len(export["sheet_configs"]) == 1
        assert export["sheet_configs"][0]["config"]["groups"][0]["fields"] == [
            "task1",
            "task2",
            "topo",
        ]

        export["competition"]["name"] = "Imported Scoring"
        resp = client.post("/api/competition/import", json=export)
        assert resp.status_code == 201
        new_id = resp.get_json()["competition_id"]

        new_configs = SheetConfig.query.filter_by(competition_id=new_id).all()
        assert len(new_configs) == 1
        cfg = new_configs[0]
        # Spreadsheet ID was rewritten to the local-only form so the import
        # never grants implicit write access to the source's Google Sheet.
        assert cfg.spreadsheet_id == f"local:{new_id}"
        # Scoring fields preserved.
        assert cfg.config["groups"][0]["fields"] == ["task1", "task2", "topo"]
        # group_id remapped to the destination competition's group.
        new_group = next(
            g for g in cfg.config["groups"] if g["name"] == group.name
        )
        local_group = CheckpointGroup.query.filter_by(
            competition_id=new_id, name=group.name
        ).first()
        assert local_group is not None
        assert new_group["group_id"] == local_group.id

    def test_import_round_trips_score_rules_and_score_metadata(self, client, _seeded):
        """ScoreRule, GlobalScoreRule, and ScoreEntry.judge_user_id +
        created_at must all round-trip through export+import."""
        comp, user, group, cp, t1, _ = _seeded
        from datetime import datetime

        db.session.add(
            ScoreRule(
                competition_id=comp.id,
                checkpoint_id=cp.id,
                group_id=group.id,
                rules={"field_rules": {"task1": {"type": "multiplier", "factor": 4}}},
            )
        )
        db.session.add(
            GlobalScoreRule(
                competition_id=comp.id,
                group_id=group.id,
                rules={"time": {"max_points": 50}},
            )
        )
        # Seed a ScoreEntry with a judge + a known created_at so we can
        # assert both round-trip.
        scored_at = datetime(2026, 5, 20, 9, 30, 0)
        db.session.add(
            ScoreEntry(
                competition_id=comp.id,
                team_id=t1.id,
                checkpoint_id=cp.id,
                judge_user_id=user.id,
                raw_fields={"task1": 8},
                total=32.0,
                created_at=scored_at,
            )
        )
        db.session.commit()

        export = client.get(f"/api/competition/{comp.id}/export").get_json()
        assert "score_rules" in export and len(export["score_rules"]) == 1
        assert export["score_rules"][0]["rules"]["field_rules"]["task1"]["factor"] == 4
        assert "global_score_rules" in export and len(export["global_score_rules"]) == 1
        # Score export now carries judge + created_at.
        landed_score = next(
            s for s in export["scores"] if s["team_name"] == t1.name and s["checkpoint_name"] == cp.name
        )
        assert landed_score["judge_username"] == user.username
        assert landed_score["created_at"].startswith("2026-05-20T09:30:00")

        export["competition"]["name"] = "Imported With Rules"
        resp = client.post("/api/competition/import", json=export)
        assert resp.status_code == 201
        new_id = resp.get_json()["competition_id"]

        new_rule = ScoreRule.query.filter_by(competition_id=new_id).first()
        assert new_rule is not None
        assert new_rule.rules["field_rules"]["task1"]["factor"] == 4
        new_gr = GlobalScoreRule.query.filter_by(competition_id=new_id).first()
        assert new_gr is not None
        assert new_gr.rules["time"]["max_points"] == 50

        # Judge attribution + created_at preserved on the imported score.
        new_score = ScoreEntry.query.filter_by(competition_id=new_id).first()
        assert new_score is not None
        assert new_score.judge_user_id == user.id, "judge attribution lost on import"
        assert new_score.created_at == scored_at, (
            f"created_at not preserved: got {new_score.created_at}, expected {scored_at}"
        )

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

    def test_merge_syncs_scores_without_sheets_writes(self, client, _seeded, monkeypatch):
        """Score entries land via /merge and the Sheets sync helpers are
        never invoked. A merge into the local DB must not require sheets
        write permission to succeed.
        """
        comp, _, _, cp, t1, _ = _seeded
        export_data = client.get(f"/api/competition/{comp.id}/export").get_json()

        # Inject a synthetic score on a team+checkpoint that doesn't have one
        # locally yet. The export payload may already contain scores; replace
        # with a single known one for a clean assertion.
        export_data["scores"] = [
            {
                "team_name": t1.name,
                "checkpoint_name": cp.name,
                "raw_fields": {"task": 17, "points": 17},
                "total": 17.0,
            }
        ]
        export_data["resolutions"] = {}

        # Guard: neither sheets helper should be invoked during a local merge.
        from app.utils import sheets_sync

        def fail_if_called(*args, **kwargs):
            raise AssertionError("Sheets sync must not run during /merge")

        monkeypatch.setattr(sheets_sync, "mark_arrival_checkbox", fail_if_called)
        monkeypatch.setattr(sheets_sync, "update_checkpoint_scores", fail_if_called)

        resp = client.post(f"/api/competition/{comp.id}/merge", json=export_data)
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["ok"] is True
        assert data["summary"]["added"]["scores"] == 1

        landed = ScoreEntry.query.filter_by(
            competition_id=comp.id, team_id=t1.id, checkpoint_id=cp.id
        ).all()
        assert len(landed) == 1
        assert landed[0].total == pytest.approx(17.0)
        assert landed[0].raw_fields == {"task": 17, "points": 17}

    def test_merge_skips_score_when_already_present(self, client, _seeded):
        """Re-merging the same payload doesn't duplicate scores."""
        comp, _, _, cp, t1, _ = _seeded
        export_data = client.get(f"/api/competition/{comp.id}/export").get_json()
        export_data["scores"] = [
            {
                "team_name": t1.name,
                "checkpoint_name": cp.name,
                "raw_fields": {"points": 9},
                "total": 9.0,
            }
        ]
        export_data["resolutions"] = {}

        first = client.post(f"/api/competition/{comp.id}/merge", json=export_data)
        assert first.get_json()["summary"]["added"]["scores"] == 1

        second = client.post(f"/api/competition/{comp.id}/merge", json=export_data)
        assert second.get_json()["summary"]["added"]["scores"] == 0

        rows = ScoreEntry.query.filter_by(
            competition_id=comp.id, team_id=t1.id, checkpoint_id=cp.id
        ).count()
        assert rows == 1

    def test_merge_syncs_sheet_configs_locally(self, client, _seeded):
        """Per-checkpoint scoring layouts must also land via /merge, with
        spreadsheet_id rewritten to the local-only form so the merge does
        not require Google Sheets write permission on the destination."""
        comp, _, group, cp, _, _ = _seeded
        export_data = client.get(f"/api/competition/{comp.id}/export").get_json()
        export_data["sheet_configs"] = [
            {
                "tab_name": cp.name,
                "tab_type": "checkpoint",
                "checkpoint_name": cp.name,
                "config": {
                    "arrived_header": "Arr",
                    "points_header": "Pts",
                    "dead_time_header": "DT",
                    "dead_time_enabled": True,
                    "time_header": "T",
                    "time_enabled": False,
                    # group_id is stale (from another comp); merge must remap.
                    "groups": [
                        {"group_id": 99999, "name": group.name, "fields": ["taskX"]}
                    ],
                },
            }
        ]
        export_data["resolutions"] = {}

        resp = client.post(f"/api/competition/{comp.id}/merge", json=export_data)
        assert resp.status_code == 200
        body = resp.get_json()
        assert body["summary"]["added"]["sheet_configs"] == 1

        cfg = SheetConfig.query.filter_by(competition_id=comp.id, tab_name=cp.name).first()
        assert cfg is not None
        assert cfg.spreadsheet_id == f"local:{comp.id}"
        assert cfg.config["groups"][0]["fields"] == ["taskX"]
        # Stale group_id remapped to the local group with the same name.
        assert cfg.config["groups"][0]["group_id"] == group.id

        # Re-merge does not duplicate.
        resp2 = client.post(f"/api/competition/{comp.id}/merge", json=export_data)
        assert resp2.get_json()["summary"]["added"]["sheet_configs"] == 0
        assert SheetConfig.query.filter_by(competition_id=comp.id).count() == 1

    def test_merge_carries_team_group_assignments(self, client, _seeded):
        """Teams brought in via /merge must also get their group memberships.
        Without this, imported teams show up orphaned in the scoring UI
        because there's no TeamGroup row tying them to a category."""
        comp, _, group, _, _, _ = _seeded
        export_data = client.get(f"/api/competition/{comp.id}/export").get_json()

        # Add a brand-new team plus its team_groups entry, simulating the
        # shape /api/competition/<id>/export emits.
        export_data["teams"].append(
            {"name": "Team-Imported", "number": 909, "organization": "Org-X"}
        )
        export_data["team_groups"] = list(export_data.get("team_groups", [])) + [
            {"team_name": "Team-Imported", "group_name": group.name, "active": True},
        ]
        export_data["resolutions"] = {}

        resp = client.post(f"/api/competition/{comp.id}/merge", json=export_data)
        assert resp.status_code == 200
        summary = resp.get_json()["summary"]
        # At least the new team's link must land. The two existing teams
        # may also be present in the payload — those re-add as no-ops.
        assert summary["added"]["team_groups"] >= 1

        new_team = Team.query.filter_by(competition_id=comp.id, name="Team-Imported").first()
        assert new_team is not None, "Team did not land via merge"
        link = TeamGroup.query.filter_by(team_id=new_team.id, group_id=group.id).first()
        assert link is not None, "Team-Group assignment did not land via merge"
        assert link.active is True

    def test_merge_team_groups_are_idempotent(self, client, _seeded):
        """Re-merging the same payload does not create duplicate
        TeamGroup rows for teams that are already in the group."""
        comp, _, group, _, t1, _ = _seeded
        export_data = client.get(f"/api/competition/{comp.id}/export").get_json()
        # The fixture already wires t1 -> group; the export carries that
        # row. A second merge must skip it.
        assert any(
            tg["team_name"] == t1.name and tg["group_name"] == group.name
            for tg in export_data["team_groups"]
        )
        export_data["resolutions"] = {}

        before = TeamGroup.query.filter_by(team_id=t1.id, group_id=group.id).count()
        first = client.post(f"/api/competition/{comp.id}/merge", json=export_data)
        second = client.post(f"/api/competition/{comp.id}/merge", json=export_data)
        assert first.status_code == 200 and second.status_code == 200

        after = TeamGroup.query.filter_by(team_id=t1.id, group_id=group.id).count()
        assert after == before, f"Re-merge duplicated TeamGroup row: {before} -> {after}"

    def test_merge_carries_group_checkpoint_links(self, client, _seeded):
        """Group -> checkpoint links must also flow through merge so that
        the arrivals / score builders can find which groups score where."""
        comp, _, group, cp, _, _ = _seeded
        # Seed an existing link in the source export so we know what
        # should round-trip.
        db.session.add(
            CheckpointGroupLink(group_id=group.id, checkpoint_id=cp.id, position=0)
        )
        db.session.commit()

        export_data = client.get(f"/api/competition/{comp.id}/export").get_json()
        # Add a fresh checkpoint + link via the merge payload, simulating
        # an admin who shipped a new checkpoint+group pairing in the
        # source competition.
        export_data["checkpoints"].append({"name": "CP-Merged", "is_virtual": False})
        export_data["group_checkpoint_links"] = list(
            export_data.get("group_checkpoint_links", [])
        ) + [{"group_name": group.name, "checkpoint_name": "CP-Merged", "position": 1}]
        export_data["resolutions"] = {}

        resp = client.post(f"/api/competition/{comp.id}/merge", json=export_data)
        assert resp.status_code == 200
        summary = resp.get_json()["summary"]
        assert summary["added"]["group_checkpoint_links"] >= 1

        new_cp = CheckpointGroup.query.filter_by(competition_id=comp.id, name=group.name).first()
        assert new_cp is not None
        # The link for the brand-new checkpoint must exist.
        from app.models import Checkpoint as _CP

        merged_cp = _CP.query.filter_by(competition_id=comp.id, name="CP-Merged").first()
        assert merged_cp is not None
        link = CheckpointGroupLink.query.filter_by(
            group_id=group.id, checkpoint_id=merged_cp.id
        ).first()
        assert link is not None, "group->checkpoint link did not land via merge"
        assert link.position == 1

    def test_merge_carries_score_rules(self, client, _seeded):
        """ScoreRule + GlobalScoreRule must land via merge. Without them
        the destination has scoring layouts but no logic to compute totals."""
        comp, _, group, cp, _, _ = _seeded
        export_data = client.get(f"/api/competition/{comp.id}/export").get_json()

        # The export now carries score_rules/global_score_rules sections
        # (empty in the fixture). Inject one of each.
        export_data["score_rules"] = [
            {
                "checkpoint_name": cp.name,
                "group_name": group.name,
                "rules": {"field_rules": {"task1": {"type": "multiplier", "factor": 2}}},
            }
        ]
        export_data["global_score_rules"] = [
            {
                "group_name": group.name,
                "rules": {"found": {"points_per": 3}},
            }
        ]
        export_data["resolutions"] = {}

        resp = client.post(f"/api/competition/{comp.id}/merge", json=export_data)
        assert resp.status_code == 200
        summary = resp.get_json()["summary"]
        assert summary["added"]["score_rules"] == 1
        assert summary["added"]["global_score_rules"] == 1

        score_rule = ScoreRule.query.filter_by(
            competition_id=comp.id, checkpoint_id=cp.id, group_id=group.id
        ).first()
        assert score_rule is not None
        assert score_rule.rules["field_rules"]["task1"]["factor"] == 2

        gr = GlobalScoreRule.query.filter_by(
            competition_id=comp.id, group_id=group.id
        ).first()
        assert gr is not None
        assert gr.rules["found"]["points_per"] == 3

    def test_merge_score_rules_do_not_clobber_local(self, client, _seeded):
        """Hand-tuned local ScoreRule rows must survive a re-merge —
        admins often customize these locally and a merge that overwrites
        would silently lose their tuning."""
        comp, _, group, cp, _, _ = _seeded
        # Seed a hand-tuned local rule first.
        db.session.add(
            ScoreRule(
                competition_id=comp.id,
                checkpoint_id=cp.id,
                group_id=group.id,
                rules={"field_rules": {"task1": {"type": "multiplier", "factor": 99}}},
            )
        )
        db.session.commit()

        export_data = client.get(f"/api/competition/{comp.id}/export").get_json()
        # The export carries the local rule; the merge inbound payload
        # claims a different factor. The local row must win.
        export_data["score_rules"] = [
            {
                "checkpoint_name": cp.name,
                "group_name": group.name,
                "rules": {"field_rules": {"task1": {"type": "multiplier", "factor": 1}}},
            }
        ]
        export_data["resolutions"] = {}

        resp = client.post(f"/api/competition/{comp.id}/merge", json=export_data)
        assert resp.status_code == 200
        assert resp.get_json()["summary"]["added"]["score_rules"] == 0

        kept = ScoreRule.query.filter_by(
            competition_id=comp.id, checkpoint_id=cp.id, group_id=group.id
        ).one()
        assert kept.rules["field_rules"]["task1"]["factor"] == 99, (
            "Merge clobbered a hand-tuned local rule"
        )

    def test_merge_carries_devices_and_rfid_cards(self, client, _seeded):
        """LoRaDevice and RFIDCard rows must land via merge (the new-comp
        import already handles them; merge was silently dropping both)."""
        comp, _, _, _, t1, _ = _seeded
        export_data = client.get(f"/api/competition/{comp.id}/export").get_json()

        export_data["devices"] = [
            {"dev_num": 77, "name": "Merge-Dev", "active": True}
        ]
        export_data["rfid_cards"] = [
            {"uid": "AA:BB:CC:DD", "team_name": t1.name, "number": 7}
        ]
        export_data["resolutions"] = {}

        resp = client.post(f"/api/competition/{comp.id}/merge", json=export_data)
        assert resp.status_code == 200
        summary = resp.get_json()["summary"]
        assert summary["added"]["devices"] == 1
        assert summary["added"]["rfid_cards"] == 1

        assert LoRaDevice.query.filter_by(competition_id=comp.id, dev_num=77).count() == 1
        # UID is normalized on the way in.
        assert RFIDCard.query.filter_by(competition_id=comp.id).count() == 1

    def test_merge_admin_only(self, app, client):
        viewer = create_user(username="viewer-merge", role="public")
        comp = create_competition(name="Viewer Merge")
        add_membership(viewer, comp, role="viewer")
        login_as(client, viewer, comp)

        resp = client.post(
            f"/api/competition/{comp.id}/merge",
            json={
                "schema_version": "1.0.0",
                "competition": {"name": "x"},
                "teams": [],
                "groups": [],
                "checkpoints": [],
            },
        )
        assert resp.status_code == 403
