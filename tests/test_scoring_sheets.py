"""Test suite 3: Scoring and sheets integration.

These tests require a real Google Sheets API connection.
Set the TEST_SPREADSHEET_ID environment variable to a test spreadsheet ID.
The spreadsheet must be shared with the service account email (Editor access).
See docs/test-spreadsheet-setup.md for full setup instructions.

Run with: pytest -m sheets
Skip with: pytest -m "not sheets"
"""
from __future__ import annotations

import os
import time

import pytest

from app.extensions import db
from app.models import ScoreEntry, SheetConfig
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


# Register the marker
def pytest_configure(config):
    config.addinivalue_line("markers", "sheets: requires Google Sheets API access (set TEST_SPREADSHEET_ID)")


SPREADSHEET_ID = os.environ.get("TEST_SPREADSHEET_ID", "")


def _sheets_available() -> bool:
    """Check if sheets credentials and spreadsheet ID are configured."""
    if not SPREADSHEET_ID:
        return False
    # Check if service account credentials exist
    sa_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "")
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    return bool(sa_file or sa_json)


skip_no_sheets = pytest.mark.skipif(
    not _sheets_available(),
    reason="Google Sheets API not configured (set TEST_SPREADSHEET_ID and service account credentials)",
)


@pytest.fixture
def _seeded(app, client):
    """Seed a competition with teams, groups, checkpoints, and scores."""
    user = create_user(username="sheets-admin", role="admin")
    comp = create_competition(name="Sheets Race")
    add_membership(user, comp, role="admin")

    group = create_group(comp, name="SheetGrp", prefix="5xx")
    cp1 = create_checkpoint(comp, name="Sheet-CP-1")
    cp2 = create_checkpoint(comp, name="Sheet-CP-2")

    t1 = create_team(comp, name="Sheet-Team-A", number=501)
    t2 = create_team(comp, name="Sheet-Team-B", number=502)
    t3 = create_team(comp, name="Sheet-Team-C", number=503)
    assign_team_group(t1, group)
    assign_team_group(t2, group)
    assign_team_group(t3, group)

    ci1 = create_checkin(comp, t1, cp1)
    ci2 = create_checkin(comp, t2, cp1)
    ci3 = create_checkin(comp, t3, cp2)

    # Add scores
    for ci, team, cp, total in [
        (ci1, t1, cp1, 25),
        (ci2, t2, cp1, 30),
        (ci3, t3, cp2, 18),
    ]:
        db.session.add(ScoreEntry(
            competition_id=comp.id,
            checkin_id=ci.id,
            team_id=team.id,
            checkpoint_id=cp.id,
            judge_user_id=user.id,
            raw_fields={"task1": total // 2, "task2": total - total // 2},
            total=total,
        ))
    db.session.commit()

    login_as(client, user, comp)
    return comp, user, group, [cp1, cp2], [t1, t2, t3]


def _get_sheets_client():
    """Create a gspread client for test assertions."""
    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        pytest.skip("gspread or google-auth not installed")

    sa_file = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "")
    sa_json = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")

    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]

    if sa_file and os.path.isfile(sa_file):
        creds = Credentials.from_service_account_file(sa_file, scopes=scopes)
    elif sa_json:
        import json
        info = json.loads(sa_json)
        creds = Credentials.from_service_account_info(info, scopes=scopes)
    else:
        pytest.skip("No service account credentials found")

    return gspread.authorize(creds)


@pytest.mark.sheets
@skip_no_sheets
class TestBuildCheckpointTabs:
    def test_build_checkpoint_tabs(self, client, _seeded):
        comp, _, group, checkpoints, teams = _seeded
        gc = _get_sheets_client()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)

        # Build checkpoint tabs via the sheets wizard
        cp_ids = [cp.id for cp in checkpoints]
        resp = client.post("/sheets/wizard/checkpoints", data={
            "spreadsheet_id": SPREADSHEET_ID,
            "checkpoint_ids": cp_ids,
            "tab_prefix": "test_",
            "create_remote": "1",
        })
        assert resp.status_code in (200, 302)
        time.sleep(2)

        # Verify tabs were created
        sheet_titles = [ws.title for ws in spreadsheet.worksheets()]
        created_tabs = [t for t in sheet_titles if t.startswith("test_")]
        assert len(created_tabs) >= 1

        # Cleanup
        for tab_title in created_tabs:
            try:
                ws = spreadsheet.worksheet(tab_title)
                spreadsheet.del_worksheet(ws)
                time.sleep(2)
            except Exception:
                pass


@pytest.mark.sheets
@skip_no_sheets
class TestBuildArrivalsMatrix:
    def test_build_arrivals_matrix(self, client, _seeded):
        comp, _, _, _, _ = _seeded
        gc = _get_sheets_client()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)

        tab_name = "test_arrivals"
        resp = client.post("/sheets/build-arrivals", data={
            "spreadsheet_id": SPREADSHEET_ID,
            "tab_name": tab_name,
        })
        assert resp.status_code in (200, 302)
        time.sleep(2)

        try:
            ws = spreadsheet.worksheet(tab_name)
            rows = ws.get_all_values()
            assert len(rows) >= 2  # header + at least 1 data row
        finally:
            try:
                spreadsheet.del_worksheet(ws)
                time.sleep(2)
            except Exception:
                pass


@pytest.mark.sheets
@skip_no_sheets
class TestBuildTeamsRoster:
    def test_build_teams_roster(self, client, _seeded):
        comp, _, _, _, teams = _seeded
        gc = _get_sheets_client()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)

        tab_name = "test_teams"
        resp = client.post("/sheets/build-teams", data={
            "spreadsheet_id": SPREADSHEET_ID,
            "tab_name": tab_name,
        })
        assert resp.status_code in (200, 302)
        time.sleep(2)

        try:
            ws = spreadsheet.worksheet(tab_name)
            rows = ws.get_all_values()
            # Header + 3 teams
            assert len(rows) >= 4
        finally:
            try:
                spreadsheet.del_worksheet(ws)
                time.sleep(2)
            except Exception:
                pass


@pytest.mark.sheets
@skip_no_sheets
class TestBuildScoreSheet:
    def test_build_score_sheet(self, client, _seeded):
        comp, _, group, checkpoints, teams = _seeded
        gc = _get_sheets_client()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)

        tab_name = "test_scores"
        resp = client.post("/sheets/build-score", data={
            "spreadsheet_id": SPREADSHEET_ID,
            "tab_name": tab_name,
            "group_id": group.id,
        })
        assert resp.status_code in (200, 302)
        time.sleep(2)

        try:
            ws = spreadsheet.worksheet(tab_name)
            rows = ws.get_all_values()
            assert len(rows) >= 2
        finally:
            try:
                spreadsheet.del_worksheet(ws)
                time.sleep(2)
            except Exception:
                pass


@pytest.mark.sheets
@skip_no_sheets
class TestSyncTeamNumbers:
    def test_sync_team_numbers(self, client, _seeded):
        comp, _, _, _, teams = _seeded
        # Find a sheet config to sync
        config = SheetConfig.query.filter_by(competition_id=comp.id).first()
        if not config:
            pytest.skip("No SheetConfig available to sync")

        resp = client.post(f"/sheets/sync-team-numbers/{config.id}")
        assert resp.status_code in (200, 302)
