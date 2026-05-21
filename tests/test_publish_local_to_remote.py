"""Promote local-only SheetConfigs to a real Google Sheet.

After an admin imports a competition (or runs the local-only wizard),
every SheetConfig.spreadsheet_id has the form "local:N". This batch
flips them to a real spreadsheet ID, creates the remote tabs (headers
+ team numbers + any existing scored data), and builds the summary
tabs (Teams / Arrivals / Score) on top so the spreadsheet is a
self-contained backup of the race state."""

from __future__ import annotations

import pytest

from app.extensions import db
from app.models import (
    CheckpointGroupLink,
    ScoreEntry,
    SheetConfig,
)
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
)


class _FakeWorksheet:
    """gspread.Worksheet stand-in that records writes and exposes update/clear/id."""

    def __init__(self, title: str, sheet_id: int = 1):
        self.title = title
        self.id = sheet_id
        self.updates: list[dict] = []
        self.cleared = False

    def update(self, *, range_name=None, values=None, value_input_option=None, **_):
        self.updates.append(
            {
                "range_name": range_name,
                "values": values,
                "value_input_option": value_input_option,
            }
        )

    def clear(self):
        self.cleared = True


class _FakeSpreadsheet:
    def __init__(self, title="My Sheet"):
        self.title = title
        self._worksheets: dict[str, _FakeWorksheet] = {}
        self.batch_updates: list[dict] = []
        self._next_id = 100

    def add_worksheet(self, *, title, rows, cols, **_):
        if title in self._worksheets:
            raise RuntimeError(f"Worksheet 'WORKSHEET_TITLE_TAKEN' already exists with title {title}")
        ws = _FakeWorksheet(title, sheet_id=self._next_id)
        self._next_id += 1
        self._worksheets[title] = ws
        return ws

    def worksheet(self, title):
        if title not in self._worksheets:
            raise RuntimeError(f"worksheet {title!r} not found")
        return self._worksheets[title]

    def worksheets(self):
        return list(self._worksheets.values())

    def batch_update(self, body):
        self.batch_updates.append(body)

    def del_worksheet(self, ws):
        self._worksheets.pop(ws.title, None)


class _FakeClient:
    """Stand-in for SheetsClient. Exposes the same public surface but
    backed by an in-memory spreadsheet, and records call counts so tests
    can verify throttling / rate-limit posture without hitting the
    network."""

    def __init__(self):
        self.spreadsheet = _FakeSpreadsheet()
        self.call_count = 0

        class _GC:
            def __init__(outer, ss):
                outer.ss = ss

            def open_by_key(outer, key):
                return outer.ss

        self.gc = _GC(self.spreadsheet)

    def _call(self, fn, *args, **kwargs):
        self.call_count += 1
        return fn(*args, **kwargs)

    # public API mirror
    def add_tab(self, spreadsheet_id, title, rows=100, cols=26):
        self.call_count += 1
        return self.gc.open_by_key(spreadsheet_id).add_worksheet(title=title, rows=rows, cols=cols)

    def set_header_row(self, spreadsheet_id, tab_name, headers):
        self.call_count += 1
        ws = self.gc.open_by_key(spreadsheet_id).worksheet(tab_name)
        ws.update(range_name="A1:Z1", values=[headers], value_input_option="USER_ENTERED")
        return ws

    def update_column(self, spreadsheet_id, tab_name, col_index, start_row, values):
        self.call_count += 1
        ws = self.gc.open_by_key(spreadsheet_id).worksheet(tab_name)
        ws.update(
            range_name=f"col{col_index}:{start_row}",
            values=[[v] for v in values],
            value_input_option="USER_ENTERED",
        )

    def update_cell(self, *args, **kwargs):
        self.call_count += 1

    def update_cell_formula(self, *args, **kwargs):
        self.call_count += 1

    def update_column_formula(self, *args, **kwargs):
        self.call_count += 1

    def set_checkbox_validation(self, *args, **kwargs):
        self.call_count += 1


@pytest.fixture
def sheets_app(app_factory):
    application = app_factory(SHEETS_SYNC_ENABLED=True)
    with application.app_context():
        from app.utils.sheets_settings import save_settings

        save_settings({"sync_enabled": True})
        yield application


def _install_fake_client(monkeypatch) -> _FakeClient:
    fake = _FakeClient()

    def _get(_app):
        return fake

    monkeypatch.setattr(sheets_sync, "get_sheets_client", _get)
    monkeypatch.setattr(sheets_client_module, "get_sheets_client", _get)
    return fake


def _seed_imported_competition():
    """Mimic the post-import state: a competition with two groups, two
    checkpoints, four teams, and local-only SheetConfigs pointing at a
    local: sentinel spreadsheet_id."""
    user = create_user(username="publish-admin", role="admin")
    comp = create_competition(name="Imported Race")
    add_membership(user, comp, role="admin")

    grp_a = create_group(comp, name="Alpha", prefix="1xx")
    grp_b = create_group(comp, name="Beta", prefix="2xx")

    cp1 = create_checkpoint(comp, name="CP-One")
    cp2 = create_checkpoint(comp, name="CP-Two")

    t1 = create_team(comp, name="Team-A1", number=101)
    t2 = create_team(comp, name="Team-A2", number=102)
    t3 = create_team(comp, name="Team-B1", number=201)
    t4 = create_team(comp, name="Team-B2", number=202)
    assign_team_group(t1, grp_a)
    assign_team_group(t2, grp_a)
    assign_team_group(t3, grp_b)
    assign_team_group(t4, grp_b)

    db.session.add(CheckpointGroupLink(group_id=grp_a.id, checkpoint_id=cp1.id, position=0))
    db.session.add(CheckpointGroupLink(group_id=grp_a.id, checkpoint_id=cp2.id, position=1))
    db.session.add(CheckpointGroupLink(group_id=grp_b.id, checkpoint_id=cp1.id, position=0))
    db.session.add(CheckpointGroupLink(group_id=grp_b.id, checkpoint_id=cp2.id, position=1))

    local_id = f"local:{comp.id}"
    cfg1 = SheetConfig(
        competition_id=comp.id,
        spreadsheet_id=local_id,
        spreadsheet_name="Local",
        tab_name=cp1.name,
        tab_type="checkpoint",
        checkpoint_id=cp1.id,
        config={
            "arrived_header": "Arr",
            "points_header": "Points",
            "dead_time_header": "Dead",
            "dead_time_enabled": True,
            "time_header": "Time",
            "time_enabled": False,
            "groups": [
                {"group_id": grp_a.id, "name": "Alpha", "fields": ["task1", "task2"]},
                {"group_id": grp_b.id, "name": "Beta", "fields": ["task1"]},
            ],
        },
    )
    cfg2 = SheetConfig(
        competition_id=comp.id,
        spreadsheet_id=local_id,
        spreadsheet_name="Local",
        tab_name=cp2.name,
        tab_type="checkpoint",
        checkpoint_id=cp2.id,
        config={
            "points_header": "Points",
            "dead_time_enabled": False,
            "time_enabled": False,
            "groups": [
                {"group_id": grp_a.id, "name": "Alpha", "fields": ["task"]},
                {"group_id": grp_b.id, "name": "Beta", "fields": ["task"]},
            ],
        },
    )
    db.session.add_all([cfg1, cfg2])
    db.session.commit()

    # Pre-existing score on cp1/team1 to verify backfill into the grid.
    db.session.add(
        ScoreEntry(
            competition_id=comp.id,
            team_id=t1.id,
            checkpoint_id=cp1.id,
            raw_fields={"task1": 7, "task2": 3, "dead_time": 4},
            total=10.0,
        )
    )
    db.session.commit()

    return {
        "comp": comp,
        "cp1": cp1,
        "cp2": cp2,
        "grp_a": grp_a,
        "grp_b": grp_b,
        "t1": t1,
        "local_id": local_id,
    }


def test_publish_rebinds_local_configs_to_remote(sheets_app, monkeypatch):
    with sheets_app.app_context():
        s = _seed_imported_competition()
        fake = _install_fake_client(monkeypatch)

        result = sheets_sync.publish_local_configs_to_spreadsheet(
            competition_id=s["comp"].id,
            spreadsheet_id="REAL-SHEET-ID",
        )

        # Two CP tabs published, no skips, no errors.
        assert result["published"] == 2, result
        assert result["skipped"] == 0
        assert result["errors"] == []

        # Both SheetConfigs now point at the real spreadsheet.
        refreshed = SheetConfig.query.filter_by(competition_id=s["comp"].id).all()
        assert all(c.spreadsheet_id == "REAL-SHEET-ID" for c in refreshed), (
            f"Expected all rebinds, got {[c.spreadsheet_id for c in refreshed]}"
        )
        # The local: sentinel is gone entirely.
        assert (
            SheetConfig.query.filter(SheetConfig.spreadsheet_id.like("local:%")).count() == 0
        )

        # Each CP tab was created on the remote.
        assert "CP-One" in fake.spreadsheet._worksheets
        assert "CP-Two" in fake.spreadsheet._worksheets

        # Summary tabs were built (they call back into build_*_tab which
        # use the same fake client). The function returns the names of
        # successfully built summary tabs.
        # build_arrivals_tab + build_score_tab create tabs that hit the
        # fake; build_teams_tab too. Names come from lang defaults.
        assert len(result["summary_tabs"]) >= 1


def test_publish_backfills_existing_score_into_grid(sheets_app, monkeypatch):
    """The pre-existing ScoreEntry on cp1/Team-A1 must show up in the
    grid we wrote to the remote tab (raw_fields + total), so the
    spreadsheet is a complete backup."""
    with sheets_app.app_context():
        s = _seed_imported_competition()
        fake = _install_fake_client(monkeypatch)

        sheets_sync.publish_local_configs_to_spreadsheet(
            competition_id=s["comp"].id,
            spreadsheet_id="REAL-SHEET-ID",
        )

        ws = fake.spreadsheet.worksheet("CP-One")
        # The last update should be the full grid for this tab.
        full_grid_writes = [u for u in ws.updates if u["range_name"] == "A1"]
        assert full_grid_writes, f"No A1 grid write recorded; got {ws.updates}"
        grid = full_grid_writes[-1]["values"]

        # Header row matches the wizard layout: each group block is
        # [group_name, Dead, fields..., Points] (no Time since
        # time_enabled=False on cp1).
        # Alpha block (5 cols): Alpha | Dead | task1 | task2 | Points
        # Beta  block (4 cols): Beta  | Dead | task1 | Points
        assert grid[0] == ["Alpha", "Dead", "task1", "task2", "Points",
                           "Beta", "Dead", "task1", "Points"]

        # Team-A1 is the first Alpha team -> row index 1 in grid.
        # Layout columns: [num, dead_time, task1, task2, points, beta_num, ...].
        team_a1_row = grid[1]
        assert team_a1_row[0] == 101  # team number
        assert team_a1_row[1] == 4    # dead_time from raw_fields
        assert team_a1_row[2] == 7    # task1 from raw_fields
        assert team_a1_row[3] == 3    # task2 from raw_fields
        assert team_a1_row[4] == 10.0 # ScoreEntry.total
        # Beta side of row 1 (no score for first Beta team, just number).
        assert team_a1_row[5] == 201
        # Empty cells where there's no data.
        assert team_a1_row[6] in ("", None)


def test_publish_is_idempotent_when_rerun(sheets_app, monkeypatch):
    """Re-running publish with the same target finds no more local
    configs and is a clean no-op."""
    with sheets_app.app_context():
        s = _seed_imported_competition()
        _install_fake_client(monkeypatch)

        first = sheets_sync.publish_local_configs_to_spreadsheet(
            competition_id=s["comp"].id, spreadsheet_id="REAL-SHEET-ID"
        )
        assert first["published"] == 2

        second = sheets_sync.publish_local_configs_to_spreadsheet(
            competition_id=s["comp"].id, spreadsheet_id="REAL-SHEET-ID"
        )
        assert second["published"] == 0
        assert any("already point at the target" in e for e in second["errors"]), second


def test_publish_rejects_local_sentinel_target(sheets_app, monkeypatch):
    """The target must be a real Google Sheets ID, not another local:
    placeholder. This catches admins who paste the source ID by mistake."""
    with sheets_app.app_context():
        s = _seed_imported_competition()
        fake = _install_fake_client(monkeypatch)

        result = sheets_sync.publish_local_configs_to_spreadsheet(
            competition_id=s["comp"].id, spreadsheet_id="local:wrong"
        )
        assert result["published"] == 0
        assert any("real Google Sheets ID" in e for e in result["errors"]), result
        # Nothing landed on the fake remote.
        assert fake.spreadsheet._worksheets == {}


def test_publish_continues_when_one_cp_fails(sheets_app, monkeypatch):
    """A failure on one CP must not abort the rest of the batch.
    Successful CPs commit; the failed one is reported in errors and
    still has its local: spreadsheet_id (rolled back)."""
    with sheets_app.app_context():
        s = _seed_imported_competition()
        fake = _install_fake_client(monkeypatch)

        original_add = fake.add_tab

        def flaky_add_tab(spreadsheet_id, title, rows=100, cols=26):
            if title == "CP-Two":
                raise RuntimeError("simulated quota burst")
            return original_add(spreadsheet_id, title, rows=rows, cols=cols)

        monkeypatch.setattr(fake, "add_tab", flaky_add_tab)

        result = sheets_sync.publish_local_configs_to_spreadsheet(
            competition_id=s["comp"].id, spreadsheet_id="REAL-SHEET-ID"
        )
        assert result["published"] == 1
        assert any("CP-Two" in e for e in result["errors"]), result

        # cp1 rebound, cp2 still local
        cfgs = {c.tab_name: c for c in SheetConfig.query.filter_by(competition_id=s["comp"].id).all()}
        assert cfgs["CP-One"].spreadsheet_id == "REAL-SHEET-ID"
        assert cfgs["CP-Two"].spreadsheet_id.startswith("local:")


def test_publish_repoints_configs_already_on_a_different_remote(sheets_app, monkeypatch):
    """User switches the target spreadsheet: configs currently bound to
    SHEET-A get rewritten and rebound to SHEET-B. Previously this was a
    silent no-op because the publish filter only matched 'local:' rows."""
    with sheets_app.app_context():
        s = _seed_imported_competition()
        _install_fake_client(monkeypatch)

        # First publish lands all configs on SHEET-A.
        first = sheets_sync.publish_local_configs_to_spreadsheet(
            competition_id=s["comp"].id, spreadsheet_id="SHEET-A"
        )
        assert first["published"] == 2, first
        assert all(
            c.spreadsheet_id == "SHEET-A"
            for c in SheetConfig.query.filter_by(competition_id=s["comp"].id).all()
        )

        # Now switch to SHEET-B. Every config must rebind to SHEET-B.
        second = sheets_sync.publish_local_configs_to_spreadsheet(
            competition_id=s["comp"].id, spreadsheet_id="SHEET-B"
        )
        assert second["published"] == 2, second
        refreshed = SheetConfig.query.filter_by(competition_id=s["comp"].id).all()
        assert all(c.spreadsheet_id == "SHEET-B" for c in refreshed), (
            f"Expected all configs on SHEET-B, got {[c.spreadsheet_id for c in refreshed]}"
        )


def test_publish_dedupes_duplicate_tab_name_rows(sheets_app, monkeypatch):
    """If two SheetConfig rows share the same tab_name (e.g. the wizard
    ran twice and left both `local:5` and `local:12` placeholders for the
    same checkpoint), publish must drop one before committing instead of
    blowing up on UNIQUE(spreadsheet_id, tab_name).

    Regression: previously this raised sqlite3.IntegrityError per duplicate
    row, ate Sheets API quota until the throttle kicked in, and eventually
    killed the gunicorn worker."""
    with sheets_app.app_context():
        s = _seed_imported_competition()
        _install_fake_client(monkeypatch)

        # Add a second, stale local config that shares CP-One's tab_name.
        # Different spreadsheet_id keeps it within the UNIQUE(spreadsheet_id,
        # tab_name) constraint at insert time, but it WILL collide once we
        # try to re-point it at the target.
        db.session.add(
            SheetConfig(
                competition_id=s["comp"].id,
                spreadsheet_id="local:stale",
                spreadsheet_name="Stale",
                tab_name=s["cp1"].name,  # same tab_name as the live cfg1
                tab_type="checkpoint",
                checkpoint_id=s["cp1"].id,
                config={"groups": []},
            )
        )
        db.session.commit()

        result = sheets_sync.publish_local_configs_to_spreadsheet(
            competition_id=s["comp"].id,
            spreadsheet_id="REAL-SHEET-ID",
        )

        assert result["errors"] == [], result
        # Two distinct tab_names land on the remote (CP-One + CP-Two). The
        # stale duplicate is dropped, counted as skipped rather than
        # published.
        assert result["published"] == 2, result
        assert result["skipped"] >= 1, result

        remaining = SheetConfig.query.filter_by(competition_id=s["comp"].id).all()
        # No duplicates: every (spreadsheet_id, tab_name) pair appears once.
        seen = set()
        for c in remaining:
            key = (c.spreadsheet_id, c.tab_name)
            assert key not in seen, f"duplicate slot after publish: {key}"
            seen.add(key)
        # And no orphan local: rows are left behind.
        assert all(not c.spreadsheet_id.startswith("local:") for c in remaining), (
            [c.spreadsheet_id for c in remaining]
        )


def test_publish_is_noop_when_sheets_sync_disabled(app_factory, monkeypatch):
    application = app_factory(SHEETS_SYNC_ENABLED=False)
    with application.app_context():
        from app.utils.sheets_settings import save_settings
        save_settings({"sync_enabled": False})

        s = _seed_imported_competition()

        called = {"flag": False}

        def fail_if_called(_app):
            called["flag"] = True
            raise AssertionError("Client should not be requested when sync is disabled")

        monkeypatch.setattr(sheets_sync, "get_sheets_client", fail_if_called)

        result = sheets_sync.publish_local_configs_to_spreadsheet(
            competition_id=s["comp"].id, spreadsheet_id="REAL-SHEET-ID"
        )
        assert result["published"] == 0
        assert any("disabled" in e.lower() for e in result["errors"]), result
        assert called["flag"] is False
