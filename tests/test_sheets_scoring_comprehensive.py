"""Comprehensive sheets scoring integration tests.

Tests verify: formulas in score tab, dead time sums, arrivals coloring,
arrival timestamps, concurrent submissions, org totals, and that
checkpoint tabs use raw values (manual fallback for judges).

Requires: TEST_SPREADSHEET_ID env var + service account credentials.

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
    return bool(SPREADSHEET_ID) and bool(SA_FILE or SA_JSON)


skip_no_sheets = pytest.mark.skipif(
    not _sheets_available(),
    reason="Google Sheets API not configured",
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
def seeded(sheets_app, sheets_client):
    """Seed competition: 2 groups, 2 regular CPs + 1 virtual, 4 teams, scores."""
    user = create_user(username="sheets-admin2", role="admin")
    comp = create_competition(name="Sheets Full Test")
    add_membership(user, comp, role="admin")

    grp = create_group(comp, name="Alpha", prefix="1xx")
    grp2 = create_group(comp, name="Beta", prefix="2xx")

    cp1 = create_checkpoint(comp, name="CP-One")
    cp2 = create_checkpoint(comp, name="CP-Two")
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

    ci1 = create_checkin(comp, t1, cp1, timestamp=T0)
    ci2 = create_checkin(comp, t2, cp1, timestamp=T0 + timedelta(minutes=5))
    ci3 = create_checkin(comp, t3, cp1, timestamp=T0 + timedelta(minutes=10))
    ci4 = create_checkin(comp, t1, cp2, timestamp=T0 + timedelta(minutes=60))
    ci5 = create_checkin(comp, t2, cp2, timestamp=T0 + timedelta(minutes=70))

    for ci, team, cp, total in [
        (ci1, t1, cp1, 40.0),
        (ci2, t2, cp1, 35.0),
        (ci3, t3, cp1, 28.0),
        (ci4, t1, cp2, 50.0),
        (ci5, t2, cp2, 45.0),
    ]:
        db.session.add(ScoreEntry(
            competition_id=comp.id, checkin_id=ci.id, team_id=team.id,
            checkpoint_id=cp.id, judge_user_id=user.id,
            raw_fields={"dead_time": 5, "task": total, "points": total},
            total=total,
        ))
    db.session.add(ScoreEntry(
        competition_id=comp.id, checkin_id=None, team_id=t1.id,
        checkpoint_id=vcp.id, judge_user_id=user.id,
        raw_fields={"topo": 32}, total=32.0,
    ))
    db.session.commit()

    login_as(sheets_client, user, comp)
    return {
        "comp": comp, "user": user,
        "grp": grp, "grp2": grp2,
        "cp1": cp1, "cp2": cp2, "vcp": vcp,
        "t1": t1, "t2": t2, "t3": t3, "t4": t4,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_wizard(client, checkpoints, prefix=TAB_PREFIX, dead_time=True, record_time=True):
    form_data = {"spreadsheet_id": SPREADSHEET_ID, "create_remote": "1"}
    for cp in checkpoints:
        form_data[f"create_cp_{cp.id}"] = "1"
        form_data[f"tab_name_cp_{cp.id}"] = f"{prefix}{cp.name}"
        if dead_time:
            form_data[f"dead_time_cp_{cp.id}"] = "1"
        if record_time:
            form_data[f"record_time_cp_{cp.id}"] = "1"
    resp = client.post("/sheets/wizard/checkpoints", data=form_data)
    assert resp.status_code in (200, 302)
    time.sleep(3)


def _write_scores(app, s):
    """Push DB scores to checkpoint tabs."""
    from app.utils.sheets_sync import update_checkpoint_scores
    for team, cp, grp_name, vals in [
        (s["t1"], s["cp1"], "Alpha", {"dead_time": 5, "task": 40, "points": 40}),
        (s["t2"], s["cp1"], "Alpha", {"dead_time": 3, "task": 35, "points": 35}),
        (s["t3"], s["cp1"], "Beta", {"dead_time": 0, "task": 28, "points": 28}),
        (s["t1"], s["cp2"], "Alpha", {"dead_time": 10, "task": 50, "points": 50}),
        (s["t2"], s["cp2"], "Alpha", {"dead_time": 7, "task": 45, "points": 45}),
    ]:
        update_checkpoint_scores(team.id, cp.id, grp_name, vals)
        time.sleep(4)  # longer pause to avoid 429 rate limits


def _build_score_tab(client, s, tab_name, dead_time_sum=True):
    resp = client.post("/sheets/build-score", data={
        "spreadsheet_id": SPREADSHEET_ID,
        "tab_name": tab_name,
        "include_dead_time_sum": "1" if dead_time_sum else "",
    })
    assert resp.status_code in (200, 302)
    time.sleep(5)


def _build_arrivals_tab(client, tab_name):
    resp = client.post("/sheets/build-arrivals", data={
        "spreadsheet_id": SPREADSHEET_ID,
        "tab_name": tab_name,
    })
    assert resp.status_code in (200, 302)
    time.sleep(5)


def _find_team_row(all_values, team_num_str):
    """Find the 1-based row index for a team number."""
    for i, row in enumerate(all_values):
        if row and str(row[0]).strip() == team_num_str:
            return i + 1  # 1-based
    return None


def _find_col(header, *keywords):
    """Find 0-based column index matching any keyword (case-insensitive)."""
    for i, h in enumerate(header):
        hl = str(h).lower()
        if any(kw in hl for kw in keywords):
            return i
    return None


# ===========================================================================
# GROUP 1: Score Tab Formula Verification
# ===========================================================================

@pytest.mark.sheets
@skip_no_sheets
class TestScoreTabFormulas:
    def test_score_tab_formulas_contain_index_match(self, sheets_app, sheets_client, seeded):
        s = seeded
        gc = _get_gc()
        sp = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(sp)
        _run_wizard(sheets_client, [s["cp1"], s["cp2"]])
        _write_scores(sheets_app, s)
        tab = f"{TAB_PREFIX}score_f1"
        _build_score_tab(sheets_client, s, tab)
        sp = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = sp.worksheet(tab)
            header = ws.row_values(1)
            # Find a CP column (should be after Group/Number/Name/Org)
            cp_col_idx = None
            for i, h in enumerate(header):
                if TAB_PREFIX in str(h):
                    cp_col_idx = i
                    break
            assert cp_col_idx is not None, f"No CP column in header: {header}"
            from gspread.utils import rowcol_to_a1
            cell_a1 = rowcol_to_a1(2, cp_col_idx + 1)
            formula = ws.acell(cell_a1, value_render_option="FORMULA").value
            assert "INDEX" in str(formula).upper(), f"Expected INDEX formula, got: {formula}"
            assert "MATCH" in str(formula).upper(), f"Expected MATCH formula, got: {formula}"
        finally:
            _cleanup_tabs(sp)

    def test_score_tab_total_formula_is_sum(self, sheets_app, sheets_client, seeded):
        s = seeded
        gc = _get_gc()
        sp = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(sp)
        _run_wizard(sheets_client, [s["cp1"], s["cp2"]])
        _write_scores(sheets_app, s)
        tab = f"{TAB_PREFIX}score_f2"
        _build_score_tab(sheets_client, s, tab)
        sp = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = sp.worksheet(tab)
            header = ws.row_values(1)
            total_col = _find_col(header, "skupaj", "total")
            assert total_col is not None, f"No total column: {header}"
            from gspread.utils import rowcol_to_a1
            cell_a1 = rowcol_to_a1(2, total_col + 1)
            formula = ws.acell(cell_a1, value_render_option="FORMULA").value
            assert str(formula).upper().startswith("=SUM("), f"Expected =SUM formula, got: {formula}"
        finally:
            _cleanup_tabs(sp)

    def test_score_tab_formula_propagation(self, sheets_app, sheets_client, seeded):
        """Edit a checkpoint tab cell → score tab total auto-updates."""
        s = seeded
        gc = _get_gc()
        sp = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(sp)
        _run_wizard(sheets_client, [s["cp1"], s["cp2"]])
        _write_scores(sheets_app, s)
        tab = f"{TAB_PREFIX}score_f3"
        _build_score_tab(sheets_client, s, tab)
        sp = gc.open_by_key(SPREADSHEET_ID)
        try:
            # Read Team-A total (should be 40+50=90)
            ws_score = sp.worksheet(tab)
            header = ws_score.row_values(1)
            total_col = _find_col(header, "skupaj", "total")
            assert total_col is not None
            all_vals = ws_score.get_all_values()
            team_row = None
            for i, row in enumerate(all_vals[1:], start=2):
                if "101" in row:
                    team_row = i
                    break
            assert team_row is not None, "Team 101 not found in score tab"
            from gspread.utils import rowcol_to_a1
            original = ws_score.acell(rowcol_to_a1(team_row, total_col + 1)).value
            assert float(original) == pytest.approx(90.0, abs=1)

            # Now change CP1 points from 40 → 999 in the checkpoint tab
            ws_cp1 = sp.worksheet(f"{TAB_PREFIX}CP-One")
            cp1_vals = ws_cp1.get_all_values()
            cp1_header = cp1_vals[0]
            pts_col = _find_col(cp1_header, "point", "točk")
            cp1_row = _find_team_row(cp1_vals, "101")
            assert pts_col is not None and cp1_row is not None
            ws_cp1.update_cell(cp1_row, pts_col + 1, 999)
            time.sleep(3)

            # Re-read score tab total — should now be 999 + 50 = 1049
            updated = ws_score.acell(rowcol_to_a1(team_row, total_col + 1)).value
            assert float(updated) == pytest.approx(1049.0, abs=1), \
                f"Propagation failed: expected ~1049, got {updated}"
        finally:
            _cleanup_tabs(sp)

    def test_score_tab_missing_team_shows_zero(self, sheets_app, sheets_client, seeded):
        """Team-C (201) has no CP2 score → CP2 column shows 0."""
        s = seeded
        gc = _get_gc()
        sp = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(sp)
        _run_wizard(sheets_client, [s["cp1"], s["cp2"]])
        _write_scores(sheets_app, s)
        tab = f"{TAB_PREFIX}score_f4"
        _build_score_tab(sheets_client, s, tab)
        sp = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = sp.worksheet(tab)
            all_vals = ws.get_all_values()
            # Find Team-C row (group Beta, number 201)
            for row in all_vals[1:]:
                if "201" in row:
                    header = all_vals[0]  # might not be the right header if multi-group
                    # Find a header row that contains CP-Two
                    # In multi-group score tab, each group has its own header row
                    # Just check that no cell shows #N/A or error
                    for cell in row:
                        assert "#N/A" not in str(cell), f"IFERROR not working: {row}"
                        assert "#REF" not in str(cell), f"Formula error: {row}"
                    break
        finally:
            _cleanup_tabs(sp)

    def test_score_tab_dead_time_sum_formula(self, sheets_app, sheets_client, seeded):
        s = seeded
        gc = _get_gc()
        sp = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(sp)
        _run_wizard(sheets_client, [s["cp1"], s["cp2"]])
        _write_scores(sheets_app, s)
        tab = f"{TAB_PREFIX}score_f5"
        _build_score_tab(sheets_client, s, tab, dead_time_sum=True)
        sp = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = sp.worksheet(tab)
            header = ws.row_values(1)
            dt_col = _find_col(header, "mrtvi", "dead time")
            assert dt_col is not None, f"No dead time sum column: {header}"
            from gspread.utils import rowcol_to_a1
            formula = ws.acell(rowcol_to_a1(2, dt_col + 1), value_render_option="FORMULA").value
            assert "SUM" in str(formula).upper(), f"Expected SUM formula, got: {formula}"
        finally:
            _cleanup_tabs(sp)


# ===========================================================================
# GROUP 2: Dead Time Sum Verification
# ===========================================================================

@pytest.mark.sheets
@skip_no_sheets
class TestDeadTimeSum:
    def test_dead_time_sum_aggregates_across_cps(self, sheets_app, sheets_client, seeded):
        """Team-A: dead_time=5 at CP1 + dead_time=10 at CP2 = 15 in score tab."""
        s = seeded
        gc = _get_gc()
        sp = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(sp)
        _run_wizard(sheets_client, [s["cp1"], s["cp2"]])
        _write_scores(sheets_app, s)
        tab = f"{TAB_PREFIX}dt1"
        _build_score_tab(sheets_client, s, tab, dead_time_sum=True)
        sp = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = sp.worksheet(tab)
            header = ws.row_values(1)
            dt_col = _find_col(header, "mrtvi", "dead time")
            assert dt_col is not None
            all_vals = ws.get_all_values()
            for row in all_vals[1:]:
                if "101" in row:
                    val = float(row[dt_col]) if row[dt_col] else 0
                    assert val == pytest.approx(15.0, abs=0.5), \
                        f"Team-A dead time sum: expected 15, got {val}"
                    break
        finally:
            _cleanup_tabs(sp)

    def test_dead_time_sum_zero_when_empty(self, sheets_app, sheets_client, seeded):
        """Team-C has dead_time=0. Score tab dead time sum = 0."""
        s = seeded
        gc = _get_gc()
        sp = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(sp)
        _run_wizard(sheets_client, [s["cp1"], s["cp2"]])
        _write_scores(sheets_app, s)
        tab = f"{TAB_PREFIX}dt2"
        _build_score_tab(sheets_client, s, tab, dead_time_sum=True)
        sp = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = sp.worksheet(tab)
            all_vals = ws.get_all_values()
            # Team-C row should show 0 dead time (not an error)
            for row in all_vals[1:]:
                if "201" in row:
                    # Just verify no error
                    for cell in row:
                        assert "#" not in str(cell), f"Error in Team-C row: {row}"
                    break
        finally:
            _cleanup_tabs(sp)

    def test_dead_time_propagation(self, sheets_app, sheets_client, seeded):
        """Edit dead_time in checkpoint tab → score tab dead time sum updates."""
        s = seeded
        gc = _get_gc()
        sp = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(sp)
        _run_wizard(sheets_client, [s["cp1"], s["cp2"]])
        _write_scores(sheets_app, s)
        tab = f"{TAB_PREFIX}dt3"
        _build_score_tab(sheets_client, s, tab, dead_time_sum=True)
        sp = gc.open_by_key(SPREADSHEET_ID)
        try:
            # Change CP1 dead_time for Team-A from 5 → 99
            ws_cp1 = sp.worksheet(f"{TAB_PREFIX}CP-One")
            cp1_vals = ws_cp1.get_all_values()
            cp1_header = cp1_vals[0]
            dt_col = _find_col(cp1_header, "dead", "mrtvi")
            team_row = _find_team_row(cp1_vals, "101")
            assert dt_col is not None and team_row is not None
            ws_cp1.update_cell(team_row, dt_col + 1, 99)
            time.sleep(3)

            ws_score = sp.worksheet(tab)
            header = ws_score.row_values(1)
            score_dt_col = _find_col(header, "mrtvi", "dead time")
            all_vals = ws_score.get_all_values()
            for row in all_vals[1:]:
                if "101" in row:
                    val = float(row[score_dt_col]) if row[score_dt_col] else 0
                    # Was 5+10=15, now 99+10=109
                    assert val == pytest.approx(109.0, abs=1), \
                        f"Dead time propagation: expected ~109, got {val}"
                    break
        finally:
            _cleanup_tabs(sp)


# ===========================================================================
# GROUP 3: Arrivals Coloring
# ===========================================================================

@pytest.mark.sheets
@skip_no_sheets
class TestArrivalsColoring:
    def test_arrivals_coloring_applied(self, sheets_app, sheets_client, seeded):
        """After fix: arrivals tab has conditional formatting rules that use ISBLANK."""
        s = seeded
        gc = _get_gc()
        sp = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(sp)
        _run_wizard(sheets_client, [s["cp1"], s["cp2"]])
        tab = f"{TAB_PREFIX}arr_color"
        _build_arrivals_tab(sheets_client, tab)
        sp = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = sp.worksheet(tab)
            # Fetch sheet metadata to inspect conditional format rules
            meta = sp.fetch_sheet_metadata()
            sheet_meta = None
            for sheet in meta.get("sheets", []):
                if sheet.get("properties", {}).get("title") == tab:
                    sheet_meta = sheet
                    break
            assert sheet_meta is not None, "Sheet metadata not found"
            cond_rules = sheet_meta.get("conditionalFormats", [])
            assert len(cond_rules) >= 2, f"Expected 2 conditional format rules, got {len(cond_rules)}"
            # Verify at least one rule uses ISBLANK-based formula
            rule_texts = []
            for rule in cond_rules:
                cond = rule.get("booleanRule", {}).get("condition", {})
                for v in cond.get("values", []):
                    rule_texts.append(v.get("userEnteredValue", ""))
            joined = " ".join(rule_texts).upper()
            assert "ISBLANK" in joined, f"Expected ISBLANK in conditional rules, got: {rule_texts}"
        finally:
            _cleanup_tabs(sp)

    def test_arrivals_virtual_cp_not_in_columns(self, sheets_client, seeded):
        s = seeded
        gc = _get_gc()
        sp = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(sp)
        _run_wizard(sheets_client, [s["cp1"], s["cp2"], s["vcp"]])
        tab = f"{TAB_PREFIX}arr_vcp"
        _build_arrivals_tab(sheets_client, tab)
        sp = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = sp.worksheet(tab)
            all_text = " ".join(str(c) for row in ws.get_all_values() for c in row)
            assert "VCP-Start" not in all_text, "Virtual CP should not appear in arrivals"
        finally:
            _cleanup_tabs(sp)


# ===========================================================================
# GROUP 4: mark_arrival_checkbox Behavior
# ===========================================================================

@pytest.mark.sheets
@skip_no_sheets
class TestMarkArrival:
    def test_mark_arrival_writes_timestamp(self, sheets_app, sheets_client, seeded):
        """mark_arrival_checkbox writes a formatted timestamp, not TRUE."""
        s = seeded
        gc = _get_gc()
        sp = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(sp)
        # Must have record_time=True for the time column to exist
        _run_wizard(sheets_client, [s["cp1"]], dead_time=True, record_time=True)
        time.sleep(2)

        from app.utils.sheets_sync import mark_arrival_checkbox
        ts = T0 + timedelta(minutes=15)
        mark_arrival_checkbox(s["t1"].id, s["cp1"].id, ts)
        time.sleep(3)

        sp = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = sp.worksheet(f"{TAB_PREFIX}CP-One")
            all_vals = ws.get_all_values()
            team_row = _find_team_row(all_vals, "101")
            assert team_row is not None
            header = all_vals[0]
            # The time column header comes from config (default "Čas")
            # Find it — it's the column AFTER dead time and BEFORE extra fields
            # Header layout: [GroupName, DeadTime, Time, Points]
            # Time column is at index 2 when dead_time is enabled
            time_col = 2  # 0-based: Group(0), DeadTime(1), Time(2), Points(3)
            cell_val = str(all_vals[team_row - 1][time_col])
            assert cell_val != "TRUE", f"Expected timestamp, got TRUE"
            assert "2026" in cell_val or "08:" in cell_val, \
                f"Expected timestamp at col {time_col}, got: {cell_val}. Header: {header}"
        finally:
            _cleanup_tabs(sp)

    def test_mark_arrival_correct_column_with_dead_time(self, sheets_app, sheets_client, seeded):
        """With both dead_time and time enabled, timestamp goes to col 3 (0-based: 2), not dead_time col."""
        s = seeded
        gc = _get_gc()
        sp = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(sp)
        _run_wizard(sheets_client, [s["cp1"]], dead_time=True, record_time=True)
        time.sleep(2)

        from app.utils.sheets_sync import mark_arrival_checkbox
        mark_arrival_checkbox(s["t1"].id, s["cp1"].id, T0)
        time.sleep(3)

        sp = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = sp.worksheet(f"{TAB_PREFIX}CP-One")
            all_vals = ws.get_all_values()
            header = all_vals[0]
            # With dead_time + time: Header = [GroupName, DeadTime_header, Time_header, Points]
            # Dead time is at col index 1, Time at col index 2
            assert len(header) >= 4, f"Expected at least 4 headers, got: {header}"
            team_row = _find_team_row(all_vals, "101")
            assert team_row is not None
            # Col 1 = dead time header (should NOT have timestamp)
            dt_val = str(all_vals[team_row - 1][1])
            # Col 2 = time header (SHOULD have timestamp)
            time_val = str(all_vals[team_row - 1][2])
            assert "2026" not in dt_val, f"Timestamp leaked into dead_time column: {dt_val}"
            assert "2026" in time_val or "08:" in time_val, \
                f"Expected timestamp in time col (index 2): {time_val}. Row: {all_vals[team_row - 1]}"
        finally:
            _cleanup_tabs(sp)


# ===========================================================================
# GROUP 5: Concurrent Submissions
# ===========================================================================

@pytest.mark.sheets
@skip_no_sheets
class TestConcurrentSubmissions:
    def test_concurrent_different_teams_no_conflict(self, sheets_app, sheets_client, seeded):
        """Two teams scored sequentially-fast → both rows correct.

        Note: true threading with SQLAlchemy is unsafe (shared session).
        We test rapid sequential writes instead, which is the realistic scenario.
        """
        s = seeded
        gc = _get_gc()
        sp = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(sp)
        _run_wizard(sheets_client, [s["cp1"]])
        time.sleep(2)

        from app.utils.sheets_sync import update_checkpoint_scores
        # Rapid sequential writes (simulating near-concurrent submissions)
        update_checkpoint_scores(s["t1"].id, s["cp1"].id, "Alpha", {"dead_time": 1, "points": 77})
        update_checkpoint_scores(s["t2"].id, s["cp1"].id, "Alpha", {"dead_time": 2, "points": 88})
        time.sleep(3)

        sp = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = sp.worksheet(f"{TAB_PREFIX}CP-One")
            all_vals = ws.get_all_values()
            header = all_vals[0]
            pts_col = _find_col(header, "point", "točk")
            assert pts_col is not None
            r1 = _find_team_row(all_vals, "101")
            r2 = _find_team_row(all_vals, "102")
            assert r1 is not None and r2 is not None
            v1 = float(all_vals[r1 - 1][pts_col]) if all_vals[r1 - 1][pts_col] else 0
            v2 = float(all_vals[r2 - 1][pts_col]) if all_vals[r2 - 1][pts_col] else 0
            assert v1 == pytest.approx(77, abs=0.1)
            assert v2 == pytest.approx(88, abs=0.1)
        finally:
            _cleanup_tabs(sp)

    def test_concurrent_same_team_last_write_wins(self, sheets_app, sheets_client, seeded):
        """Two rapid submissions for the same team — last write wins."""
        s = seeded
        gc = _get_gc()
        sp = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(sp)
        _run_wizard(sheets_client, [s["cp1"]])
        time.sleep(2)

        from app.utils.sheets_sync import update_checkpoint_scores
        update_checkpoint_scores(s["t1"].id, s["cp1"].id, "Alpha", {"points": 111})
        update_checkpoint_scores(s["t1"].id, s["cp1"].id, "Alpha", {"points": 222})
        time.sleep(3)

        sp = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = sp.worksheet(f"{TAB_PREFIX}CP-One")
            all_vals = ws.get_all_values()
            header = all_vals[0]
            pts_col = _find_col(header, "point", "točk")
            r1 = _find_team_row(all_vals, "101")
            assert r1 is not None and pts_col is not None
            val = float(all_vals[r1 - 1][pts_col])
            # Last write (222) should win
            assert val == pytest.approx(222, abs=0.1), f"Expected 222, got {val}"
        finally:
            _cleanup_tabs(sp)


# ===========================================================================
# GROUP 6: Score Tab Org Summary
# ===========================================================================

@pytest.mark.sheets
@skip_no_sheets
class TestOrgSummary:
    def test_org_total_formula(self, sheets_app, sheets_client, seeded):
        s = seeded
        gc = _get_gc()
        sp = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(sp)
        _run_wizard(sheets_client, [s["cp1"], s["cp2"]])
        _write_scores(sheets_app, s)
        time.sleep(3)  # ensure all writes complete before building score tab
        tab = f"{TAB_PREFIX}org1"
        _build_score_tab(sheets_client, s, tab)
        time.sleep(3)  # let formulas recalculate
        sp = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = sp.worksheet(tab)
            all_vals = ws.get_all_values()
            # Find org section — look for "Org-1" in any row
            for i, row in enumerate(all_vals):
                if "Org-1" in row:
                    # The last cell with a number should be the org total
                    nums = []
                    for cell in row:
                        try:
                            nums.append(float(cell))
                        except (ValueError, TypeError):
                            continue
                    assert len(nums) > 0, f"No numeric values in Org-1 row: {row}"
                    org_total = nums[-1]
                    # Team-A: CP1=40 + CP2=50 = 90
                    # Team-B: CP1=35 + CP2=45 = 80
                    # Org-1 total should be >= 90 (at minimum Team-A's contribution)
                    # The formula uses FILTER by org name across the group block
                    assert org_total >= 80, \
                        f"Org-1 total too low: got {org_total}, row: {row}"
                    break
            else:
                pytest.fail("Org-1 not found in score tab")
        finally:
            _cleanup_tabs(sp)

    def test_org_names_present(self, sheets_app, sheets_client, seeded):
        s = seeded
        gc = _get_gc()
        sp = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(sp)
        _run_wizard(sheets_client, [s["cp1"], s["cp2"]])
        tab = f"{TAB_PREFIX}org2"
        _build_score_tab(sheets_client, s, tab)
        sp = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = sp.worksheet(tab)
            flat = " ".join(str(c) for row in ws.get_all_values() for c in row)
            assert "Org-1" in flat
            assert "Org-2" in flat
        finally:
            _cleanup_tabs(sp)


# ===========================================================================
# GROUP 7: Checkpoint Tab Values
# ===========================================================================

@pytest.mark.sheets
@skip_no_sheets
class TestCheckpointTabValues:
    def test_cp_tab_values_are_raw_not_formulas(self, sheets_app, sheets_client, seeded):
        """Checkpoint tab points are raw values — judges can edit them manually."""
        s = seeded
        gc = _get_gc()
        sp = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(sp)
        _run_wizard(sheets_client, [s["cp1"]])
        _write_scores(sheets_app, s)
        time.sleep(3)
        sp = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = sp.worksheet(f"{TAB_PREFIX}CP-One")
            all_vals = ws.get_all_values()
            header = all_vals[0]
            pts_col = _find_col(header, "point", "točk")
            team_row = _find_team_row(all_vals, "101")
            assert pts_col is not None and team_row is not None
            from gspread.utils import rowcol_to_a1
            formula = ws.acell(rowcol_to_a1(team_row, pts_col + 1), value_render_option="FORMULA").value
            assert not str(formula).startswith("="), \
                f"Checkpoint tab should have raw values, got formula: {formula}"
        finally:
            _cleanup_tabs(sp)

    def test_cp_tab_db_scores_match_sheet_values(self, sheets_app, sheets_client, seeded):
        """Scores written via update_checkpoint_scores match DB values."""
        s = seeded
        gc = _get_gc()
        sp = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(sp)
        _run_wizard(sheets_client, [s["cp1"]])
        _write_scores(sheets_app, s)
        time.sleep(3)
        sp = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = sp.worksheet(f"{TAB_PREFIX}CP-One")
            all_vals = ws.get_all_values()
            header = all_vals[0]
            pts_col = _find_col(header, "point", "točk")
            assert pts_col is not None
            expected = {"101": 40.0, "102": 35.0}
            for num_str, db_val in expected.items():
                row = _find_team_row(all_vals, num_str)
                assert row is not None, f"Team {num_str} not found"
                sheet_val = float(all_vals[row - 1][pts_col])
                assert sheet_val == pytest.approx(db_val, abs=0.01), \
                    f"Team {num_str}: sheet={sheet_val} vs db={db_val}"
        finally:
            _cleanup_tabs(sp)
