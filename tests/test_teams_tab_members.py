"""build_teams_tab must include each team's member list in the new
Clani column. Members are stored as TeamMember rows; the sheet shows them
joined with newlines inside one cell so the row count still matches
teams 1:1.
"""

from __future__ import annotations

import pytest

from app.extensions import db
from app.models import TeamMember
from app.utils import sheets_client as sheets_client_module
from app.utils import sheets_sync
from tests.support import (
    add_membership,
    assign_team_group,
    create_competition,
    create_group,
    create_team,
    create_user,
)


class _Recorder:
    """Minimal SheetsClient fake to capture what ws.update was called with."""

    def __init__(self):
        self.last_values = None
        self.gc = self._GC(self)

    class _GC:
        def __init__(self, parent):
            self._parent = parent

        def open_by_key(self, _key):
            return _Recorder._SS(self._parent)

    class _SS:
        def __init__(self, parent):
            self._parent = parent

        def worksheet(self, _name):
            return _Recorder._WS(self._parent)

        def add_worksheet(self, *, title, rows, cols, **_):
            return _Recorder._WS(self._parent)

    class _WS:
        def __init__(self, parent):
            self._parent = parent

        def clear(self):
            pass

        def update(self, *, range_name, values, value_input_option=None, **_):
            self._parent.last_values = values

    def _call(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)


@pytest.fixture
def sheets_app(app_factory):
    application = app_factory(SHEETS_SYNC_ENABLED=True)
    with application.app_context():
        from app.utils.sheets_settings import save_settings

        save_settings({"sync_enabled": True})
        yield application


def test_build_teams_tab_includes_members_column(sheets_app, monkeypatch):
    with sheets_app.app_context():
        user = create_user(username="members-tab-admin", role="admin")
        comp = create_competition(name="Members Tab Race")
        add_membership(user, comp, role="admin")
        grp = create_group(comp, name="mGG", prefix="1xx")
        team = create_team(comp, name="POŠTARJI", number=101)
        assign_team_group(team, grp)
        db.session.add(TeamMember(team_id=team.id, name="Ana Novak", position=0))
        db.session.add(TeamMember(team_id=team.id, name="Bor Kovač", position=1))
        db.session.commit()

        recorder = _Recorder()

        def _get(_app):
            return recorder

        monkeypatch.setattr(sheets_sync, "get_sheets_client", _get)
        monkeypatch.setattr(sheets_client_module, "get_sheets_client", _get)

        sheets_sync.build_teams_tab(
            spreadsheet_id="sheet-xyz",
            tab_name="Ekipe",
            competition_id=comp.id,
        )

        values = recorder.last_values
        assert values, "build_teams_tab wrote nothing"

        # Row 0: group header row. Row 1: column subheaders. Row 2: first
        # team. New columns Clani + St. clanov go between Rod/Org and the
        # points column, so each block is 6 wide.
        subheader = values[1]
        assert "Člani" in subheader, f"Members header missing: {subheader}"
        assert "Št. članov" in subheader, f"Members count header missing: {subheader}"

        team_row = values[2]
        # Block columns: number, name, org, members, count, points
        assert team_row[0] == 101
        assert team_row[1] == "POŠTARJI"
        assert team_row[3] == "Ana Novak, Bor Kovač"
        assert team_row[4] == 2


def test_build_teams_tab_empty_members_yields_empty_cell(sheets_app, monkeypatch):
    """A team without TeamMember rows shows an empty Clani cell rather
    than crashing or rendering 'None'."""
    with sheets_app.app_context():
        user = create_user(username="empty-members-admin", role="admin")
        comp = create_competition(name="Empty Members Race")
        add_membership(user, comp, role="admin")
        grp = create_group(comp, name="mGG", prefix="1xx")
        team = create_team(comp, name="Pitoni", number=103)
        assign_team_group(team, grp)
        db.session.commit()

        recorder = _Recorder()
        monkeypatch.setattr(sheets_sync, "get_sheets_client", lambda _a: recorder)
        monkeypatch.setattr(sheets_client_module, "get_sheets_client", lambda _a: recorder)

        sheets_sync.build_teams_tab(
            spreadsheet_id="sheet-xyz",
            tab_name="Ekipe",
            competition_id=comp.id,
        )
        team_row = recorder.last_values[2]
        # Empty list -> empty string in members cell, 0 in count cell.
        assert team_row[3] == ""
        assert team_row[4] == 0
