"""Pre-launch hardening regressions for app/utils/sheets_sync.py.

Three bugs, all on the live-sync path:

1. update_checkpoint_scores_sync wrote scored_at into the arrival-time
   cell of a time-enabled CP whenever the submitted values carried no
   explicit time entry - scoring a team at 10:25 overwrote the 10:00
   arrival that mark_arrival_checkbox_sync had written, so the sheet's
   time-race formulas diverged from the app's checkin-based result.

2. sync_all_checkpoint_tabs rewrote only the team-number column per
   group block; after a renumber the numbers moved while the
   dead_time/time/field/points cells kept their old row order, pairing
   every number with another team's scores (and the Score tab's
   MATCH(team_number) lookups credited the wrong team).

3. mark_arrival_checkbox_sync fell back to naive *local*
   datetime.now() while every caller passes naive-UTC checkin
   timestamps; the fallback must be utcnow_naive so the time column
   stays on one clock.
"""

from __future__ import annotations

from datetime import datetime

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
    assign_team_group,
    create_checkin,
    create_checkpoint,
    create_competition,
    create_group,
    create_team,
)


class _GridRecordingClient:
    """Fake SheetsClient that applies batch_update_columns writes onto an
    in-memory (row, col) -> value grid per tab, so tests can assert the
    final sheet state rather than just the call shapes."""

    def __init__(self):
        self.cells: dict[str, dict[tuple[int, int], object]] = {}
        self.batch_update_columns_calls: list[tuple[str, list[dict]]] = []
        self.gc = self

    def open_by_key(self, _key):
        return self

    def worksheet(self, _title):
        return self

    def _call(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def batch_update_columns(self, _sid, tab_name, columns):
        self.batch_update_columns_calls.append((tab_name, list(columns)))
        grid = self.cells.setdefault(tab_name, {})
        for spec in columns:
            for offset, value in enumerate(spec["values"]):
                grid[(spec["start_row"] + offset, spec["col"])] = value


@pytest.fixture
def sheets_app(app_factory):
    application = app_factory(SHEETS_SYNC_ENABLED=True)
    with application.app_context():
        from app.utils.sheets_settings import save_settings

        save_settings({"sync_enabled": True})
        yield application


def _install_fake_client(monkeypatch) -> _GridRecordingClient:
    fake = _GridRecordingClient()

    def _get(_app):
        return fake

    monkeypatch.setattr(sheets_sync, "get_sheets_client", _get)
    monkeypatch.setattr(sheets_client_module, "get_sheets_client", _get)
    return fake


def _seed_time_cp(*, points_formula: bool = False, include_group_id: bool = True):
    """One group, one time-enabled CP, two teams.

    Tab layout per the config below: col 1 = team number, col 2 = Time
    (arrival), col 3 = task1, col 4 = Points. Team-A (101) on row 2,
    Team-B (102) on row 3.
    """
    comp = create_competition()
    grp = create_group(comp, name="Alpha", prefix="1xx")
    cp = create_checkpoint(comp, name="CP-Time")
    team_a = create_team(comp, name="Team-A", number=101)
    team_b = create_team(comp, name="Team-B", number=102)
    assign_team_group(team_a, grp)
    assign_team_group(team_b, grp)
    db.session.add(CheckpointGroupLink(group_id=grp.id, checkpoint_id=cp.id, position=0))
    group_blob: dict = {"name": "Alpha", "fields": ["task1"]}
    if include_group_id:
        group_blob["group_id"] = grp.id
    if points_formula:
        group_blob["points_formula"] = True
    db.session.add(
        SheetConfig(
            competition_id=comp.id,
            spreadsheet_id="REAL-SHEET",
            spreadsheet_name="Sheet",
            tab_name=cp.name,
            tab_type="checkpoint",
            checkpoint_id=cp.id,
            config={
                "points_header": "Points",
                "dead_time_enabled": False,
                "time_enabled": True,
                "time_header": "Time",
                "groups": [group_blob],
            },
        )
    )
    db.session.commit()
    return {"comp": comp, "grp": grp, "cp": cp, "team_a": team_a, "team_b": team_b}


# ---------------------------------------------------------------------------
# Fix 1: scoring must not clobber the arrival-time cell
# ---------------------------------------------------------------------------


def test_scoring_does_not_overwrite_arrival_time_cell(sheets_app, monkeypatch):
    with sheets_app.app_context():
        s = _seed_time_cp()
        fake = _install_fake_client(monkeypatch)

        arrival = datetime(2026, 6, 20, 10, 0, 0)
        sheets_sync.mark_arrival_checkbox_sync(s["team_a"].id, s["cp"].id, arrival)
        grid = fake.cells["CP-Time"]
        assert grid[(2, 2)] == "2026-06-20 10:00:00"

        scored_at = datetime(2026, 6, 20, 10, 25, 0)
        sheets_sync.update_checkpoint_scores_sync(
            s["team_a"].id, s["cp"].id, "Alpha", {"task1": 5, "points": 80}, scored_at
        )
        # task1 and Points were written...
        assert grid[(2, 3)] == 5
        assert grid[(2, 4)] == 80
        # ...but the arrival cell still holds the check-in timestamp.
        assert grid[(2, 2)] == "2026-06-20 10:00:00", (
            f"arrival cell was overwritten by the scoring time: {grid[(2, 2)]}"
        )


def test_explicit_time_value_still_writes_time_cell(sheets_app, monkeypatch):
    """An explicit time entry in the submitted values is a deliberate
    correction and must still reach the time cell."""
    with sheets_app.app_context():
        s = _seed_time_cp()
        fake = _install_fake_client(monkeypatch)

        sheets_sync.update_checkpoint_scores_sync(
            s["team_a"].id,
            s["cp"].id,
            "Alpha",
            {"time": "2026-06-20 09:55:00", "task1": 5},
            datetime(2026, 6, 20, 10, 25, 0),
        )
        assert fake.cells["CP-Time"][(2, 2)] == "2026-06-20 09:55:00"


# ---------------------------------------------------------------------------
# Fix 2: sync_all_checkpoint_tabs keeps scores aligned with numbers
# ---------------------------------------------------------------------------


def test_sync_keeps_scores_on_same_row_as_number_after_renumber(sheets_app, monkeypatch):
    with sheets_app.app_context():
        s = _seed_time_cp()
        comp, cp = s["comp"], s["cp"]
        team_a, team_b = s["team_a"], s["team_b"]
        create_checkin(comp, team_a, cp, timestamp=datetime(2026, 6, 20, 8, 0, 0))
        create_checkin(comp, team_b, cp, timestamp=datetime(2026, 6, 20, 8, 5, 0))
        db.session.add_all(
            [
                ScoreEntry(
                    competition_id=comp.id,
                    team_id=team_a.id,
                    checkpoint_id=cp.id,
                    raw_fields={"task1": 7},
                    total=70.0,
                ),
                ScoreEntry(
                    competition_id=comp.id,
                    team_id=team_b.id,
                    checkpoint_id=cp.id,
                    raw_fields={"task1": 3},
                    total=30.0,
                ),
            ]
        )
        # Renumber: swap, so Team-B (now 101) sorts to row 2.
        team_a.number = 102
        team_b.number = 101
        db.session.commit()

        fake = _install_fake_client(monkeypatch)
        sheets_sync.sync_all_checkpoint_tabs(competition_id=comp.id)

        # Batching guarantee unchanged: one batched call for the one CP.
        assert len(fake.batch_update_columns_calls) == 1

        grid = fake.cells["CP-Time"]
        # Row 2 is now Team-B: its number, ITS checkin time and ITS score.
        assert grid[(2, 1)] == 101
        assert grid[(2, 2)] == "2026-06-20 08:05:00"
        assert grid[(2, 3)] == 3
        assert grid[(2, 4)] == 30.0
        # Row 3 is Team-A with its own data, not leftovers from row 2.
        assert grid[(3, 1)] == 102
        assert grid[(3, 2)] == "2026-06-20 08:00:00"
        assert grid[(3, 3)] == 7
        assert grid[(3, 4)] == 70.0


def test_sync_skips_formula_points_column(sheets_app, monkeypatch):
    """A points_formula group keeps its published formulas: routing them
    through batch_update_columns would escape the leading '=' into
    literal text. The formulas read raw cells in their own row, so they
    recompute correctly once the rest of the block is rewritten."""
    with sheets_app.app_context():
        s = _seed_time_cp(points_formula=True)
        fake = _install_fake_client(monkeypatch)

        sheets_sync.sync_all_checkpoint_tabs(competition_id=s["comp"].id)

        grid = fake.cells["CP-Time"]
        # Number, time and task1 columns were rewritten...
        assert (2, 1) in grid and (2, 2) in grid and (2, 3) in grid
        # ...the Points column was left to its formulas.
        assert (2, 4) not in grid and (3, 4) not in grid, (
            "formula-driven Points column must not be rewritten"
        )


def test_sync_name_only_config_falls_back_to_numbers_only(sheets_app, monkeypatch):
    """Legacy configs that identify groups only by name cannot be
    rebuilt by the grid builder (it keys blocks by group_id). They keep
    the old numbers-only refresh instead of blanking the block."""
    with sheets_app.app_context():
        s = _seed_time_cp(include_group_id=False)
        fake = _install_fake_client(monkeypatch)

        sheets_sync.sync_all_checkpoint_tabs(competition_id=s["comp"].id)

        assert len(fake.batch_update_columns_calls) == 1
        _tab, cols = fake.batch_update_columns_calls[0]
        assert [c["col"] for c in cols] == [1], cols
        assert cols[0]["values"] == [101, 102]


# ---------------------------------------------------------------------------
# Fix 3: mark_arrival fallback timestamp is naive UTC
# ---------------------------------------------------------------------------


def test_mark_arrival_fallback_uses_naive_utc(sheets_app, monkeypatch):
    with sheets_app.app_context():
        s = _seed_time_cp()
        fake = _install_fake_client(monkeypatch)
        fixed = datetime(2026, 6, 20, 7, 30, 0)
        monkeypatch.setattr(sheets_sync, "utcnow_naive", lambda: fixed)

        sheets_sync.mark_arrival_checkbox_sync(s["team_a"].id, s["cp"].id, None)

        assert fake.cells["CP-Time"][(2, 2)] == "2026-06-20 07:30:00"
