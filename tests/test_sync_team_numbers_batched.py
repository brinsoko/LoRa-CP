"""sync_all_checkpoint_tabs must batch its writes to stay under the
gunicorn worker timeout.

Earlier implementation did one update_column per (CP × group), which on
a 15-CP × 5-group competition (75 calls) hit the 40-calls/60s
SheetsClient throttle, forced a 60s sleep mid-request, and the
gunicorn worker timed out at 30s returning 500.

The fix batches all column writes per CP into one ws.batch_update call
(~15 API calls total instead of 75). These tests pin both the call
count and the per-CP error tolerance."""

from __future__ import annotations

import pytest

from app.extensions import db
from app.models import (
    CheckpointGroupLink,
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


class _FakeWS:
    def __init__(self, title):
        self.title = title
        self.id = 1
        self.batch_updates: list[tuple[list[dict], str | None]] = []
        self.updates: list[dict] = []

    def batch_update(self, data, value_input_option=None, **_):
        self.batch_updates.append((data, value_input_option))

    def update(self, *, range_name=None, values=None, value_input_option=None, **_):
        self.updates.append({"range_name": range_name, "values": values})

    def clear(self):
        pass


class _FakeSS:
    def __init__(self):
        self.title = "Sync Test Sheet"
        self._ws: dict[str, _FakeWS] = {}

    def add_worksheet(self, *, title, rows, cols, **_):
        ws = _FakeWS(title)
        self._ws[title] = ws
        return ws

    def worksheet(self, title):
        if title not in self._ws:
            # Auto-create so the test fixture doesn't have to pre-build them.
            self._ws[title] = _FakeWS(title)
        return self._ws[title]

    def worksheets(self):
        return list(self._ws.values())


class _FakeClient:
    """Mirrors SheetsClient.batch_update_columns precisely so we can
    assert the per-tab batched behavior."""

    def __init__(self):
        self.spreadsheet = _FakeSS()
        self.batch_update_columns_calls: list[tuple[str, list[dict]]] = []
        self.update_column_calls: list[tuple] = []

        class _GC:
            def open_by_key(_, _key):
                return self.spreadsheet

        self.gc = _GC()

    def _call(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def batch_update_columns(self, spreadsheet_id, tab_name, columns):
        self.batch_update_columns_calls.append((tab_name, list(columns)))

    def update_column(self, spreadsheet_id, tab_name, col, start_row, values):
        # If this gets called we're back on the per-group pattern — fail loud.
        self.update_column_calls.append((tab_name, col, start_row, values))

    def add_tab(self, *args, **kwargs):
        pass

    def set_header_row(self, *args, **kwargs):
        pass

    def update_cell(self, *args, **kwargs):
        pass

    def update_cell_formula(self, *args, **kwargs):
        pass

    def update_column_formula(self, *args, **kwargs):
        pass

    def set_checkbox_validation(self, *args, **kwargs):
        pass


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


def _seed_competition_with_n_cps(n_cps: int, groups_per_cp: int = 5, teams_per_group: int = 4):
    user = create_user(username=f"sync-admin-{n_cps}", role="admin")
    comp = create_competition(name=f"Sync Race {n_cps}")
    add_membership(user, comp, role="admin")
    # Group prefixes 1..N (DB constraint requires team numbers > 0).
    groups = [create_group(comp, name=f"G{i}", prefix=f"{i+1}xx") for i in range(groups_per_cp)]
    for g_idx, g in enumerate(groups):
        for t_idx in range(teams_per_group):
            t = create_team(comp, name=f"{g.name}-T{t_idx}", number=(g_idx + 1) * 100 + t_idx + 1)
            assign_team_group(t, g)
    for i in range(n_cps):
        cp = create_checkpoint(comp, name=f"CP-{i}")
        for g in groups:
            db.session.add(CheckpointGroupLink(group_id=g.id, checkpoint_id=cp.id, position=0))
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
                    "groups": [
                        {"group_id": g.id, "name": g.name, "fields": ["task1"]} for g in groups
                    ],
                },
            )
        )
    db.session.commit()
    return comp


def test_sync_uses_one_batch_call_per_cp_not_one_per_group(sheets_app, monkeypatch):
    """With 5 CPs × 5 groups = 25 (CP,group) pairs, the old code made
    25 update_column calls. The new code must make at most 5 batched
    calls (one per CP)."""
    with sheets_app.app_context():
        comp = _seed_competition_with_n_cps(n_cps=5, groups_per_cp=5, teams_per_group=4)
        fake = _install_fake_client(monkeypatch)
        sheets_sync.sync_all_checkpoint_tabs(competition_id=comp.id)

        assert fake.update_column_calls == [], (
            f"Per-group update_column should not be used anymore; got {len(fake.update_column_calls)} calls"
        )
        # One batched call per CP that has at least one team.
        assert len(fake.batch_update_columns_calls) == 5, (
            f"Expected 5 batch calls (one per CP), got {len(fake.batch_update_columns_calls)}: "
            f"{[c[0] for c in fake.batch_update_columns_calls]}"
        )
        # Each batch call carries 5 column updates (one per group).
        for tab_name, cols in fake.batch_update_columns_calls:
            assert len(cols) == 5, f"{tab_name}: expected 5 group columns, got {len(cols)}"
            # Each column has 4 values (teams_per_group).
            for c in cols:
                assert len(c["values"]) == 4, f"{tab_name} col {c['col']}: expected 4 values, got {len(c['values'])}"


def test_sync_writes_correct_team_numbers_per_group(sheets_app, monkeypatch):
    """The batched payload must carry the right team numbers for each
    group block — regression guard against shuffling rows across groups."""
    with sheets_app.app_context():
        comp = _seed_competition_with_n_cps(n_cps=1, groups_per_cp=3, teams_per_group=3)
        fake = _install_fake_client(monkeypatch)
        sheets_sync.sync_all_checkpoint_tabs(competition_id=comp.id)

        assert len(fake.batch_update_columns_calls) == 1
        tab_name, cols = fake.batch_update_columns_calls[0]
        assert tab_name == "CP-0"
        # Three group blocks. Helper used numbers 0xx, 1xx, 2xx by prefix.
        # Group G0 has prefix "0" => team numbers 0, 1, 2 (0*100+t)
        # Group G1 has prefix "1" => 100, 101, 102
        # Group G2 has prefix "2" => 200, 201, 202
        nums_by_col = {c["col"]: c["values"] for c in cols}
        # Find the column for G0 (start_col=1), G1, G2 — block width = 1+1+1+1 = 4
        # (name + time + task1 + points)
        # So G0 starts at col 1, G1 at col 5, G2 at col 9.
        # Team numbers per helper: (g_idx+1)*100 + t_idx+1
        assert sorted(nums_by_col.keys()) == [1, 5, 9]
        assert nums_by_col[1] == [101, 102, 103]
        assert nums_by_col[5] == [201, 202, 203]
        assert nums_by_col[9] == [301, 302, 303]


def test_sync_continues_when_one_cp_batch_fails(sheets_app, monkeypatch):
    """A bad tab name (or any other Sheets error on one CP) must not
    abort the entire sync. The catch in sync_all_checkpoint_tabs logs
    and moves to the next CP."""
    with sheets_app.app_context():
        comp = _seed_competition_with_n_cps(n_cps=3, groups_per_cp=2, teams_per_group=2)
        fake = _install_fake_client(monkeypatch)

        original = fake.batch_update_columns

        def flaky(spreadsheet_id, tab_name, columns):
            if tab_name == "CP-1":
                raise RuntimeError("simulated sheet edit collision")
            return original(spreadsheet_id, tab_name, columns)

        monkeypatch.setattr(fake, "batch_update_columns", flaky)

        # Should NOT raise.
        sheets_sync.sync_all_checkpoint_tabs(competition_id=comp.id)

        # CP-0 and CP-2 still got their batched call; CP-1 did not (failed mid-call,
        # but the test's flaky() raises before recording, so it's only counted via
        # the surviving CPs).
        tab_names_called = {c[0] for c in fake.batch_update_columns_calls}
        assert tab_names_called == {"CP-0", "CP-2"}, f"got {tab_names_called}"
