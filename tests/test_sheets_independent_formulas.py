"""Phase 2: the published spreadsheet must compute the final score on
its own from raw judge inputs + check-in timestamps, with no live
dependency on the LoRa-KT scoring engine.

Three new behaviors:

1. Per-CP `Points` cell is emitted as a *formula* on the publish path
   when the ScoreRule is expressible (multiplier, mapping, deviation,
   raw sum). The formula reads the field cells in the same row, so a
   manual edit to a raw field on the sheet propagates through Points.

2. `update_checkpoint_scores_sync` honors a per-group `points_formula`
   flag on SheetConfig.config and skips writing the Points cell when
   set, so live score submissions don't clobber the formula.

3. The Score summary tab grows two new columns — Časovnica
   (Article 39) and Found-points (Article 38) — driven by formulas
   that look up Time-column timestamps across the per-CP tabs.
"""

from __future__ import annotations

import pytest

from app.extensions import db
from app.models import (
    CheckpointGroupLink,
    GlobalScoreRule,
    ScoreEntry,
    ScoreRule,
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

# ---------------------------------------------------------------------------
# Unit tests for the rule -> formula translator
# ---------------------------------------------------------------------------


def test_field_rule_to_formula_raw_field_is_passthrough():
    assert sheets_sync._field_rule_to_formula(None, "D2") == "D2"
    assert sheets_sync._field_rule_to_formula({}, "D2") == "D2"


def test_field_rule_to_formula_multiplier():
    assert sheets_sync._field_rule_to_formula(
        {"type": "multiplier", "factor": 20}, "D2"
    ) == "(D2*20)"
    # Non-integer factor stays as float; integers render without trailing .0
    assert sheets_sync._field_rule_to_formula(
        {"type": "multiplier", "factor": 2.5}, "D2"
    ).startswith("(D2*")


def test_field_rule_to_formula_mapping_binary_0_50():
    """The competition's prihod_pod_kotom: 0->0, 1->50 binary."""
    formula = sheets_sync._field_rule_to_formula(
        {"type": "mapping", "map": {"0": 0, "1": 50}}, "D2"
    )
    # Output is an IFS chain with TRUE; 0 fallback. Spaces after each ';'.
    assert formula == "IFS(D2=0; 0; D2=1; 50; TRUE; 0)", formula


def test_field_rule_to_formula_deviation_guards_empty_cell():
    """The G substitute task: stone_kg target=1.0, max=100, penalty 5
    per 0.05 kg. Empty cell must return 0, not the spurious penalty
    that ABS(0-1) would produce."""
    formula = sheets_sync._field_rule_to_formula(
        {
            "type": "deviation",
            "target": 1.0,
            "max_points": 100,
            "penalty_points": 5,
            "penalty_distance": 0.05,
            "min_points": 0,
        },
        "D2",
    )
    assert formula.startswith('IF(D2="";'), formula
    assert "MAX(100-ABS(D2-1)/" in formula
    assert "MAX(0; ({} - {})".format("", "") not in formula  # sanity: no broken substitution


def test_field_rule_to_formula_unsupported_returns_none():
    """time_race nested inside field_rules can't be expressed; fall back."""
    assert sheets_sync._field_rule_to_formula(
        {"type": "time_race", "start_checkpoint_id": 1}, "D2"
    ) is None


def test_points_formula_from_rule_sums_multiple_fields():
    """vesla = veslo_izgled + veslo_dimenzija (both raw 0-50)."""
    rule = {
        "field_rules": {"veslo_izgled": {}, "veslo_dimenzija": {}},
        "total_fields": ["veslo_izgled", "veslo_dimenzija"],
    }
    formula = sheets_sync._points_formula_from_rule(
        rule, {"veslo_izgled": 3, "veslo_dimenzija": 4}, row=5
    )
    assert formula == "=C5+D5"


def test_points_formula_from_rule_time_race_returns_none():
    """time_race CPs (hitrostna etapa) stay system-dependent."""
    rule = {
        "time_race": {"start_checkpoint_id": 1, "end_checkpoint_id": 2, "min_points": 10, "max_points": 100}
    }
    assert sheets_sync._points_formula_from_rule(rule, {}, row=5) is None


def test_points_formula_from_rule_drops_dead_time_from_total():
    """The app's _compute_total excludes 'dead_time' from total_fields;
    the spreadsheet formula must do the same so dead time penalties
    are accounted for only via the Časovnica column."""
    rule = {
        "field_rules": {"dead_time": {}, "points": {}},
        "total_fields": ["dead_time", "points"],
    }
    # Field columns: dead_time at col 2, points at col 5.
    formula = sheets_sync._points_formula_from_rule(
        rule, {"dead_time": 2, "points": 5}, row=3
    )
    # Only the points cell contributes (E3); dead_time (B3) is excluded.
    assert formula == "=E3"


# ---------------------------------------------------------------------------
# Integration tests against the fake-client publish path
# ---------------------------------------------------------------------------


class _FakeWorksheet:
    def __init__(self, title):
        self.title = title
        self.id = 1
        self.updates: list[dict] = []

    def update(self, *, range_name=None, values=None, value_input_option=None, **_):
        self.updates.append({"range_name": range_name, "values": values})

    def clear(self):
        pass


class _FakeSpreadsheet:
    def __init__(self, title="Test Sheet"):
        self.title = title
        self._worksheets: dict[str, _FakeWorksheet] = {}

    def add_worksheet(self, *, title, rows, cols, **_):
        if title in self._worksheets:
            raise RuntimeError(f"Worksheet already exists with title {title}")
        ws = _FakeWorksheet(title)
        self._worksheets[title] = ws
        return ws

    def worksheet(self, title):
        return self._worksheets[title]

    def worksheets(self):
        return list(self._worksheets.values())

    def batch_update(self, body):
        pass


class _FakeClient:
    def __init__(self):
        self.spreadsheet = _FakeSpreadsheet()

        class _GC:
            def open_by_key(_, _key):
                return self.spreadsheet

        self.gc = _GC()

    def _call(self, fn, *args, **kwargs):
        return fn(*args, **kwargs)

    def add_tab(self, _sid, title, rows=100, cols=26):
        return self.spreadsheet.add_worksheet(title=title, rows=rows, cols=cols)

    def set_header_row(self, _sid, tab_name, headers):
        ws = self.spreadsheet.worksheet(tab_name)
        ws.update(range_name="A1:Z1", values=[headers], value_input_option="USER_ENTERED")
        return ws

    def update_column(self, _sid, tab_name, col_idx, start_row, values):
        ws = self.spreadsheet.worksheet(tab_name)
        ws.update(
            range_name=f"col{col_idx}:{start_row}",
            values=[[v] for v in values],
            value_input_option="USER_ENTERED",
        )

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


def _seed_competition_with_rule(rule_blob):
    """Set up: one group, one CP with two field-columns, one team, one
    ScoreRule with the given blob. Returns the SheetConfig + comp."""
    user = create_user(username="phase2-admin", role="admin")
    comp = create_competition(name="Phase 2 Race")
    add_membership(user, comp, role="admin")
    grp = create_group(comp, name="Alpha", prefix="1xx")
    cp = create_checkpoint(comp, name="CP-One")
    team = create_team(comp, name="Team-A1", number=101)
    assign_team_group(team, grp)
    db.session.add(CheckpointGroupLink(group_id=grp.id, checkpoint_id=cp.id, position=0))
    cfg = SheetConfig(
        competition_id=comp.id,
        spreadsheet_id=f"local:{comp.id}",
        spreadsheet_name="Local",
        tab_name=cp.name,
        tab_type="checkpoint",
        checkpoint_id=cp.id,
        config={
            "points_header": "Points",
            "dead_time_enabled": False,
            "time_enabled": False,
            "groups": [
                {"group_id": grp.id, "name": "Alpha", "fields": ["task1", "task2"]},
            ],
        },
    )
    rule = ScoreRule(
        competition_id=comp.id,
        checkpoint_id=cp.id,
        group_id=grp.id,
        rules=rule_blob,
    )
    db.session.add_all([cfg, rule])
    db.session.commit()
    return {"comp": comp, "cp": cp, "grp": grp, "team": team, "cfg": cfg}


def test_publish_emits_formula_in_points_cell_for_simple_rule(sheets_app, monkeypatch):
    """A multiplier rule (×20) means Points cell = =D2*20, not the
    raw ScoreEntry.total. So a manual edit to D2 on the sheet
    propagates through Points without our system."""
    with sheets_app.app_context():
        s = _seed_competition_with_rule(
            {
                "field_rules": {
                    "task1": {"type": "multiplier", "factor": 20},
                    "task2": {},  # raw
                },
                "total_fields": ["task1", "task2"],
            }
        )
        # Seed a score so we can also assert backfill uses the formula
        # (not the precomputed total).
        db.session.add(
            ScoreEntry(
                competition_id=s["comp"].id,
                team_id=s["team"].id,
                checkpoint_id=s["cp"].id,
                raw_fields={"task1": 7, "task2": 3},
                total=143.0,  # pre-existing computed total (irrelevant: formula wins)
            )
        )
        db.session.commit()

        fake = _install_fake_client(monkeypatch)
        result = sheets_sync.publish_local_configs_to_spreadsheet(
            competition_id=s["comp"].id, spreadsheet_id="REAL-SHEET"
        )
        assert result["published"] == 1, result

        ws = fake.spreadsheet.worksheet("CP-One")
        # Pick the A1 write (the grid one — there's also a separate
        # set_header_row write but the A1 one carries the full grid).
        full_writes = [u for u in ws.updates if u["range_name"] == "A1"]
        grid = full_writes[-1]["values"]
        # Layout: [team_num, task1, task2, points] for a single group block.
        team_row = grid[1]
        assert team_row[0] == 101
        assert team_row[1] == 7
        assert team_row[2] == 3
        # Points cell is a formula, not the literal 143.0.
        assert isinstance(team_row[3], str) and team_row[3].startswith("=")
        # Specifically: task1*20 + task2 with B/C as raw field columns.
        assert "(B2*20)" in team_row[3]
        assert "C2" in team_row[3]
        # The pre-existing 143.0 must NOT be the value written.
        assert team_row[3] != 143.0

        # SheetConfig has the points_formula flag set per group so
        # update_checkpoint_scores_sync knows to skip the Points cell.
        refreshed_cfg = SheetConfig.query.filter_by(
            competition_id=s["comp"].id, tab_name="CP-One"
        ).one()
        groups_blob = refreshed_cfg.config["groups"]
        assert groups_blob[0].get("points_formula") is True


def test_publish_leaves_points_raw_when_rule_is_time_race(sheets_app, monkeypatch):
    """Hitrostna etapa CPs (time_race rule) can't be expressed as a
    static formula — the Points cell stays system-written. The
    points_formula flag must NOT be set so update_checkpoint_scores_sync
    keeps writing the cell."""
    with sheets_app.app_context():
        s = _seed_competition_with_rule(
            {
                "time_race": {
                    "start_checkpoint_id": 999,
                    "end_checkpoint_id": 998,
                    "min_points": 10,
                    "max_points": 100,
                }
            }
        )
        db.session.add(
            ScoreEntry(
                competition_id=s["comp"].id,
                team_id=s["team"].id,
                checkpoint_id=s["cp"].id,
                raw_fields={"task1": "", "task2": ""},
                total=55.0,
            )
        )
        db.session.commit()
        fake = _install_fake_client(monkeypatch)

        sheets_sync.publish_local_configs_to_spreadsheet(
            competition_id=s["comp"].id, spreadsheet_id="REAL-SHEET"
        )

        ws = fake.spreadsheet.worksheet("CP-One")
        grid = [u for u in ws.updates if u["range_name"] == "A1"][-1]["values"]
        team_row = grid[1]
        # Points stays as the raw system-written number.
        assert team_row[3] == 55.0

        # And the flag is NOT set.
        refreshed_cfg = SheetConfig.query.filter_by(
            competition_id=s["comp"].id, tab_name="CP-One"
        ).one()
        assert refreshed_cfg.config["groups"][0].get("points_formula") in (False, None)


def test_update_checkpoint_scores_sync_skips_points_when_flag_set(sheets_app, monkeypatch):
    """The crucial safety: a live score submission must not overwrite
    the Points formula. With points_formula=True on the group blob,
    update_checkpoint_scores_sync writes the field cells but skips the
    Points cell."""
    with sheets_app.app_context():
        user = create_user(username="sync-admin", role="admin")
        comp = create_competition(name="Sync Race")
        add_membership(user, comp, role="admin")
        grp = create_group(comp, name="Alpha", prefix="1xx")
        cp = create_checkpoint(comp, name="CP-Sync")
        team = create_team(comp, name="T1", number=101)
        assign_team_group(team, grp)
        db.session.add(
            SheetConfig(
                competition_id=comp.id,
                spreadsheet_id="REAL-SHEET",
                spreadsheet_name="Sheet",
                tab_name="CP-Sync",
                tab_type="checkpoint",
                checkpoint_id=cp.id,
                config={
                    "points_header": "Points",
                    "dead_time_enabled": False,
                    "time_enabled": False,
                    "groups": [
                        {
                            "group_id": grp.id,
                            "name": "Alpha",
                            "fields": ["task1"],
                            "points_formula": True,  # <-- the flag we're testing
                        }
                    ],
                },
            )
        )
        db.session.commit()

        # Track every cell write to verify we wrote task1 but not the
        # Points column. update_checkpoint_scores_sync now batches per
        # cfg via batch_update_columns, so the mock unpacks the column
        # spec list into the same (row, col, value) tuples the original
        # update_cell path emitted — keeps the assertions API-shape
        # agnostic.
        written_cells: list[tuple[int, int, object]] = []

        class _RecordingClient:
            def __init__(self):
                self.gc = self

            def open_by_key(self, *_):
                return self

            def worksheet(self, *_):
                return self

            def _call(self, fn, *args, **kwargs):
                return fn(*args, **kwargs)

            def update_cell(self, _sid, _tab, row, col, value):
                written_cells.append((row, col, value))

            def batch_update_columns(self, _sid, _tab, columns):
                for spec in columns:
                    col = spec["col"]
                    start_row = spec["start_row"]
                    for offset, v in enumerate(spec["values"]):
                        written_cells.append((start_row + offset, col, v))

            def update_column(self, *args, **kwargs):
                pass

        recorder = _RecordingClient()
        monkeypatch.setattr(sheets_sync, "get_sheets_client", lambda _app: recorder)

        sheets_sync.update_checkpoint_scores_sync(
            team_id=team.id,
            checkpoint_id=cp.id,
            group_name="Alpha",
            values={"task1": 9, "points": 180},
        )

        # task1 (col 2 in the Alpha block: [name, task1, points]) was written;
        # Points (col 3) was NOT written.
        cols_written = {c[1] for c in written_cells}
        assert 2 in cols_written, f"task1 wasn't written: {written_cells}"
        assert 3 not in cols_written, (
            f"Points cell was overwritten despite points_formula flag: {written_cells}"
        )


def test_update_checkpoint_scores_sync_still_writes_points_without_flag(sheets_app, monkeypatch):
    """Backwards-compat: SheetConfigs without the flag (legacy or
    time_race CPs) keep getting Points written as before."""
    with sheets_app.app_context():
        user = create_user(username="nofl-admin", role="admin")
        comp = create_competition(name="NoFlag Race")
        add_membership(user, comp, role="admin")
        grp = create_group(comp, name="Alpha", prefix="1xx")
        cp = create_checkpoint(comp, name="CP-NoFlag")
        team = create_team(comp, name="T1", number=101)
        assign_team_group(team, grp)
        db.session.add(
            SheetConfig(
                competition_id=comp.id,
                spreadsheet_id="REAL-SHEET",
                spreadsheet_name="Sheet",
                tab_name="CP-NoFlag",
                tab_type="checkpoint",
                checkpoint_id=cp.id,
                config={
                    "points_header": "Points",
                    "dead_time_enabled": False,
                    "time_enabled": False,
                    "groups": [
                        {"group_id": grp.id, "name": "Alpha", "fields": ["task1"]},
                    ],
                },
            )
        )
        db.session.commit()
        written: list[tuple[int, int, object]] = []

        class _Rec:
            def __init__(self):
                self.gc = self

            def open_by_key(self, *_):
                return self

            def worksheet(self, *_):
                return self

            def _call(self, fn, *args, **kwargs):
                return fn(*args, **kwargs)

            def update_cell(self, _sid, _tab, row, col, value):
                written.append((row, col, value))

            def batch_update_columns(self, _sid, _tab, columns):
                # Match the batched API path the sync now uses; unpack
                # each column spec into (row, col, value) so existing
                # assertions on `written` remain meaningful.
                for spec in columns:
                    col = spec["col"]
                    start_row = spec["start_row"]
                    for offset, v in enumerate(spec["values"]):
                        written.append((start_row + offset, col, v))

        monkeypatch.setattr(sheets_sync, "get_sheets_client", lambda _app: _Rec())
        sheets_sync.update_checkpoint_scores_sync(
            team.id, cp.id, "Alpha", values={"task1": 5, "points": 80}
        )
        cols_written = {c[1] for c in written}
        assert 3 in cols_written, f"Points should be written: {written}"


def test_score_tab_found_formula_excludes_virtual_checkpoints(sheets_app, monkeypatch):
    """Virtual CPs (Topo&Vrisovanje, Lokostrelstvo) carry no physical
    arrival — the in-app _compute_global_contrib naturally skips them
    (no Checkin row gets auto-created for virtual CPs), so the
    spreadsheet's Found formula must too. Otherwise a team that scored
    Topo would get +100 on the sheet but not in the app."""
    with sheets_app.app_context():
        user = create_user(username="virt-excl-admin", role="admin")
        comp = create_competition(name="Virtual Excl Race")
        add_membership(user, comp, role="admin")
        grp = create_group(comp, name="Alpha", prefix="1xx")
        start = create_checkpoint(comp, name="Start")
        cilj = create_checkpoint(comp, name="Cilj")
        mid = create_checkpoint(comp, name="CP-Mid")
        # Virtual CPs — same shape as the real race's Topo&Vrisovanje
        # and Lokostrelstvo.
        topo = create_checkpoint(comp, name="Topo&Vrisovanje")
        topo.is_virtual = True
        loko = create_checkpoint(comp, name="Lokostrelstvo")
        loko.is_virtual = True
        team = create_team(comp, name="T1", number=101)
        assign_team_group(team, grp)
        for pos, cp in enumerate([start, topo, mid, loko, cilj]):
            db.session.add(CheckpointGroupLink(group_id=grp.id, checkpoint_id=cp.id, position=pos))
        # SheetConfigs with time_enabled so the Time column exists
        # (otherwise the formula short-circuits on missing column).
        for cp in (start, topo, mid, loko, cilj):
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
                        "groups": [{"group_id": grp.id, "name": "Alpha", "fields": []}],
                    },
                )
            )
        db.session.add(
            GlobalScoreRule(
                competition_id=comp.id,
                group_id=grp.id,
                rules={
                    "time": {
                        "start_checkpoint_id": start.id,
                        "end_checkpoint_id": cilj.id,
                        "max_points": 195,
                        "threshold_minutes": 195,
                        "penalty_minutes": 1,
                        "penalty_points": 2,
                        "min_points": 0,
                    },
                    "found": {
                        "points_per": 100,
                        "exclude_start_checkpoint": True,
                        "exclude_end_checkpoint": True,
                    },
                },
            )
        )
        db.session.commit()
        fake = _install_fake_client(monkeypatch)

        sheets_sync.build_score_tab(
            "REAL-SHEET", tab_name="Score", competition_id=comp.id, include_dead_time_sum=False
        )
        ws = fake.spreadsheet.worksheet("Score")
        grid = ws.updates[-1]["values"]
        header = grid[0]
        found_idx = next(i for i, h in enumerate(header) if "Najden" in str(h))
        team_row = grid[1]
        found_cell = team_row[found_idx]
        assert isinstance(found_cell, str)
        # Only CP-Mid is a physical CP between Start and Cilj. Topo and
        # Lokostrelstvo are virtual and excluded; Start and Cilj are
        # excluded via the exclude_start/end flags.
        assert "'CP-Mid'!" in found_cell, found_cell
        assert "'Topo&Vrisovanje'!" not in found_cell, (
            f"Topo (virtual) leaked into Found formula: {found_cell}"
        )
        assert "'Lokostrelstvo'!" not in found_cell, (
            f"Lokostrelstvo (virtual) leaked into Found formula: {found_cell}"
        )
        # Start and Cilj also stay out (already covered by exclude_* flags).
        assert "'Start'!" not in found_cell
        assert "'Cilj'!" not in found_cell


def test_score_tab_has_casovnica_and_found_columns(sheets_app, monkeypatch):
    """build_score_tab emits Časovnica + Found-points columns whose
    formulas reach into the per-CP tabs' Time columns. With a
    GlobalScoreRule referencing real Start/Cilj CPs, the formulas use
    INDEX/MATCH lookups; the Total formula sums all three contributions."""
    with sheets_app.app_context():
        user = create_user(username="score-tab-admin", role="admin")
        comp = create_competition(name="Score Tab Race")
        add_membership(user, comp, role="admin")
        grp = create_group(comp, name="Alpha", prefix="1xx")
        start = create_checkpoint(comp, name="Start")
        cilj = create_checkpoint(comp, name="Cilj")
        mid = create_checkpoint(comp, name="CP-Mid")
        team = create_team(comp, name="T1", number=101)
        assign_team_group(team, grp)
        for pos, cp in enumerate([start, mid, cilj]):
            db.session.add(CheckpointGroupLink(group_id=grp.id, checkpoint_id=cp.id, position=pos))
        # SheetConfigs for each CP with time_enabled so the Time column
        # exists for the formulas to reference.
        for cp in (start, mid, cilj):
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
                            {"group_id": grp.id, "name": "Alpha", "fields": []},
                        ],
                    },
                )
            )
        # Global rule wiring Start -> Cilj with the user's actual
        # numbers (195 min threshold, -2 per min, found=100 with
        # exclude_start + exclude_end).
        db.session.add(
            GlobalScoreRule(
                competition_id=comp.id,
                group_id=grp.id,
                rules={
                    "time": {
                        "start_checkpoint_id": start.id,
                        "end_checkpoint_id": cilj.id,
                        "max_points": 195,
                        "threshold_minutes": 195,
                        "penalty_minutes": 1,
                        "penalty_points": 2,
                        "min_points": 0,
                    },
                    "found": {
                        "points_per": 100,
                        "exclude_start_checkpoint": True,
                        "exclude_end_checkpoint": True,
                    },
                },
            )
        )
        db.session.commit()
        fake = _install_fake_client(monkeypatch)
        # Pre-create the Score tab name so add_worksheet doesn't collide
        # on a second call (build_score_tab creates the tab itself).
        sheets_sync.build_score_tab(
            "REAL-SHEET", tab_name="Score", competition_id=comp.id, include_dead_time_sum=False
        )
        ws = fake.spreadsheet.worksheet("Score")
        # The single update on A1 carries the whole grid.
        grid = ws.updates[-1]["values"]
        header = grid[0]
        # Expected column order: Group, Number, Name, Org, <CPs...>,
        # (no dead-time-sum since include_dead_time_sum=False), Časovnica, Found, Total
        assert header[-1] == "Skupaj točke" or header[-1].lower().startswith("skupaj") or "total" in header[-1].lower()
        assert any("Časovnica" in h or "casov" in h.lower() for h in header), header
        assert any("Najden" in h or "Found" in h or "found" in h.lower() for h in header), header

        # Team row should have a Časovnica formula that references both
        # Start and Cilj tabs and uses the *1440 minutes conversion.
        team_row = grid[1]
        cas_cell = team_row[-3]
        assert isinstance(cas_cell, str) and cas_cell.startswith("=")
        assert "'Start'!" in cas_cell
        assert "'Cilj'!" in cas_cell
        assert "1440" in cas_cell, f"Časovnica formula missing minute conversion: {cas_cell}"
        assert "195" in cas_cell, f"Časovnica threshold not embedded: {cas_cell}"

        # Found formula references CP-Mid (the one not excluded) but
        # NOT Start or Cilj (excluded via the flag).
        found_cell = team_row[-2]
        assert isinstance(found_cell, str) and found_cell.startswith("=100*")
        assert "'CP-Mid'!" in found_cell
        assert "'Start'!" not in found_cell
        assert "'Cilj'!" not in found_cell

        # Total formula sums per-CP lookups + casovnica + found.
        total_cell = team_row[-1]
        assert isinstance(total_cell, str) and total_cell.startswith("=SUM(")
