"""Regression for: build_arrivals_tab / build_score_tab reported success
even when nothing landed on the spreadsheet.

The arrivals/score/teams builders walk the competition's groups and
accumulate a `values` list, only appending rows when a group has teams
with numbers AND at least one SheetConfig.config "groups" entry whose
name matches that group. If neither condition holds for any group the
list ends up empty, but the old code still wrote the empty list to the
sheet (a no-op) and returned None, so the calling route flashed
"Arrivals tab updated." with nothing actually in the spreadsheet.

These tests reproduce the silent-failure shape with a monkey-patched
SheetsClient (so they never touch the real Google API): they assert the
builder now returns a warning string AND that no Sheets API call was
made on the empty-data path."""

from __future__ import annotations

import pytest

from app.extensions import db
from app.models import SheetConfig
from app.utils import sheets_client as sheets_client_module
from app.utils import sheets_sync
from tests.support import (
    add_membership,
    assign_team_group,
    create_checkpoint,
    create_competition,
    create_group,
    create_team,
    create_user,
    set_group_route,
)


class _RecordingClient:
    """Stand-in for SheetsClient that records every gspread call attempt."""

    def __init__(self):
        self.calls: list[str] = []
        self.gc = self  # so client.gc.open_by_key still works

    def open_by_key(self, *args, **kwargs):
        self.calls.append("open_by_key")
        raise AssertionError(
            "Empty-data path must not contact the Sheets API, but open_by_key was called"
        )

    def _call(self, fn, *args, **kwargs):
        self.calls.append(getattr(fn, "__name__", repr(fn)))
        return fn(*args, **kwargs)


@pytest.fixture
def sheets_app(app_factory):
    """App with sheets sync enabled — required because the builders early
    out with the disabled-sync warning before reaching the empty-values
    guard we want to exercise."""
    application = app_factory(SHEETS_SYNC_ENABLED=True)
    with application.app_context():
        from app.utils.sheets_settings import save_settings

        save_settings({"sync_enabled": True})
        yield application


def _seed_with_unmatched_group_name(app):
    """Set up a competition where a SheetConfig exists but its baked-in
    group name no longer matches any current CheckpointGroup. This is the
    real-world trigger (admin renamed a group after running the wizard).
    """
    user = create_user(username="emptiness-admin", role="admin")
    comp = create_competition(name="Empty Race")
    add_membership(user, comp, role="admin")
    group = create_group(comp, name="Alpha", prefix="1xx")
    cp = create_checkpoint(comp, name="CP-One")
    team = create_team(comp, name="T1", number=11)
    assign_team_group(team, group)
    set_group_route(group, [cp])
    # The SheetConfig references a group name that doesn't exist in the
    # current competition — every iteration of the values-building loop
    # finds no matching cp_configs and skips the group.
    db.session.add(
        SheetConfig(
            competition_id=comp.id,
            spreadsheet_id="sheet-xyz",
            spreadsheet_name="Test Sheet",
            tab_name=cp.name,
            tab_type="checkpoint",
            checkpoint_id=cp.id,
            config={
                "groups": [
                    {"group_id": 999, "name": "RenamedAway", "fields": ["task1"]}
                ],
                "points_header": "Points",
                "time_enabled": True,
                "dead_time_enabled": True,
                "time_header": "Time",
                "dead_time_header": "Dead",
            },
        )
    )
    db.session.commit()
    return comp


def _seed_with_teams_missing_numbers(app):
    """SheetConfig matches the group name but the team has no number, so
    the per-group teams list is empty and the group is skipped."""
    user = create_user(username="numberless-admin", role="admin")
    comp = create_competition(name="Numberless Race")
    add_membership(user, comp, role="admin")
    group = create_group(comp, name="Alpha", prefix="1xx")
    cp = create_checkpoint(comp, name="CP-One")
    # Team exists but has no number — the arrivals build filters
    # Team.number.isnot(None), so this group ends up with no teams.
    team = create_team(comp, name="NumberlessTeam", number=None)
    assign_team_group(team, group)
    set_group_route(group, [cp])
    db.session.add(
        SheetConfig(
            competition_id=comp.id,
            spreadsheet_id="sheet-xyz",
            spreadsheet_name="Test Sheet",
            tab_name=cp.name,
            tab_type="checkpoint",
            checkpoint_id=cp.id,
            config={
                "groups": [
                    {"group_id": group.id, "name": "Alpha", "fields": ["task1"]}
                ],
                "points_header": "Points",
                "time_enabled": True,
                "dead_time_enabled": True,
                "time_header": "Time",
                "dead_time_header": "Dead",
            },
        )
    )
    db.session.commit()
    return comp


def _install_recording_client(monkeypatch):
    recorder = _RecordingClient()

    def fake_get_sheets_client(app):
        return recorder

    monkeypatch.setattr(sheets_sync, "get_sheets_client", fake_get_sheets_client)
    # The module also keeps a direct reference in sheets_client; patch both
    # so any newly added code path can't slip past.
    monkeypatch.setattr(sheets_client_module, "get_sheets_client", fake_get_sheets_client)
    return recorder


def test_arrivals_returns_warning_when_no_data(sheets_app, monkeypatch):
    with sheets_app.app_context():
        comp = _seed_with_unmatched_group_name(sheets_app)
        recorder = _install_recording_client(monkeypatch)
        err = sheets_sync.build_arrivals_tab(
            spreadsheet_id="sheet-xyz",
            tab_name="Arrivals",
            competition_id=comp.id,
        )
        assert err is not None and "No arrivals data" in err, err
        assert recorder.calls == [], (
            f"No Sheets API call should fire on the empty-data path, got {recorder.calls}"
        )


def test_arrivals_returns_warning_when_teams_have_no_numbers(sheets_app, monkeypatch):
    with sheets_app.app_context():
        comp = _seed_with_teams_missing_numbers(sheets_app)
        recorder = _install_recording_client(monkeypatch)
        err = sheets_sync.build_arrivals_tab(
            spreadsheet_id="sheet-xyz",
            tab_name="Arrivals",
            competition_id=comp.id,
        )
        assert err is not None and "No arrivals data" in err, err
        assert recorder.calls == []


def test_arrivals_route_flashes_warning_instead_of_success(sheets_app, monkeypatch):
    """End-to-end shape: the /sheets/build-arrivals route now surfaces
    the warning as a flash, instead of "Arrivals tab updated." over an
    empty sheet."""
    from tests.support import login_as

    with sheets_app.app_context():
        comp = _seed_with_unmatched_group_name(sheets_app)
        admin = create_user(username="route-admin", role="admin")
        add_membership(admin, comp, role="admin")
        _install_recording_client(monkeypatch)
        client = sheets_app.test_client()
        login_as(client, admin, comp)

        resp = client.post(
            "/sheets/build-arrivals",
            data={"spreadsheet_id": "sheet-xyz", "tab_name": "Arrivals"},
            follow_redirects=True,
        )
        body = resp.data.decode("utf-8", errors="replace")
        assert "No arrivals data" in body, body[:500]
        # The misleading success line must not appear.
        assert "Arrivals tab &#39;Arrivals&#39; updated." not in body
        assert "Arrivals tab 'Arrivals' updated." not in body


def test_score_returns_warning_when_no_data(sheets_app, monkeypatch):
    with sheets_app.app_context():
        comp = _seed_with_unmatched_group_name(sheets_app)
        recorder = _install_recording_client(monkeypatch)
        err = sheets_sync.build_score_tab(
            spreadsheet_id="sheet-xyz",
            tab_name="Score",
            competition_id=comp.id,
        )
        assert err is not None and "No score data" in err, err
        assert recorder.calls == []


def test_teams_returns_warning_when_no_groups(sheets_app, monkeypatch):
    """build_teams_tab does not depend on SheetConfig, but it should
    still warn when the competition has no groups at all."""
    with sheets_app.app_context():
        user = create_user(username="no-groups-admin", role="admin")
        comp = create_competition(name="No Groups Race")
        add_membership(user, comp, role="admin")
        recorder = _install_recording_client(monkeypatch)
        err = sheets_sync.build_teams_tab(
            spreadsheet_id="sheet-xyz",
            tab_name="Teams",
            competition_id=comp.id,
        )
        assert err is not None and "No team data" in err, err
        assert recorder.calls == []
