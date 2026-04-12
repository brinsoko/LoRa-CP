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
from app.models import CheckpointGroupLink  # used in _seeded fixture
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

SPREADSHEET_ID = os.environ.get("TEST_SPREADSHEET_ID", "")
SA_FILE = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "")
SA_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "")


def _sheets_available() -> bool:
    if not SPREADSHEET_ID:
        return False
    return bool(SA_FILE or SA_JSON)


skip_no_sheets = pytest.mark.skipif(
    not _sheets_available(),
    reason="Google Sheets API not configured (set TEST_SPREADSHEET_ID and service account credentials)",
)


@pytest.fixture
def sheets_app(app_factory):
    """App with sheets sync enabled and service account configured."""
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
def _seeded(sheets_app, sheets_client):
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

    # Link checkpoints to group (required for wizard tab creation)
    db.session.add(CheckpointGroupLink(group_id=group.id, checkpoint_id=cp1.id, position=0))
    db.session.add(CheckpointGroupLink(group_id=group.id, checkpoint_id=cp2.id, position=1))
    db.session.commit()

    ci1 = create_checkin(comp, t1, cp1)
    ci2 = create_checkin(comp, t2, cp1)
    ci3 = create_checkin(comp, t3, cp2)

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

    login_as(sheets_client, user, comp)
    return comp, user, group, [cp1, cp2], [t1, t2, t3]


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


def _cleanup_tabs(spreadsheet, prefix: str):
    time.sleep(2)
    for ws in spreadsheet.worksheets():
        if ws.title.startswith(prefix):
            try:
                spreadsheet.del_worksheet(ws)
                time.sleep(2)
            except Exception:
                pass


def _run_wizard(sheets_client, checkpoints, prefix="test_"):
    """Run the checkpoint wizard to create checkpoint tab configs + remote tabs."""
    form_data = {
        "spreadsheet_id": SPREADSHEET_ID,
        "create_remote": "1",
    }
    for cp in checkpoints:
        form_data[f"create_cp_{cp.id}"] = "1"
        form_data[f"tab_name_cp_{cp.id}"] = f"{prefix}{cp.name}"
    resp = sheets_client.post("/sheets/wizard/checkpoints", data=form_data)
    assert resp.status_code in (200, 302)
    time.sleep(3)
    return resp


@pytest.mark.sheets
@skip_no_sheets
class TestBuildCheckpointTabs:
    def test_build_checkpoint_tabs(self, sheets_client, _seeded):
        comp, _, group, checkpoints, teams = _seeded
        gc = _get_gc()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(spreadsheet, "test_")

        _run_wizard(sheets_client, checkpoints, "test_")

        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        sheet_titles = [ws.title for ws in spreadsheet.worksheets()]
        created_tabs = [t for t in sheet_titles if t.startswith("test_")]
        try:
            assert len(created_tabs) >= 1, f"Expected tabs starting with 'test_', got: {sheet_titles}"
            # Check headers exist
            ws = spreadsheet.worksheet(created_tabs[0])
            header_row = ws.row_values(1)
            assert len(header_row) >= 1
        finally:
            _cleanup_tabs(spreadsheet, "test_")


@pytest.mark.sheets
@skip_no_sheets
class TestBuildArrivalsMatrix:
    def test_build_arrivals_matrix(self, sheets_client, _seeded):
        comp, _, _, checkpoints, _ = _seeded
        gc = _get_gc()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(spreadsheet, "test_")

        # Must create checkpoint tabs first (arrivals depends on them)
        _run_wizard(sheets_client, checkpoints, "test_")

        tab_name = "test_arrivals"
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
        finally:
            _cleanup_tabs(spreadsheet, "test_")


@pytest.mark.sheets
@skip_no_sheets
class TestBuildTeamsRoster:
    def test_build_teams_roster(self, sheets_client, _seeded):
        comp, _, _, _, teams = _seeded
        gc = _get_gc()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(spreadsheet, "test_")

        tab_name = "test_teams"
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
            assert len(rows) >= 4  # header + 3 teams
        finally:
            _cleanup_tabs(spreadsheet, "test_")


@pytest.mark.sheets
@skip_no_sheets
class TestBuildScoreSheet:
    def test_build_score_sheet(self, sheets_client, _seeded):
        comp, _, group, checkpoints, teams = _seeded
        gc = _get_gc()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(spreadsheet, "test_")

        # Must create checkpoint tabs first
        _run_wizard(sheets_client, checkpoints, "test_")

        tab_name = "test_scores"
        resp = sheets_client.post("/sheets/build-score", data={
            "spreadsheet_id": SPREADSHEET_ID,
            "tab_name": tab_name,
            "group_id": group.id,
        })
        assert resp.status_code in (200, 302)
        time.sleep(3)

        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        try:
            ws = spreadsheet.worksheet(tab_name)
            rows = ws.get_all_values()
            assert len(rows) >= 2
        finally:
            _cleanup_tabs(spreadsheet, "test_")


@pytest.mark.sheets
@skip_no_sheets
class TestSyncTeamNumbers:
    def test_sync_team_numbers(self, sheets_client, _seeded):
        comp, _, _, checkpoints, teams = _seeded
        # Create checkpoint tabs first
        _run_wizard(sheets_client, checkpoints, "test_")
        time.sleep(2)

        config = SheetConfig.query.filter_by(competition_id=comp.id).first()
        if not config:
            pytest.skip("No SheetConfig available to sync")

        resp = sheets_client.post(f"/sheets/sync-team-numbers/{config.id}")
        assert resp.status_code in (200, 302)

        # Cleanup
        gc = _get_gc()
        spreadsheet = gc.open_by_key(SPREADSHEET_ID)
        _cleanup_tabs(spreadsheet, "test_")
