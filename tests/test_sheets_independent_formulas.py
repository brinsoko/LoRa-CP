"""Phase 2: the published spreadsheet must compute the final score on
its own from raw judge inputs + check-in timestamps, with no live
dependency on the LoRa-KT scoring engine.

Migrated to the phase-2 scoring model (tables instead of JSON blobs):

1. Per-CP `Points` cells are emitted as *formulas* on the publish path
   when the resolved ScoreField rules are expressible (multiplier,
   mapping, deviation, raw sum). _build_local_cp_grid builds a
   legacy-shaped rule blob from ScoreField rows (never time_race);
   per-group ScoreFieldGroup overrides and counts_in_total are honored.

2. `update_checkpoint_scores_sync` honors a per-group `points_formula`
   flag on SheetConfig.config and skips writing the Points cell when
   set, so live score submissions don't clobber the formula.

3. The Score summary tab emits Časovnica + Found-points columns from
   GroupScoring (race_* columns, STEPPED deduction via FLOOR; endpoints
   are the group's directed route start/finish) and Checkpoint.
   counts_for_found, plus four formula columns per TimedSegment
   (A arrival, B arrival, diff in minutes, rank-spread points).
"""

from __future__ import annotations

import pytest

from app.extensions import db
from app.models import ScoreEntry, SheetConfig
from app.utils import sheets_client as sheets_client_module
from app.utils import sheets_sync
from tests.support import (
    add_membership,
    assign_team_group,
    create_checkpoint,
    create_competition,
    create_group,
    create_score_field,
    create_segment,
    create_team,
    create_user,
    set_field_group,
    set_group_route,
    set_group_scoring,
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
    """interpolate is a real phase-2 ScoreField.rule_type but has no
    static-formula translation; the caller falls back to the
    system-written total. Legacy blobs with nested time_race bail too."""
    assert sheets_sync._field_rule_to_formula(
        {"type": "interpolate", "points": [[0, 0], [10, 100]]}, "D2"
    ) is None
    assert sheets_sync._field_rule_to_formula(
        {"type": "time_race", "start_checkpoint_id": 1}, "D2"
    ) is None


def test_points_formula_from_rule_sums_multiple_fields():
    """vesla = veslo_izgled + veslo_dimenzija (both raw 0-50). The blob
    shape is exactly what _build_local_cp_grid derives from ScoreField
    rows: field_rules per key, total_fields from counts_in_total."""
    rule = {
        "field_rules": {"veslo_izgled": {}, "veslo_dimenzija": {}},
        "total_fields": ["veslo_izgled", "veslo_dimenzija"],
    }
    formula = sheets_sync._points_formula_from_rule(
        rule, {"veslo_izgled": 3, "veslo_dimenzija": 4}, row=5
    )
    assert formula == "=C5+D5"


def test_points_formula_from_rule_time_race_returns_none():
    """Phase-2 blobs built from ScoreField rows never carry time_race
    (segments moved to TimedSegment + Score-tab columns), but the guard
    must survive for legacy-shaped blobs: a time_race rule cannot become
    a static Points formula."""
    rule = {
        "time_race": {"start_checkpoint_id": 1, "end_checkpoint_id": 2, "min_points": 10, "max_points": 100}
    }
    assert sheets_sync._points_formula_from_rule(rule, {}, row=5) is None


def test_points_formula_from_rule_drops_dead_time_from_total():
    """The engine's compute_entry_total excludes 'dead_time' from the
    total (dead_time now comes from Checkpoint.dead_time_enabled, not a
    ScoreField); the spreadsheet formula must do the same so dead time
    penalties are accounted for only via the Časovnica column."""
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


def _seed_competition_with_fields(field_specs):
    """Set up: one group, one CP with field columns, one team.

    `field_specs` is a list of (key, create_score_field kwargs); the
    SheetConfig's per-group `fields` list mirrors the keys, matching
    what wizard_create_checkpoint_configs derives via resolve_fields.
    Returns the seeded objects incl. the created ScoreFields by key.
    """
    user = create_user(username="phase2-admin", role="admin")
    comp = create_competition(name="Phase 2 Race")
    add_membership(user, comp, role="admin")
    grp = create_group(comp, name="Alpha", prefix="1xx")
    cp = create_checkpoint(comp, name="CP-One")
    team = create_team(comp, name="Team-A1", number=101)
    assign_team_group(team, grp)
    set_group_route(grp, [cp])
    fields = {key: create_score_field(cp, key, **kwargs) for key, kwargs in field_specs}
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
                {"group_id": grp.id, "name": "Alpha", "fields": [key for key, _ in field_specs]},
            ],
        },
    )
    db.session.add(cfg)
    db.session.commit()
    return {"comp": comp, "cp": cp, "grp": grp, "team": team, "cfg": cfg, "fields": fields}


def test_publish_emits_formula_in_points_cell_for_simple_rule(sheets_app, monkeypatch):
    """A multiplier ScoreField (x20) means Points cell = =(B2*20)+C2, not
    the raw ScoreEntry.total. So a manual edit to B2/C2 on the sheet
    propagates through Points without our system."""
    with sheets_app.app_context():
        s = _seed_competition_with_fields(
            [
                ("task1", {"rule_type": "multiplier", "rule_params": {"factor": 20}}),
                ("task2", {}),  # raw
            ]
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
        # Points cell is a formula, not the literal 143.0: task1*20 + task2
        # with B/C as raw field columns.
        assert team_row[3] == "=(B2*20)+C2"

        # SheetConfig has the points_formula flag set per group so
        # update_checkpoint_scores_sync knows to skip the Points cell.
        refreshed_cfg = SheetConfig.query.filter_by(
            competition_id=s["comp"].id, tab_name="CP-One"
        ).one()
        groups_blob = refreshed_cfg.config["groups"]
        assert groups_blob[0].get("points_formula") is True


def test_publish_formula_excludes_counts_in_total_false_field(sheets_app, monkeypatch):
    """counts_in_total=False (the ScoreRule.total_fields replacement)
    keeps a judged field out of the Points formula, matching
    compute_entry_total which skips it in the app."""
    with sheets_app.app_context():
        s = _seed_competition_with_fields(
            [
                ("task1", {}),
                ("task2", {"counts_in_total": False}),
            ]
        )
        fake = _install_fake_client(monkeypatch)
        result = sheets_sync.publish_local_configs_to_spreadsheet(
            competition_id=s["comp"].id, spreadsheet_id="REAL-SHEET"
        )
        assert result["published"] == 1, result

        ws = fake.spreadsheet.worksheet("CP-One")
        grid = [u for u in ws.updates if u["range_name"] == "A1"][-1]["values"]
        # Only task1 (B) contributes; task2 (C) is excluded from the total.
        assert grid[1][3] == "=B2"


def test_publish_formula_honors_group_rule_override(sheets_app, monkeypatch):
    """A ScoreFieldGroup.rule_override (per-group divergence, replacing
    the old per-group ScoreRule rows) drives the published formula."""
    with sheets_app.app_context():
        s = _seed_competition_with_fields(
            [("task1", {"rule_type": "multiplier", "rule_params": {"factor": 20}})]
        )
        set_field_group(
            s["fields"]["task1"],
            s["grp"],
            rule_override={"rule_type": "multiplier", "rule_params": {"factor": 30}},
        )
        fake = _install_fake_client(monkeypatch)
        result = sheets_sync.publish_local_configs_to_spreadsheet(
            competition_id=s["comp"].id, spreadsheet_id="REAL-SHEET"
        )
        assert result["published"] == 1, result

        ws = fake.spreadsheet.worksheet("CP-One")
        grid = [u for u in ws.updates if u["range_name"] == "A1"][-1]["values"]
        # The group's override factor (30) wins over the default (20).
        assert grid[1][2] == "=(B2*30)"


def test_publish_leaves_points_raw_when_rule_not_expressible(sheets_app, monkeypatch):
    """Behavior change note: time_race ScoreRules are gone — timed
    segments now live on the Score tab as formula columns. The CP-tab
    equivalent of "system-dependent Points" is a ScoreField whose
    rule_type has no formula translation (interpolate): the Points cell
    stays the system-written total and the points_formula flag must NOT
    be set so update_checkpoint_scores_sync keeps writing the cell."""
    with sheets_app.app_context():
        s = _seed_competition_with_fields(
            [
                ("task1", {"rule_type": "interpolate", "rule_params": {"points": [[0, 0], [10, 100]]}}),
                ("task2", {}),
            ]
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
    non-expressible CPs) keep getting Points written as before."""
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


# ---------------------------------------------------------------------------
# Score-tab formulas from GroupScoring / TimedSegment
# ---------------------------------------------------------------------------


def _lookup(tab: str, col: str, row_idx: int) -> str:
    """The INDEX/MATCH arrival-time lookup emitted for CP tabs whose
    layout is [group name, Time, Points] (team col A, time col `col`)."""
    return f"INDEX('{tab}'!{col}:{col}; MATCH(B{row_idx}; '{tab}'!A:A; 0))"


def _cp_sheet_config(comp, grp, cp) -> SheetConfig:
    """A per-CP SheetConfig with time_enabled so the Time column exists
    (otherwise the score-tab formulas short-circuit to =0/'=\"\"')."""
    return SheetConfig(
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


def test_score_tab_found_formula_excludes_non_counting_checkpoints(sheets_app, monkeypatch):
    """Virtual CPs (Topo&Vrisovanje, Lokostrelstvo) carry no physical
    arrival and start/finish never earn found points. In phase 2 the
    exclusion mechanism is Checkpoint.counts_for_found=False (which the
    setup UI / backfill applies to virtual CPs and route endpoints,
    replacing the exclude_start/end flags), and the sheet's Found
    formula must honor it exactly like compute_group_contrib does."""
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
        # counts_for_found=False is how virtual CPs and route endpoints
        # are kept out of found points now (default is True).
        for cp in (start, cilj, topo, loko):
            cp.counts_for_found = False
        team = create_team(comp, name="T1", number=101)
        assign_team_group(team, grp)
        set_group_route(grp, [start, topo, mid, loko, cilj])
        for cp in (start, topo, mid, loko, cilj):
            db.session.add(_cp_sheet_config(comp, grp, cp))
        # Category rules live on GroupScoring now (race_* columns +
        # found_points_per) instead of a GlobalScoreRule JSON blob.
        set_group_scoring(
            grp,
            found_points_per=100,
            race_max_points=195,
            race_threshold_minutes=195,
            race_penalty_minutes=1,
            race_penalty_points=2,
            race_min_points=0,
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
        # Only CP-Mid counts: exact one-term formula.
        assert found_cell == (
            f'=100*(IFERROR(IF({_lookup("CP-Mid", "B", 2)}<>""; 1; 0); 0))'
        ), found_cell
        assert "'Topo&Vrisovanje'!" not in found_cell, (
            f"Topo (virtual) leaked into Found formula: {found_cell}"
        )
        assert "'Lokostrelstvo'!" not in found_cell, (
            f"Lokostrelstvo (virtual) leaked into Found formula: {found_cell}"
        )
        # Start and Cilj also stay out (counts_for_found=False).
        assert "'Start'!" not in found_cell
        assert "'Cilj'!" not in found_cell


def test_score_tab_has_casovnica_and_found_columns(sheets_app, monkeypatch):
    """build_score_tab emits Časovnica + Found-points columns whose
    formulas reach into the per-CP tabs' Time columns. Časovnica comes
    from GroupScoring race_* columns: endpoints are the group's directed
    route start/finish and the deduction is STEPPED — FLOOR(minutes over
    threshold / penalty_minutes) * penalty_points (behavior change from
    the old proportional GlobalScoreRule formula)."""
    with sheets_app.app_context():
        user = create_user(username="score-tab-admin", role="admin")
        comp = create_competition(name="Score Tab Race")
        add_membership(user, comp, role="admin")
        grp = create_group(comp, name="Alpha", prefix="1xx")
        start = create_checkpoint(comp, name="Start")
        cilj = create_checkpoint(comp, name="Cilj")
        mid = create_checkpoint(comp, name="CP-Mid")
        # Endpoints don't earn found points; CP-Mid does.
        start.counts_for_found = False
        cilj.counts_for_found = False
        team = create_team(comp, name="T1", number=101)
        assign_team_group(team, grp)
        set_group_route(grp, [start, mid, cilj])
        for cp in (start, mid, cilj):
            db.session.add(_cp_sheet_config(comp, grp, cp))
        # The race numbers (195 min threshold, -2 per full minute over,
        # found=100) now live on GroupScoring; the route start/finish
        # replace the old configurable start/end_checkpoint_id.
        set_group_scoring(
            grp,
            found_points_per=100,
            race_max_points=195,
            race_threshold_minutes=195,
            race_penalty_minutes=1,
            race_penalty_points=2,
            race_min_points=0,
        )
        db.session.commit()
        fake = _install_fake_client(monkeypatch)
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

        # Časovnica: directed endpoints Start -> Cilj, minutes via *1440,
        # dead time (0 here) subtracted before the threshold comparison,
        # stepped deduction via FLOOR, floored at min_points.
        team_row = grid[1]
        start_lk = _lookup("Start", "B", 2)
        end_lk = _lookup("Cilj", "B", 2)
        cas_cell = team_row[-3]
        assert cas_cell == (
            f'=IFERROR(IF({end_lk}=""; 0; '
            f"MAX(195-FLOOR(MAX(0; ({end_lk}-{start_lk})*1440-(0)-195)/1)*2; 0)); 0)"
        ), cas_cell

        # Found formula references CP-Mid (counts_for_found) but NOT
        # Start or Cilj (counts_for_found=False).
        found_cell = team_row[-2]
        assert found_cell == (
            f'=100*(IFERROR(IF({_lookup("CP-Mid", "B", 2)}<>""; 1; 0); 0))'
        ), found_cell
        assert "'Start'!" not in found_cell
        assert "'Cilj'!" not in found_cell

        # Total formula sums per-CP lookups + casovnica + found.
        total_cell = team_row[-1]
        assert isinstance(total_cell, str) and total_cell.startswith("=SUM(")
        assert "FLOOR" in total_cell  # časovnica joined the total


def test_score_tab_casovnica_uses_directed_route_endpoints(sheets_app, monkeypatch):
    """A reverse-direction group runs the same path backwards: the
    Časovnica endpoints must come from the DIRECTED route, so the last
    path stop ('Start' here after reversal... i.e. the stored first stop)
    becomes the finish lookup and the stored last stop the start."""
    with sheets_app.app_context():
        user = create_user(username="rev-admin", role="admin")
        comp = create_competition(name="Reverse Race")
        add_membership(user, comp, role="admin")
        grp = create_group(comp, name="Alpha", prefix="1xx")
        start = create_checkpoint(comp, name="Start")
        cilj = create_checkpoint(comp, name="Cilj")
        mid = create_checkpoint(comp, name="CP-Mid")
        team = create_team(comp, name="T1", number=101)
        assign_team_group(team, grp)
        # Stops stored forward as Start -> CP-Mid -> Cilj, but the group
        # traverses in reverse: directed route is Cilj -> CP-Mid -> Start.
        set_group_route(grp, [start, mid, cilj], direction="reverse")
        for cp in (start, mid, cilj):
            db.session.add(_cp_sheet_config(comp, grp, cp))
        set_group_scoring(
            grp,
            race_max_points=195,
            race_threshold_minutes=195,
            race_penalty_minutes=1,
            race_penalty_points=2,
            race_min_points=0,
        )
        db.session.commit()
        fake = _install_fake_client(monkeypatch)
        sheets_sync.build_score_tab(
            "REAL-SHEET", tab_name="Score", competition_id=comp.id, include_dead_time_sum=False
        )
        grid = fake.spreadsheet.worksheet("Score").updates[-1]["values"]
        team_row = grid[1]
        # Directed: race start = Cilj tab, race finish = Start tab.
        start_lk = _lookup("Cilj", "B", 2)
        end_lk = _lookup("Start", "B", 2)
        cas_cell = team_row[-3]
        assert cas_cell == (
            f'=IFERROR(IF({end_lk}=""; 0; '
            f"MAX(195-FLOOR(MAX(0; ({end_lk}-{start_lk})*1440-(0)-195)/1)*2; 0)); 0)"
        ), cas_cell


def test_score_tab_emits_segment_formula_columns(sheets_app, monkeypatch):
    """New in phase 2: each TimedSegment adds four Score-tab columns —
    A/B arrival lookups, diff = (B-A)*1440 minutes over the A/B cells,
    and rank-spread points via MIN/MAX over the group's diff range. The
    segment points cell joins the Total SUM (segments are no longer part
    of ScoreEntry totals)."""
    with sheets_app.app_context():
        user = create_user(username="seg-admin", role="admin")
        comp = create_competition(name="Segment Race")
        add_membership(user, comp, role="admin")
        grp = create_group(comp, name="Alpha", prefix="1xx")
        start = create_checkpoint(comp, name="Start")
        cilj = create_checkpoint(comp, name="Cilj")
        mid = create_checkpoint(comp, name="CP-Mid")
        t1 = create_team(comp, name="T1", number=101)
        t2 = create_team(comp, name="T2", number=102)
        assign_team_group(t1, grp)
        assign_team_group(t2, grp)
        path = set_group_route(grp, [start, mid, cilj])
        create_segment(path, start, mid, name="Etapa", max_points=100.0, min_points=10.0)
        for cp in (start, mid, cilj):
            db.session.add(_cp_sheet_config(comp, grp, cp))
        db.session.commit()
        fake = _install_fake_client(monkeypatch)

        sheets_sync.build_score_tab(
            "REAL-SHEET", tab_name="Score", competition_id=comp.id, include_dead_time_sum=False
        )
        grid = fake.spreadsheet.worksheet("Score").updates[-1]["values"]
        header = grid[0]
        # 4 base cols + 3 CP cols (route order Start, CP-Mid, Cilj), then
        # the segment block at columns H..K, then Časovnica/Found/Total.
        assert header[7:11] == ["Etapa A", "Etapa B", "Etapa čas (min)", "Etapa točke"], header

        row2, row3 = grid[1], grid[2]
        assert row2[1] == 101 and row3[1] == 102

        # A/B lookups pull the segment endpoints' Time cells.
        assert row2[7] == f'=IFERROR({_lookup("Start", "B", 2)}; "")'
        assert row2[8] == f'=IFERROR({_lookup("CP-Mid", "B", 2)}; "")'
        # diff recomputes from the A/B cells so a hand-patched arrival
        # flows through; blank endpoints yield a blank diff.
        assert row2[9] == '=IF(OR(H2=""; I2=""); ""; (I2-H2)*1440)'
        # points: rank spread over the group's diff range J2:J3 — fastest
        # gets max (100), slowest min (10), all-equal gets max.
        assert row2[10] == (
            "=IF(J2=\"\"; 0; IF(MAX(J2:J3)=MIN(J2:J3); 100; "
            "MAX(100-(J2-MIN(J2:J3))/(MAX(J2:J3)-MIN(J2:J3))*(100-(10)); 10)))"
        ), row2[10]
        # Second team: same shared range, own cells.
        assert row3[9] == '=IF(OR(H3=""; I3=""); ""; (I3-H3)*1440)'
        assert "J2:J3" in row3[10] and "J3" in row3[10]

        # The segment points cell (K2) joins the Total SUM; no GroupScoring
        # here so časovnica/found contribute literal zeros.
        total_cell = row2[-1]
        assert total_cell.startswith("=SUM(")
        assert ";K2;0;0)" in total_cell, total_cell
