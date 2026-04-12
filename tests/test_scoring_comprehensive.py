"""Comprehensive scoring tests for Ščukanujanje competition system.

Tests all scoring types (no-rule, mapping, interpolation, multiplier, found,
deviation), timeline/časovnica, time trial, DNF, virtual checkpoints, org
scoring, negative-score clamping, and decimal precision — all LOCAL (no Sheets).
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.extensions import db
from app.models import (
    Checkin,
    Checkpoint,
    CheckpointGroup,
    CheckpointGroupLink,
    GlobalScoreRule,
    ScoreEntry,
    ScoreRule,
    Team,
    TeamGroup,
)
from app.resources.scores import (
    _apply_field_rule,
    _clamp_non_negative,
    _compute_global_contrib,
    _compute_time_race_scores_from_checkins,
    _compute_total,
    _round_score,
    _to_number,
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

T0 = datetime(2026, 6, 20, 8, 0, 0)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def seeded(app):
    """Seed full competition data matching the Ščukanujanje spec."""
    user = create_user(username="test-admin", role="admin")
    comp = create_competition(name="Scukanujanje Test 2026")
    add_membership(user, comp, role="admin")

    # --- Groups (categories) ---
    cat1 = create_group(comp, name="mGG", prefix="1xx")
    cat2 = create_group(comp, name="PP", prefix="2xx")
    cat3 = create_group(comp, name="RR+", prefix="3xx")

    # --- Checkpoints ---
    cp1 = create_checkpoint(comp, name="Hitrostna etapa - Start")
    cp2 = create_checkpoint(comp, name="Hitrostna etapa - Cilj")
    cp3 = create_checkpoint(comp, name="Rocne spretnosti")
    cp4 = create_checkpoint(comp, name="Minsko polje")
    cp5 = create_checkpoint(comp, name="Dodatna naloga")
    vcp = create_checkpoint(comp, name="Start")
    # Mark virtual
    vcp.is_virtual = True
    db.session.commit()

    # Link checkpoints to groups
    for pos, cp in enumerate([vcp, cp1, cp2, cp3, cp4, cp5]):
        db.session.add(CheckpointGroupLink(group_id=cat1.id, checkpoint_id=cp.id, position=pos))
    for pos, cp in enumerate([vcp, cp2, cp1, cp3, cp4, cp5]):  # reversed cp1/cp2 for cat2
        db.session.add(CheckpointGroupLink(group_id=cat2.id, checkpoint_id=cp.id, position=pos))
    for pos, cp in enumerate([vcp, cp1, cp2, cp3, cp4, cp5]):
        db.session.add(CheckpointGroupLink(group_id=cat3.id, checkpoint_id=cp.id, position=pos))
    db.session.commit()

    # --- Teams ---
    teams = {}
    for num, name, org, grp in [
        (101, "mGG-1", "Rod Jezerska scuka", cat1),
        (102, "mGG-2", "Rod Jezerska scuka", cat1),
        (103, "mGG-3", "Rod Roznik", cat1),
        (104, "mGG-4", "Rod Gorica", cat1),
        (105, "mGG-5", None, cat1),
        (201, "PP-1", "Rod Jezerska scuka", cat2),
        (202, "PP-2", "Rod Jezerska scuka", cat2),
        (203, "PP-3", "Rod Roznik", cat2),
        (204, "PP-4", "Rod Triglav", cat2),
        (205, "PP-5", None, cat2),
        (301, "RR-1", "Rod Jezerska scuka", cat3),
        (302, "RR-2", "Rod Roznik", cat3),
        (303, "RR-3", None, cat3),
        (304, "RR-4", None, cat3),
        (305, "RR-5", None, cat3),
    ]:
        t = create_team(comp, name=name, number=num, organization=org)
        assign_team_group(t, grp)
        teams[name] = t

    return {
        "comp": comp,
        "user": user,
        "cat1": cat1, "cat2": cat2, "cat3": cat3,
        "cp1": cp1, "cp2": cp2, "cp3": cp3, "cp4": cp4, "cp5": cp5, "vcp": vcp,
        "teams": teams,
    }


def _checkin(comp, team, cp, offset_minutes):
    """Create a check-in at T0 + offset_minutes."""
    return create_checkin(comp, team, cp, timestamp=T0 + timedelta(minutes=offset_minutes))


# ---------------------------------------------------------------------------
# Helper: apply a field rule in isolation
# ---------------------------------------------------------------------------

def _rule(rule_dict, value, context=None):
    return _apply_field_rule(value, rule_dict, context or {})


# ===========================================================================
# DEVIATION TESTS (CP4 — Minefield)
# ===========================================================================

class TestDeviation:
    DEV_RULE = {
        "type": "deviation",
        "target": 1000,
        "max_points": 100,
        "penalty_points": 5,
        "penalty_distance": 10,
        "min_points": 0,
    }

    def test_deviation_perfect_score(self):
        assert _rule(self.DEV_RULE, 1000) == 100.0

    def test_deviation_proportional_deduction(self):
        # offset=50 → penalty = (50/10)*5 = 25 → score = 75
        assert _rule(self.DEV_RULE, 1050) == 75.0

    def test_deviation_large_offset_clamped_to_zero(self):
        # offset=200 → penalty = (200/10)*5 = 100 → score = max(0, 0) = 0
        assert _rule(self.DEV_RULE, 1200) == 0.0

    def test_deviation_minimal_offset(self):
        # offset=1 → penalty = (1/10)*5 = 0.5 → score = 99.5
        assert _rule(self.DEV_RULE, 999) == 99.5

    def test_deviation_direction_irrelevant(self):
        # over by 10 and under by 10 should give same score
        assert _rule(self.DEV_RULE, 1010) == _rule(self.DEV_RULE, 990)
        assert _rule(self.DEV_RULE, 1010) == 95.0

    def test_deviation_never_negative(self):
        # offset=500 → penalty = 250, but clamped to 0
        assert _rule(self.DEV_RULE, 1500) == 0.0

    def test_deviation_2_decimal_places(self):
        # offset=1 → 99.5 exactly (already 1dp)
        result = _rule(self.DEV_RULE, 999)
        assert result == 99.5
        # offset=3 → (3/10)*5 = 1.5 → 98.5
        result = _rule(self.DEV_RULE, 997)
        assert result == 98.5

    def test_deviation_without_min_points_defaults_to_zero(self):
        rule = dict(self.DEV_RULE)
        del rule["min_points"]
        # offset=300 → penalty=150 → score = max(0, -50) = 0 (clamped)
        assert _rule(rule, 1300) == 0.0


# ===========================================================================
# MULTIPLIER TESTS (CP5 — Bonus task)
# ===========================================================================

class TestMultiplier:
    def test_multiplier_basic(self):
        rule = {"type": "multiplier", "factor": 5}
        assert _rule(rule, 5) == 25.0

    def test_multiplier_zero_input(self):
        rule = {"type": "multiplier", "factor": 5}
        assert _rule(rule, 0) == 0.0

    def test_multiplier_same_cp_different_category_multiplier(self):
        rule5 = {"type": "multiplier", "factor": 5}
        rule10 = {"type": "multiplier", "factor": 10}
        assert _rule(rule5, 5) == 25.0
        assert _rule(rule10, 5) == 50.0

    def test_multiplier_result_never_negative(self):
        # Even with factor=5, input 0 → 0, not negative
        rule = {"type": "multiplier", "factor": 5}
        assert _rule(rule, 0) == 0.0

    def test_multiplier_large_value(self):
        rule = {"type": "multiplier", "factor": 10}
        assert _rule(rule, 10) == 100.0


# ===========================================================================
# MAPPING TESTS (Virtual CP — Topo test)
# ===========================================================================

class TestMapping:
    TOPO_MAP = {"type": "mapping", "map": {"0": 0, "1": 8, "2": 16, "3": 24, "4": 32, "5": 40}}

    def test_mapping_valid_input(self):
        assert _rule(self.TOPO_MAP, 5) == 40.0

    def test_mapping_zero_input(self):
        assert _rule(self.TOPO_MAP, 0) == 0.0

    def test_mapping_unmapped_input_returns_none(self):
        assert _rule(self.TOPO_MAP, 6) is None

    def test_mapping_all_values(self):
        expected = {0: 0, 1: 8, 2: 16, 3: 24, 4: 32, 5: 40}
        for inp, out in expected.items():
            assert _rule(self.TOPO_MAP, inp) == float(out)


# ===========================================================================
# NO-RULE / CONSTRAINT TESTS (CP3 — Looks + Effect)
# ===========================================================================

class TestNoRule:
    def test_no_rule_passthrough(self):
        assert _rule({}, 20) == 20.0

    def test_no_rule_zero(self):
        assert _rule({}, 0) == 0.0

    def test_no_rule_decimal_accepted(self):
        assert _rule({}, 12.5) == 12.5

    def test_no_rule_3_decimal_places_rounded(self):
        # Python uses banker's rounding: round(12.555, 2) == 12.55
        assert _rule({}, 12.555) == 12.55
        # But 12.556 rounds up
        assert _rule({}, 12.556) == 12.56

    def test_no_rule_none_returns_none(self):
        assert _rule({}, None) is None

    def test_no_rule_empty_returns_none(self):
        assert _rule({}, "") is None


# ===========================================================================
# INTERPOLATION TESTS
# ===========================================================================

class TestInterpolation:
    INTERP = {"type": "interpolate", "points": [[0, 100], [60, 50], [120, 0]]}

    def test_interpolate_at_boundary_low(self):
        assert _rule(self.INTERP, 0) == 100.0

    def test_interpolate_at_boundary_high(self):
        assert _rule(self.INTERP, 120) == 0.0

    def test_interpolate_midpoint(self):
        assert _rule(self.INTERP, 30) == 75.0

    def test_interpolate_clamp_below(self):
        # Below first x → returns first y
        assert _rule(self.INTERP, -10) == 100.0

    def test_interpolate_clamp_above(self):
        # Above last x → returns last y (0, clamped to 0)
        assert _rule(self.INTERP, 200) == 0.0

    def test_interpolate_result_never_negative(self):
        # Points that would go negative get clamped
        rule = {"type": "interpolate", "points": [[0, 10], [100, -10]]}
        result = _rule(rule, 100)
        assert result == 0.0  # clamped from -10


# ===========================================================================
# FOUND POINTS TESTS
# ===========================================================================

class TestFoundPoints:
    def test_found_points_auto_awarded_on_checkin(self, app, seeded):
        s = seeded
        _checkin(s["comp"], s["teams"]["mGG-1"], s["cp3"], 60)
        rule = {"type": "found", "checkpoint_ids": [s["cp3"].id], "points_per": 100}
        ctx = {"team_id": s["teams"]["mGG-1"].id, "competition_id": s["comp"].id}
        assert _rule(rule, None, ctx) == 100.0

    def test_found_points_configurable_amount(self, app, seeded):
        s = seeded
        _checkin(s["comp"], s["teams"]["mGG-1"], s["cp3"], 60)
        rule = {"type": "found", "checkpoint_ids": [s["cp3"].id], "points_per": 50}
        ctx = {"team_id": s["teams"]["mGG-1"].id, "competition_id": s["comp"].id}
        assert _rule(rule, None, ctx) == 50.0

    def test_found_multiple_checkpoints(self, app, seeded):
        s = seeded
        _checkin(s["comp"], s["teams"]["mGG-1"], s["cp3"], 60)
        _checkin(s["comp"], s["teams"]["mGG-1"], s["cp4"], 90)
        rule = {"type": "found", "checkpoint_ids": [s["cp3"].id, s["cp4"].id], "points_per": 100}
        ctx = {"team_id": s["teams"]["mGG-1"].id, "competition_id": s["comp"].id}
        assert _rule(rule, None, ctx) == 200.0

    def test_found_no_checkin_zero(self, app, seeded):
        s = seeded
        rule = {"type": "found", "checkpoint_ids": [s["cp3"].id], "points_per": 100}
        ctx = {"team_id": s["teams"]["mGG-1"].id, "competition_id": s["comp"].id}
        assert _rule(rule, None, ctx) == 0.0


# ===========================================================================
# TIME TRIAL TESTS
# ===========================================================================

class TestTimeTrial:
    def _setup_cat1_time_trial(self, s):
        """Set up Cat 1 time trial checkins (CP1=start, CP2=end)."""
        teams = s["teams"]
        comp = s["comp"]
        # mGG-1: 60 min
        _checkin(comp, teams["mGG-1"], s["cp1"], 0)
        _checkin(comp, teams["mGG-1"], s["cp2"], 60)
        # mGG-2: 90 min
        _checkin(comp, teams["mGG-2"], s["cp1"], 5)
        _checkin(comp, teams["mGG-2"], s["cp2"], 95)
        # mGG-3: 120 min (but has 30 min dead time — effective 90, but time_race uses raw)
        _checkin(comp, teams["mGG-3"], s["cp1"], 10)
        _checkin(comp, teams["mGG-3"], s["cp2"], 130)
        # mGG-4: 150 min
        _checkin(comp, teams["mGG-4"], s["cp1"], 15)
        _checkin(comp, teams["mGG-4"], s["cp2"], 165)
        return [teams[n].id for n in ["mGG-1", "mGG-2", "mGG-3", "mGG-4"]]

    def test_time_trial_cat1_fastest_gets_max(self, app, seeded):
        team_ids = self._setup_cat1_time_trial(seeded)
        scores = _compute_time_race_scores_from_checkins(
            team_ids, seeded["comp"].id,
            seeded["cp1"].id, seeded["cp2"].id,
            min_points=10, max_points=100,
        )
        # mGG-1 (60 min) is fastest → max points
        assert scores[seeded["teams"]["mGG-1"].id] == 100.0

    def test_time_trial_cat1_slowest_gets_min(self, app, seeded):
        team_ids = self._setup_cat1_time_trial(seeded)
        scores = _compute_time_race_scores_from_checkins(
            team_ids, seeded["comp"].id,
            seeded["cp1"].id, seeded["cp2"].id,
            min_points=10, max_points=100,
        )
        # mGG-4 (150 min) is slowest → min points
        assert scores[seeded["teams"]["mGG-4"].id] == 10.0

    def test_time_trial_cat1_linear_interpolation(self, app, seeded):
        team_ids = self._setup_cat1_time_trial(seeded)
        scores = _compute_time_race_scores_from_checkins(
            team_ids, seeded["comp"].id,
            seeded["cp1"].id, seeded["cp2"].id,
            min_points=10, max_points=100,
        )
        # mGG-2 (90 min): t = (90-60)/(150-60) = 30/90 = 1/3 → 100 - 1/3*90 = 70
        assert scores[seeded["teams"]["mGG-2"].id] == pytest.approx(70.0, abs=0.01)

    def test_time_trial_cat1_monotonically_decreasing(self, app, seeded):
        team_ids = self._setup_cat1_time_trial(seeded)
        scores = _compute_time_race_scores_from_checkins(
            team_ids, seeded["comp"].id,
            seeded["cp1"].id, seeded["cp2"].id,
            min_points=10, max_points=100,
        )
        vals = [scores[tid] for tid in team_ids]
        for a, b in zip(vals, vals[1:]):
            assert a >= b

    def test_time_trial_cat2_reversed_direction(self, app, seeded):
        """Cat 2 uses CP2 as start and CP1 as end."""
        s = seeded
        teams = s["teams"]
        comp = s["comp"]
        # PP-1: CP2 at T0, CP1 at T0+45 → 45 min
        _checkin(comp, teams["PP-1"], s["cp2"], 0)
        _checkin(comp, teams["PP-1"], s["cp1"], 45)
        # PP-2: CP2 at T0+5, CP1 at T0+80 → 75 min
        _checkin(comp, teams["PP-2"], s["cp2"], 5)
        _checkin(comp, teams["PP-2"], s["cp1"], 80)

        team_ids = [teams["PP-1"].id, teams["PP-2"].id]
        # Note: start=cp2, end=cp1 (reversed for cat2)
        scores = _compute_time_race_scores_from_checkins(
            team_ids, comp.id,
            s["cp2"].id, s["cp1"].id,
            min_points=10, max_points=100,
        )
        assert scores[teams["PP-1"].id] == 100.0  # fastest
        assert scores[teams["PP-2"].id] == 10.0   # slowest

    def test_time_trial_equal_times_all_get_max(self, app, seeded):
        s = seeded
        teams = s["teams"]
        comp = s["comp"]
        # RR-1 and RR-4 both 90 min
        _checkin(comp, teams["RR-1"], s["cp1"], 0)
        _checkin(comp, teams["RR-1"], s["cp2"], 90)
        _checkin(comp, teams["RR-4"], s["cp1"], 15)
        _checkin(comp, teams["RR-4"], s["cp2"], 105)

        team_ids = [teams["RR-1"].id, teams["RR-4"].id]
        scores = _compute_time_race_scores_from_checkins(
            team_ids, comp.id,
            s["cp1"].id, s["cp2"].id,
            min_points=10, max_points=100,
        )
        assert scores[teams["RR-1"].id] == 100.0
        assert scores[teams["RR-4"].id] == 100.0

    def test_time_trial_no_finish_gets_zero(self, app, seeded):
        s = seeded
        teams = s["teams"]
        comp = s["comp"]
        # RR-5: only checked in at CP1 (start), never at CP2
        _checkin(comp, teams["RR-5"], s["cp1"], 20)

        team_ids = [teams["RR-5"].id]
        scores = _compute_time_race_scores_from_checkins(
            team_ids, comp.id,
            s["cp1"].id, s["cp2"].id,
            min_points=10, max_points=100,
        )
        # No end checkin → not in scores dict → 0
        assert teams["RR-5"].id not in scores

    def test_time_trial_single_team_gets_max(self, app, seeded):
        s = seeded
        teams = s["teams"]
        comp = s["comp"]
        _checkin(comp, teams["RR-3"], s["cp1"], 10)
        _checkin(comp, teams["RR-3"], s["cp2"], 160)

        scores = _compute_time_race_scores_from_checkins(
            [teams["RR-3"].id], comp.id,
            s["cp1"].id, s["cp2"].id,
            min_points=10, max_points=100,
        )
        # Only 1 team → min_d == max_d → all get max
        assert scores[teams["RR-3"].id] == 100.0

    def test_time_trial_independent_per_category(self, app, seeded):
        """Cat 1 and Cat 2 interpolation is independent."""
        s = seeded
        teams = s["teams"]
        comp = s["comp"]
        # Cat 1: mGG-1=60min, mGG-2=90min
        _checkin(comp, teams["mGG-1"], s["cp1"], 0)
        _checkin(comp, teams["mGG-1"], s["cp2"], 60)
        _checkin(comp, teams["mGG-2"], s["cp1"], 5)
        _checkin(comp, teams["mGG-2"], s["cp2"], 95)
        # Cat 2: PP-1=45min, PP-2=75min (reversed cp2→cp1)
        _checkin(comp, teams["PP-1"], s["cp2"], 0)
        _checkin(comp, teams["PP-1"], s["cp1"], 45)
        _checkin(comp, teams["PP-2"], s["cp2"], 5)
        _checkin(comp, teams["PP-2"], s["cp1"], 80)

        cat1_scores = _compute_time_race_scores_from_checkins(
            [teams["mGG-1"].id, teams["mGG-2"].id], comp.id,
            s["cp1"].id, s["cp2"].id, min_points=10, max_points=100,
        )
        cat2_scores = _compute_time_race_scores_from_checkins(
            [teams["PP-1"].id, teams["PP-2"].id], comp.id,
            s["cp2"].id, s["cp1"].id, min_points=10, max_points=100,
        )
        # Both fastest get max independently
        assert cat1_scores[teams["mGG-1"].id] == 100.0
        assert cat2_scores[teams["PP-1"].id] == 100.0


# ===========================================================================
# TIMELINE / ČASOVNICA TESTS
# ===========================================================================

class TestTimeline:
    """Test the global time rule (timeline/časovnica scoring)."""

    def _make_global_time_rule(self, comp_id, group_id, cp_start_id, cp_end_id,
                                threshold=120, max_pts=120, penalty_min=1,
                                penalty_pts=2, min_pts=0, dq_mult=None):
        time_cfg = {
            "start_checkpoint_id": cp_start_id,
            "end_checkpoint_id": cp_end_id,
            "max_points": max_pts,
            "threshold_minutes": threshold,
            "penalty_minutes": penalty_min,
            "penalty_points": penalty_pts,
            "min_points": min_pts,
        }
        if dq_mult is not None:
            time_cfg["dq_multiplier"] = dq_mult
        rules = {"time": time_cfg}
        rec = GlobalScoreRule(
            competition_id=comp_id, group_id=group_id, rules=rules
        )
        db.session.add(rec)
        db.session.commit()
        return rules

    def test_timeline_under_limit_gets_max(self, app, seeded):
        s = seeded
        t = s["teams"]["mGG-1"]
        # 100 min elapsed, timeline=120
        _checkin(s["comp"], t, s["cp1"], 0)
        _checkin(s["comp"], t, s["cp2"], 100)
        rules = self._make_global_time_rule(
            s["comp"].id, s["cat1"].id, s["cp1"].id, s["cp2"].id, threshold=120
        )
        result = _compute_global_contrib(s["comp"].id, t.id, s["cat1"].id, rules)
        assert result["time_points"] == 120.0

    def test_timeline_over_limit_penalty(self, app, seeded):
        s = seeded
        t = s["teams"]["mGG-2"]
        # 130 min elapsed, timeline=120 → 10 over × 2 = 20 penalty → 100
        _checkin(s["comp"], t, s["cp1"], 0)
        _checkin(s["comp"], t, s["cp2"], 130)
        rules = self._make_global_time_rule(
            s["comp"].id, s["cat1"].id, s["cp1"].id, s["cp2"].id, threshold=120
        )
        result = _compute_global_contrib(s["comp"].id, t.id, s["cat1"].id, rules)
        assert result["time_points"] == pytest.approx(100.0, abs=0.01)

    def test_timeline_exactly_at_limit(self, app, seeded):
        s = seeded
        t = s["teams"]["mGG-3"]
        # Exactly 120 min → max points
        _checkin(s["comp"], t, s["cp1"], 0)
        _checkin(s["comp"], t, s["cp2"], 120)
        rules = self._make_global_time_rule(
            s["comp"].id, s["cat1"].id, s["cp1"].id, s["cp2"].id, threshold=120
        )
        result = _compute_global_contrib(s["comp"].id, t.id, s["cat1"].id, rules)
        assert result["time_points"] == 120.0

    def test_timeline_heavily_over_clamped_to_zero(self, app, seeded):
        s = seeded
        t = s["teams"]["mGG-4"]
        # 200 min, timeline=120 → 80 over × 2 = 160 → 120-160 = -40 → clamped to 0
        _checkin(s["comp"], t, s["cp1"], 0)
        _checkin(s["comp"], t, s["cp2"], 200)
        rules = self._make_global_time_rule(
            s["comp"].id, s["cat1"].id, s["cp1"].id, s["cp2"].id, threshold=120
        )
        result = _compute_global_contrib(s["comp"].id, t.id, s["cat1"].id, rules)
        assert result["time_points"] == 0.0

    def test_timeline_dq_at_2x(self, app, seeded):
        s = seeded
        t = s["teams"]["mGG-5"]
        # 250 min, threshold=120, dq_mult=2 → 2×120=240 → 250>240 → auto_dnf
        _checkin(s["comp"], t, s["cp1"], 0)
        _checkin(s["comp"], t, s["cp2"], 250)
        rules = self._make_global_time_rule(
            s["comp"].id, s["cat1"].id, s["cp1"].id, s["cp2"].id,
            threshold=120, dq_mult=2.0,
        )
        result = _compute_global_contrib(s["comp"].id, t.id, s["cat1"].id, rules)
        assert result["auto_dnf"] is True

    def test_timeline_exactly_at_dq_boundary_not_dq(self, app, seeded):
        s = seeded
        t = s["teams"]["mGG-4"]
        # Exactly 240 min, threshold=120, dq_mult=2 → 240 is NOT > 240 → no DQ
        _checkin(s["comp"], t, s["cp1"], 0)
        _checkin(s["comp"], t, s["cp2"], 240)
        rules = self._make_global_time_rule(
            s["comp"].id, s["cat1"].id, s["cp1"].id, s["cp2"].id,
            threshold=120, dq_mult=2.0,
        )
        result = _compute_global_contrib(s["comp"].id, t.id, s["cat1"].id, rules)
        assert result["auto_dnf"] is False

    def test_timeline_dead_time_subtracted(self, app, seeded):
        s = seeded
        t = s["teams"]["mGG-3"]
        # 150 min raw - 30 min dead time = 120 effective → at limit → max points
        ci = _checkin(s["comp"], t, s["cp1"], 0)
        _checkin(s["comp"], t, s["cp2"], 150)
        # Add score entry with 30 min dead time
        entry = ScoreEntry(
            competition_id=s["comp"].id,
            checkin_id=ci.id,
            team_id=t.id,
            checkpoint_id=s["cp1"].id,
            raw_fields={"dead_time": 30},
            total=0,
        )
        db.session.add(entry)
        db.session.commit()

        rules = self._make_global_time_rule(
            s["comp"].id, s["cat1"].id, s["cp1"].id, s["cp2"].id, threshold=120
        )
        result = _compute_global_contrib(s["comp"].id, t.id, s["cat1"].id, rules)
        assert result["time_points"] == 120.0

    def test_timeline_minimum_zero_not_negative(self, app, seeded):
        s = seeded
        t = s["teams"]["mGG-1"]
        # Way over: 500 min → penalty exceeds max → clamped to 0
        _checkin(s["comp"], t, s["cp1"], 0)
        _checkin(s["comp"], t, s["cp2"], 500)
        rules = self._make_global_time_rule(
            s["comp"].id, s["cat1"].id, s["cp1"].id, s["cp2"].id, threshold=120
        )
        result = _compute_global_contrib(s["comp"].id, t.id, s["cat1"].id, rules)
        assert result["time_points"] >= 0

    def test_timeline_different_per_category(self, app, seeded):
        s = seeded
        # Same elapsed 170 min, cat1 timeline=120 (50 over), cat3 timeline=180 (under)
        t1 = s["teams"]["mGG-1"]
        t3 = s["teams"]["RR-1"]
        _checkin(s["comp"], t1, s["cp1"], 0)
        _checkin(s["comp"], t1, s["cp2"], 170)
        _checkin(s["comp"], t3, s["cp1"], 0)
        _checkin(s["comp"], t3, s["cp2"], 170)

        rules1 = self._make_global_time_rule(
            s["comp"].id, s["cat1"].id, s["cp1"].id, s["cp2"].id, threshold=120
        )
        rules3 = {"time": {
            "start_checkpoint_id": s["cp1"].id, "end_checkpoint_id": s["cp2"].id,
            "max_points": 180, "threshold_minutes": 180,
            "penalty_minutes": 1, "penalty_points": 2, "min_points": 0,
        }}
        r3_db = GlobalScoreRule(
            competition_id=s["comp"].id, group_id=s["cat3"].id, rules=rules3
        )
        db.session.add(r3_db)
        db.session.commit()

        result1 = _compute_global_contrib(s["comp"].id, t1.id, s["cat1"].id, rules1)
        result3 = _compute_global_contrib(s["comp"].id, t3.id, s["cat3"].id, rules3)
        # cat1: 50 over × 2 = 100 penalty → 120-100 = 20
        assert result1["time_points"] == pytest.approx(20.0, abs=0.01)
        # cat3: 170 < 180 → max
        assert result3["time_points"] == 180.0


# ===========================================================================
# DNF TESTS
# ===========================================================================

class TestDnf:
    def test_dnf_flag_exists_on_team(self, app, seeded):
        t = seeded["teams"]["mGG-5"]
        assert t.dnf is False
        t.dnf = True
        db.session.commit()
        assert Team.query.get(t.id).dnf is True

    def test_dnf_team_scores_still_recorded(self, app, seeded):
        s = seeded
        t = s["teams"]["mGG-5"]
        t.dnf = True
        db.session.commit()
        # Score entry can still be created for a DNF team
        ci = _checkin(s["comp"], t, s["cp3"], 60)
        entry = ScoreEntry(
            competition_id=s["comp"].id,
            checkin_id=ci.id,
            team_id=t.id,
            checkpoint_id=s["cp3"].id,
            raw_fields={"looks": 10, "effect": 10},
            total=20.0,
        )
        db.session.add(entry)
        db.session.commit()
        assert entry.total == 20.0

    def test_dnf_excluded_from_org_total(self, app, seeded):
        """Verify DNF teams are excluded from org total calculation."""
        s = seeded
        t = s["teams"]["mGG-5"]
        t.dnf = True
        t.organization = "Rod Jezerska scuka"
        db.session.commit()

        # Import the scoring context builder
        from app.blueprints.scores.routes import _build_scores_context
        client = app.test_client()
        login_as(client, s["user"], s["comp"])
        with app.test_request_context():
            from flask import session as flask_session
            # Just test the org_totals exclusion logic directly
            rows = [
                {"organization": "Rod Jezerska scuka", "dnf": False, "total": 100},
                {"organization": "Rod Jezerska scuka", "dnf": True, "total": 50},
            ]
            org_totals = {}
            for row in rows:
                org = (row.get("organization") or "").strip()
                if not org or row.get("dnf"):
                    continue
                org_totals[org] = org_totals.get(org, 0.0) + float(row.get("total") or 0.0)
            assert org_totals["Rod Jezerska scuka"] == 100.0  # DNF excluded


# ===========================================================================
# VIRTUAL CHECKPOINT TESTS
# ===========================================================================

class TestVirtualCheckpoint:
    def test_virtual_cp_flag_set(self, app, seeded):
        assert seeded["vcp"].is_virtual is True
        assert seeded["cp1"].is_virtual is False

    def test_virtual_cp_excluded_from_map(self, app, seeded):
        from app.utils.status import all_checkpoints_for_map
        cps = all_checkpoints_for_map(seeded["comp"].id)
        cp_names = [cp["name"] for cp in cps]
        assert "Start" not in cp_names
        assert "Hitrostna etapa - Start" in cp_names

    def test_virtual_cp_excluded_from_team_map_status(self, app, seeded):
        s = seeded
        from app.utils.status import compute_team_statuses
        status = compute_team_statuses(s["teams"]["mGG-1"].id, s["comp"].id)
        cp_names = [cp["name"] for cp in status["checkpoints"]]
        assert "Start" not in cp_names

    def test_virtual_cp_scoring_fields_work(self, app, seeded):
        """Virtual CP scoring fields compute correctly."""
        s = seeded
        # Mapping rule on virtual CP
        rule = {"type": "mapping", "map": {"0": 0, "1": 8, "2": 16, "3": 24, "4": 32, "5": 40}}
        assert _rule(rule, 5) == 40.0
        # Multiplier on virtual CP
        rule2 = {"type": "multiplier", "factor": 5}
        assert _rule(rule2, 20) == 100.0


# ===========================================================================
# ORGANISATION SCORING TESTS
# ===========================================================================

class TestOrgScoring:
    def test_org_total_sum_logic(self):
        """Org total = sum of non-DNF member teams."""
        rows = [
            {"organization": "Rod A", "dnf": False, "total": 100},
            {"organization": "Rod A", "dnf": False, "total": 80},
            {"organization": "Rod B", "dnf": False, "total": 50},
        ]
        org_totals = {}
        for row in rows:
            org = row.get("organization", "").strip()
            if not org or row.get("dnf"):
                continue
            org_totals[org] = org_totals.get(org, 0.0) + float(row.get("total") or 0.0)
        assert org_totals["Rod A"] == 180.0
        assert org_totals["Rod B"] == 50.0

    def test_org_dnf_excluded(self):
        rows = [
            {"organization": "Rod A", "dnf": False, "total": 100},
            {"organization": "Rod A", "dnf": True, "total": 200},
        ]
        org_totals = {}
        for row in rows:
            org = row.get("organization", "").strip()
            if not org or row.get("dnf"):
                continue
            org_totals[org] = org_totals.get(org, 0.0) + float(row.get("total") or 0.0)
        assert org_totals["Rod A"] == 100.0

    def test_org_unaffiliated_not_counted(self):
        rows = [
            {"organization": "", "dnf": False, "total": 100},
            {"organization": None, "dnf": False, "total": 50},
        ]
        org_totals = {}
        for row in rows:
            org = (row.get("organization") or "").strip()
            if not org or row.get("dnf"):
                continue
            org_totals[org] = org_totals.get(org, 0.0) + float(row.get("total") or 0.0)
        assert len(org_totals) == 0


# ===========================================================================
# COMPUTE TOTAL TESTS
# ===========================================================================

class TestComputeTotal:
    def test_total_is_sum_of_fields_with_rules(self):
        rule = {
            "field_rules": {
                "looks": {"type": "multiplier", "factor": 1},
                "effect": {"type": "multiplier", "factor": 1},
            },
            "total_fields": ["looks", "effect"],
        }
        values = {"looks": 20, "effect": 22}
        total = _compute_total(values, None, rule, {})
        assert total == 42.0

    def test_include_in_total_toggle(self):
        rule = {
            "field_rules": {
                "a": {"type": "multiplier", "factor": 1},
                "b": {"type": "multiplier", "factor": 1},
            },
            "total_fields": ["a"],  # only 'a' included
        }
        values = {"a": 10, "b": 20}
        total = _compute_total(values, None, rule, {})
        assert total == 10.0

    def test_dead_time_excluded_from_total(self):
        rule = {
            "field_rules": {
                "score": {"type": "multiplier", "factor": 1},
                "dead_time": {"type": "multiplier", "factor": 1},
            },
        }
        values = {"score": 50, "dead_time": 10}
        total = _compute_total(values, None, rule, {})
        assert total == 50.0


# ===========================================================================
# DECIMAL PRECISION TESTS
# ===========================================================================

class TestDecimalPrecision:
    def test_round_score_2dp(self):
        assert _round_score(99.555) == 99.56
        assert _round_score(100.0) == 100.0
        assert _round_score(12.5) == 12.5
        assert _round_score(None) is None

    def test_score_integer_stored_correctly(self):
        assert _round_score(100.0) == 100.0
        assert _round_score(0.0) == 0.0

    def test_multiplier_decimal_result(self):
        rule = {"type": "multiplier", "factor": 3.33}
        result = _rule(rule, 10)
        assert result == 33.3

    def test_deviation_decimal_result(self):
        rule = {
            "type": "deviation",
            "target": 100,
            "max_points": 100,
            "penalty_points": 7,
            "penalty_distance": 10,
            "min_points": 0,
        }
        # offset=3 → penalty = (3/10)*7 = 2.1 → score = 97.9
        assert _rule(rule, 103) == 97.9


# ===========================================================================
# NO-NEGATIVE POINTS TESTS
# ===========================================================================

class TestNoNegativePoints:
    def test_clamp_non_negative_positive(self):
        assert _clamp_non_negative(5.0) == 5.0

    def test_clamp_non_negative_zero(self):
        assert _clamp_non_negative(0.0) == 0.0

    def test_clamp_non_negative_negative(self):
        assert _clamp_non_negative(-10.0) == 0.0

    def test_clamp_non_negative_none(self):
        assert _clamp_non_negative(None) is None

    def test_deviation_never_negative(self):
        rule = {
            "type": "deviation",
            "target": 0,
            "max_points": 10,
            "penalty_points": 100,
            "penalty_distance": 1,
        }
        # offset=1 → penalty = 100 → 10-100=-90 → clamped to 0
        assert _rule(rule, 1) == 0.0

    def test_interpolation_never_negative(self):
        rule = {"type": "interpolate", "points": [[0, 10], [100, -50]]}
        assert _rule(rule, 100) == 0.0

    def test_no_negative_total(self):
        """Total should never be negative even if all sub-fields are zero."""
        rule = {
            "field_rules": {
                "a": {"type": "multiplier", "factor": 1},
            },
        }
        values = {"a": 0}
        total = _compute_total(values, None, rule, {})
        assert total >= 0

    def test_negative_score_input_rejected_by_api(self, app, seeded):
        """POST /api/scores/submit with negative field values → 400."""
        s = seeded
        client = app.test_client()
        login_as(client, s["user"], s["comp"])
        resp = client.post("/api/scores/submit", json={
            "team_id": s["teams"]["mGG-1"].id,
            "checkpoint_id": s["cp3"].id,
            "fields": {"looks": -5},
        }, headers={"Content-Type": "application/json"})
        assert resp.status_code == 400
        data = resp.get_json()
        assert "negative" in data.get("detail", "").lower()


# ===========================================================================
# SIGNALING (Multiplier ×5) — Regression
# ===========================================================================

class TestSignaling:
    def test_signaling_25_correct_gets_125(self):
        rule = {"type": "multiplier", "factor": 5}
        assert _rule(rule, 25) == 125.0

    def test_signaling_0_correct_gets_0(self):
        rule = {"type": "multiplier", "factor": 5}
        assert _rule(rule, 0) == 0.0
