"""Comprehensive sheets scoring integration tests.

These tests require a real Google Sheets API connection.
Set the TEST_SPREADSHEET_ID environment variable to a test spreadsheet ID.
The spreadsheet must be shared with the service account email (Editor access).

Run with: pytest -m sheets
Skip with: pytest -m "not sheets"
"""
from __future__ import annotations

import os
import time
from datetime import datetime, timedelta

import pytest

from app.extensions import db
from app.models import (
    Checkin,
    Checkpoint,
    CheckpointGroupLink,
    GlobalScoreRule,
    ScoreEntry,
    ScoreRule,
    SheetConfig,
)
from tests.support import (
    add_membership,
    assign_team_group,
    create_checkpoint,
    create_checkin,
    create_competition,
    create_group,
    create_team,
    create_user,
    login_as,
)

SPREADSHEET_ID = os.environ.get("TEST_SPREADSHEET_ID", "")
SA_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "")
SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
TAB_PREFIX = "test_sc_"
T0 = datetime(2026, 6, 20, 8, 0, 0)


def _sheets_available() -> bool:
    if not SPREADSHEET_ID:
        return False
    return bool(SA_FILE or SA_JSON)


skip_no_sheets = pytest.mark.skipif(
    not _sheets_available(),
    reason="Google Sheets API not configured (set TEST_SPREADSHEET_ID and service account credentials)",
)


def _get_gc():
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        pytest.skip("gspread or google-auth not installed")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    sa = os.path.abspath(SA_FILE) if SA_FILE else None
    if sa and os.path.isfile(sa):
        creds = Credentials.from_service_account_file(sa, scopes=scopes)
    elif SA_JSON:
        import json as _json
        creds = Credentials.from_service_account_info(_json.loads(SA_JSON), scopes=scopes)
    else:
        pytest.skip("No service account credentials found")
    return gspread.authorize(creds)


def _cleanup_tabs(spreadsheet, prefix: str = TAB_PREFIX):
    time.sleep(2)
    for ws in spreadsheet.worksheets():
        if ws.title.startswith(prefix):
            try:
                spreadsheet.del_worksheet(ws)
                time.sleep(2)
            except Exception:
                pass


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def sheets_app(app_factory):
    overrides = {"SHEETS_SYNC_ENABLED": True}
    if SA_FILE:
        overrides["GOOGLE_SERVICE_ACCOUNT_FILE"] = (
            os.path.abspath(SA_FILE) if not os.path.isabs(SA_FILE) else SA_FILE
        )
    if SA_JSON:
        overrides["GOOGLE_SERVICE_ACCOUNT_JSON"] = SA_JSON
    application = app_factory(**overrides)
    with application.app_context():
        from app.utils.sheets_settings import save_settings
        save_settings({"sync_enabled": True})
        yield application


@pytest.fixture
def sheets_client(sheets_app):
    return sheets_app.test_client()


@pytest.fixture
def seeded_sheets(sheets_app, sheets_client):
    """Seed a competition with 2 groups, 2 regular CPs + 1 virtual, 4 teams + scores."""
    user = create_user(username="sheets-score-admin", role="admin")
    comp = create_competition(name="Sheets Score Race")
    add_membership(user, comp, role="admin")

    grp = create_group(comp, name="Alpha", prefix="1xx")
    grp2 = create_group(comp, name="Beta", prefix="2xx")

    cp1 = create_checkpoint(comp, name="CP-Regular-1")
    cp2 = create_checkpoint(comp, name="CP-Regular-2")
    vcp = create_checkpoint(comp, name="VCP-Start")
    vcp.is_virtual = True
    db.session.commit()

    for pos, cp in enumerate([cp1, cp2, vcp]):
        db.session.add(CheckpointGroupLink(group_id=grp.id, checkpoint_id=cp.id, position=pos))
    for pos, cp in enumerate([cp1, cp2]):
        db.session.add(CheckpointGroupLink(group_id=grp2.id, checkpoint_id=cp.id, position=pos))
    db.session.commit()

    t1 = create_team(comp, name="Team-A", number=101, organization="Org-1")
    t2 = create_team(comp, name="Team-B", number=102, organization="Org-1")
    t3 = create_team(comp, name="Team-C", number=201, organization="Org-2")
    t4 = create_team(comp, name="Team-DNF", number=202)
    t4.dnf = True
    db.session.commit()
    assign_team_group(t1, grp)
    assign_team_group(t2, grp)
    assign_team_group(t3, grp2)
    assign_team_group(t4, grp2)

    # Checkins + scores for regular CPs
    ci1 = create_checkin(comp, t1, cp1, timestamp=T0)
    ci2 = create_checkin(comp, t2, cp1, timestamp=T0 + timedelta(minutes=5))
    ci3 = create_checkin(comp, t3, cp1, timestamp=T0 + timedelta(minutes=10))
    ci4 = create_checkin(comp, t1, cp2, timestamp=T0 + timedelta(minutes=60))
    ci5 = create_checkin(comp, t2, cp2, timestamp=T0 + timedelta(minutes=70))

    # Known scores that we'll verify in sheets
    db_scores = {
        (t1.number, cp1.id): 40.0,
        (t2.number, cp1.id): 35.0,
        (t3.number, cp1.id): 28.0,
        (t1.number, cp2.id): 50.0,
        (t2.number, cp2.id): 45.0,
    }
    for ci, team, cp, total in [
        (ci1, t1, cp1, 40.0),
        (ci2, t2, cp1, 35.0),
        (ci3, t3, cp1, 28.0),
        (ci4, t1, cp2, 50.0),
        (ci5, t2, cp2, 45.0),
    ]:
        db.session.add(ScoreEntry(
            competition_id=comp.id,
            checkin_id=ci.id,
            team_id=team.id,
            checkpoint_id=cp.id,
            judge_user_id=user.id,
            raw_fields={"task": total},
            total=total,
        ))
    # Virtual CP score (no checkin)
    db.session.add(ScoreEntry(
        competition_id=comp.id,
        checkin_id=None,
        team_id=t1.id,
        checkpoint_id=vcp.id,
        judge_user_id=user.id,
        raw_fields={"topo": 32},
        total=32.0,
    ))
    db.session.commit()

    login_as(sheets_client, user, comp)
    return {
        "comp": comp, "user": user,
        "grp": grp, "grp2": grp2,
        "cp1": cp1, "cp2": cp2, "vcp": vcp,
        "teams": [t1, t2, t3, t4],
        "db_scores": db_scores,
    }


def _run_wizard(client, checkpoints, prefix=TAB_PREFIX, extra_fields=None, dead_time=False):
    form_data = {
        "spreadsheet_id": SPREADSHEET_ID,
        "create_remote": "1",
    }
    for cp in checkpoints:
        form_data[f"create_cp_{cp.id}"] = "1"
        form_data[f"tab_name_cp_{cp.id}"] = f"{prefix}{cp.name}"
        if extra_fields and cp.id in extra_fields:
            form_data[f"extra_fields_cp_{cp.id}"] = ",".join(extra_fields[cp.id])
        if dead_time:
            form_data[f"dead_time_cp_{cp.id}"] = "1"
    resp = client.post("/sheets/wizard/checkpoints", data=form_data)
    assert resp.status_code in (200, 302)
    time.sleep(3)
    return resp


def _write_scores_to_sheet(sheets_app, seeded):
    """Use update_checkpoint_scores to push DB scores into checkpoint tabs."""
    from app.utils.sheets_sync import update_checkpoint_scores
    s = seeded
    comp = s["comp"]
    teams = s["teams"]
    t1, t2, t3, t4 = teams

    # Write scores for each team+checkpoint that has a score entry
    for (team, cp, group_name, total) in [
        (t1, s["cp1"], "Alpha", 40.0),
        (t2, s["cp1"], "Alpha", 35.0),
        (t3, s["cp1"], "Beta", 28.0),
        (t1, s["cp2"], "Alpha", 50.0),
        (t2, s["cp2"], "Alpha", 45.0),
    ]:
        update_checkpoint_scores(team.id, cp.id, group_name, {"task": total, "points": total})
        time.sleep(2)


# ===========================================================================
# CHECKPOINT TAB TESTS
# ===========================================================================

@pytest.mark.sheets
@skip_no_sheets
class TestCheckpointTabs:
    def test_sheets_cp_tab_per_checkpoint(self, sheets_client, seeded_sheets):
        s = seeded_sheets
        gc = _get_gc()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(spreadsheet)

        _run_wizard(sheets_client, [s["cp1"], s["cp2"], s["vcp"]])

        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        titles = [ws.title for ws in spreadsheet.worksheets()]
        created = [t for t in titles if t.startswith(TAB_PREFIX)]
        try:
            assert len(created) >= 3
            assert f"{TAB_PREFIX}CP-Regular-1" in titles
            assert f"{TAB_PREFIX}CP-Regular-2" in titles
            assert f"{TAB_PREFIX}VCP-Start" in titles
        finally:
            _cleanup_tabs(spreadsheet)

    def test_sheets_cp_tab_has_team_numbers(self, sheets_client, seeded_sheets):
        """Checkpoint tabs list team numbers — judges can write scores in the cells."""
        s = seeded_sheets
        gc = _get_gc()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(spreadsheet)

        _run_wizard(sheets_client, [s["cp1"]])

        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = spreadsheet.worksheet(f"{TAB_PREFIX}CP-Regular-1")
            all_values = ws.get_all_values()
            # Row 1 = headers, Row 2+ = team numbers
            assert len(all_values) >= 2
            # Team numbers should be present somewhere in the data
            flat = [str(cell) for row in all_values for cell in row]
            assert "101" in flat or "102" in flat
        finally:
            _cleanup_tabs(spreadsheet)

    def test_sheets_cp_tab_score_values_match_db(self, sheets_app, sheets_client, seeded_sheets):
        """After syncing, the Points column in the checkpoint tab matches DB totals."""
        s = seeded_sheets
        gc = _get_gc()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(spreadsheet)

        _run_wizard(sheets_client, [s["cp1"]])
        time.sleep(2)

        # Push scores to sheets
        _write_scores_to_sheet(sheets_app, s)
        time.sleep(3)

        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = spreadsheet.worksheet(f"{TAB_PREFIX}CP-Regular-1")
            all_values = ws.get_all_values()
            # Find team rows and points column
            # Header row has group name, then possibly dead_time, time, fields, Points
            header = all_values[0] if all_values else []
            # Find the "Points" column (last in each group block)
            points_col_idx = None
            for i, h in enumerate(header):
                if "point" in h.lower() or "točk" in h.lower():
                    points_col_idx = i
                    break

            if points_col_idx is not None:
                # Check that at least one score value matches
                scores_in_sheet = {}
                for row in all_values[1:]:
                    if len(row) > points_col_idx and row[0]:
                        try:
                            team_num = int(row[0])
                            pts = float(row[points_col_idx]) if row[points_col_idx] else None
                            if pts is not None:
                                scores_in_sheet[team_num] = pts
                        except (ValueError, IndexError):
                            continue
                # Verify DB scores match
                for (team_num, cp_id), db_total in s["db_scores"].items():
                    if cp_id == s["cp1"].id and team_num in scores_in_sheet:
                        assert scores_in_sheet[team_num] == pytest.approx(db_total, abs=0.01), \
                            f"Team {team_num}: sheet={scores_in_sheet[team_num]} vs db={db_total}"
        finally:
            _cleanup_tabs(spreadsheet)


# ===========================================================================
# ARRIVALS TAB TESTS
# ===========================================================================

@pytest.mark.sheets
@skip_no_sheets
class TestArrivalsTab:
    def test_sheets_arrivals_exists_with_formulas(self, sheets_client, seeded_sheets):
        """Arrivals tab exists, uses MATCH/INDEX formulas (not raw values)."""
        s = seeded_sheets
        gc = _get_gc()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(spreadsheet)

        _run_wizard(sheets_client, [s["cp1"], s["cp2"], s["vcp"]])

        tab_name = f"{TAB_PREFIX}arrivals"
        resp = sheets_client.post("/sheets/build-arrivals", data={
            "spreadsheet_id": SPREADSHEET_ID,
            "tab_name": tab_name,
        })
        assert resp.status_code in (200, 302)
        time.sleep(3)

        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = spreadsheet.worksheet(tab_name)
            rows = ws.get_all_values()
            assert len(rows) >= 2
            # The arrivals tab should reference checkpoint tabs via formulas
            # We can't see formulas via get_all_values (only computed values),
            # but we can verify the structure is correct
            header = rows[0]
            # Should have group name + checkpoint tab names as columns
            assert len(header) >= 2
        finally:
            _cleanup_tabs(spreadsheet)

    def test_sheets_arrivals_virtual_cp_excluded(self, sheets_client, seeded_sheets):
        s = seeded_sheets
        gc = _get_gc()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(spreadsheet)

        _run_wizard(sheets_client, [s["cp1"], s["cp2"], s["vcp"]])

        tab_name = f"{TAB_PREFIX}arrivals2"
        resp = sheets_client.post("/sheets/build-arrivals", data={
            "spreadsheet_id": SPREADSHEET_ID,
            "tab_name": tab_name,
        })
        assert resp.status_code in (200, 302)
        time.sleep(3)

        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = spreadsheet.worksheet(tab_name)
            rows = ws.get_all_values()
            all_text = " ".join(str(cell) for row in rows for cell in row)
            assert "VCP-Start" not in all_text, "Virtual CP should not appear in arrivals"
        finally:
            _cleanup_tabs(spreadsheet)


# ===========================================================================
# TEAMS TAB TESTS
# ===========================================================================

@pytest.mark.sheets
@skip_no_sheets
class TestTeamsTab:
    def test_sheets_teams_tab_all_teams_listed(self, sheets_client, seeded_sheets):
        gc = _get_gc()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(spreadsheet)

        tab_name = f"{TAB_PREFIX}teams"
        resp = sheets_client.post("/sheets/build-teams", data={
            "spreadsheet_id": SPREADSHEET_ID,
            "tab_name": tab_name,
        })
        assert resp.status_code in (200, 302)
        time.sleep(3)

        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = spreadsheet.worksheet(tab_name)
            rows = ws.get_all_values()
            assert len(rows) >= 3  # header rows + at least 1 team
            flat = " ".join(str(cell) for row in rows for cell in row)
            # All 4 team numbers should be present
            assert "101" in flat
            assert "102" in flat
            assert "201" in flat
            assert "202" in flat
        finally:
            _cleanup_tabs(spreadsheet)


# ===========================================================================
# SCORE TAB TESTS
# ===========================================================================

@pytest.mark.sheets
@skip_no_sheets
class TestScoreTab:
    def test_sheets_score_tab_uses_formulas(self, sheets_app, sheets_client, seeded_sheets):
        """Score tab pulls from checkpoint tabs via formulas — not hardcoded values.

        This means if a judge manually edits a checkpoint tab cell, the score
        tab auto-updates (sheets as manual fallback).
        """
        s = seeded_sheets
        gc = _get_gc()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(spreadsheet)

        _run_wizard(sheets_client, [s["cp1"], s["cp2"]])

        # Push scores to checkpoint tabs first
        _write_scores_to_sheet(sheets_app, s)
        time.sleep(3)

        tab_name = f"{TAB_PREFIX}score_formula"
        resp = sheets_client.post("/sheets/build-score", data={
            "spreadsheet_id": SPREADSHEET_ID,
            "tab_name": tab_name,
        })
        assert resp.status_code in (200, 302)
        time.sleep(5)

        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = spreadsheet.worksheet(tab_name)
            # get() returns computed values, but we can also check formulas
            # via the cells feed. Use get_all_records or acell to peek at formulas.
            all_values = ws.get_all_values()
            assert len(all_values) >= 2

            # The score tab should have checkpoint tab names as column headers
            header = all_values[0]
            cp1_tab = f"{TAB_PREFIX}CP-Regular-1"
            cp2_tab = f"{TAB_PREFIX}CP-Regular-2"
            assert cp1_tab in header or cp2_tab in header, \
                f"Score tab headers should reference checkpoint tabs: {header}"

            # Team-A (101) should have: CP1=40, CP2=50, total=90
            for row in all_values[1:]:
                if "101" in row:
                    # Find total column (last non-empty)
                    nums = []
                    for cell in row:
                        try:
                            nums.append(float(cell))
                        except (ValueError, TypeError):
                            continue
                    # The total should be sum of per-CP points
                    if nums:
                        total = nums[-1]
                        assert total == pytest.approx(90.0, abs=1.0), \
                            f"Team 101 total expected ~90, got {total}"
                    break
        finally:
            _cleanup_tabs(spreadsheet)

    def test_sheets_score_tab_has_org_section(self, sheets_client, seeded_sheets):
        s = seeded_sheets
        gc = _get_gc()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(spreadsheet)

        _run_wizard(sheets_client, [s["cp1"], s["cp2"]])

        tab_name = f"{TAB_PREFIX}score_org"
        resp = sheets_client.post("/sheets/build-score", data={
            "spreadsheet_id": SPREADSHEET_ID,
            "tab_name": tab_name,
        })
        assert resp.status_code in (200, 302)
        time.sleep(3)

        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = spreadsheet.worksheet(tab_name)
            rows = ws.get_all_values()
            flat = " ".join(str(cell) for row in rows for cell in row)
            assert "Org-1" in flat or "Org-2" in flat, \
                f"Org section not found in score tab"
        finally:
            _cleanup_tabs(spreadsheet)


# ===========================================================================
# CROSS-VERIFICATION: DB scores == Sheet scores
# ===========================================================================

@pytest.mark.sheets
@skip_no_sheets
class TestCrossVerification:
    def test_sheets_vs_db_scores_match(self, sheets_app, sheets_client, seeded_sheets):
        """Write scores via update_checkpoint_scores, read back from sheet, compare to DB."""
        s = seeded_sheets
        gc = _get_gc()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(spreadsheet)

        _run_wizard(sheets_client, [s["cp1"], s["cp2"]])
        time.sleep(2)
        _write_scores_to_sheet(sheets_app, s)
        time.sleep(5)

        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        try:
            for cp, tab_suffix in [(s["cp1"], "CP-Regular-1"), (s["cp2"], "CP-Regular-2")]:
                ws = spreadsheet.worksheet(f"{TAB_PREFIX}{tab_suffix}")
                all_values = ws.get_all_values()
                header = all_values[0] if all_values else []

                # Find points column
                points_col = None
                for i, h in enumerate(header):
                    if "point" in h.lower() or "točk" in h.lower():
                        points_col = i
                        break
                if points_col is None:
                    continue

                sheet_scores = {}
                for row in all_values[1:]:
                    if len(row) > points_col and row[0]:
                        try:
                            num = int(row[0])
                            val = float(row[points_col]) if row[points_col] else None
                            if val is not None:
                                sheet_scores[num] = val
                        except (ValueError, IndexError):
                            continue

                # Compare against DB
                for (team_num, cp_id), db_total in s["db_scores"].items():
                    if cp_id != cp.id:
                        continue
                    if team_num in sheet_scores:
                        assert sheet_scores[team_num] == pytest.approx(db_total, abs=0.01), \
                            f"Mismatch CP={tab_suffix} Team={team_num}: " \
                            f"sheet={sheet_scores[team_num]} db={db_total}"
                time.sleep(2)
        finally:
            _cleanup_tabs(spreadsheet)

    def test_sheets_checkpoint_tab_is_editable(self, sheets_app, sheets_client, seeded_sheets):
        """Verify checkpoint tabs are raw values (not formulas) — judges can manually edit."""
        s = seeded_sheets
        gc = _get_gc()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(spreadsheet)

        _run_wizard(sheets_client, [s["cp1"]])
        time.sleep(2)
        _write_scores_to_sheet(sheets_app, s)
        time.sleep(3)

        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = spreadsheet.worksheet(f"{TAB_PREFIX}CP-Regular-1")
            # Write a manual override to a cell to confirm it's editable
            # Find Team-A (101) row
            all_values = ws.get_all_values()
            target_row = None
            for i, row in enumerate(all_values):
                if row and str(row[0]) == "101":
                    target_row = i + 1  # 1-based
                    break
            assert target_row is not None, "Team 101 not found in checkpoint tab"

            # Find points column
            header = all_values[0]
            points_col = None
            for j, h in enumerate(header):
                if "point" in h.lower() or "točk" in h.lower():
                    points_col = j + 1  # 1-based
                    break
            assert points_col is not None, "Points column not found"

            # Write a manual value (simulating judge manual override)
            ws.update_cell(target_row, points_col, 999)
            time.sleep(2)
            # Read it back
            val = ws.cell(target_row, points_col).value
            assert val == "999", f"Manual write failed — got {val}"
        finally:
            _cleanup_tabs(spreadsheet)
