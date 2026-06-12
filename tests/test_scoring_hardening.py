"""Scoring-engine hardening regressions.

Pins four pre-launch fixes:
  - Rendering the scores views (including the unauthenticated public
    endpoint) must never persist an auto-DNF; the computed DNF only
    flows into the rendered row state.
  - The CSV export's rank column mirrors the on-screen per-group
    placement instead of a global running index.
  - time_race totals are canonical (base points + rank points) on both
    the submit path and the offline recompute path.
  - Negative dead_time submissions are rejected, and negative legacy
    dead_time rows are skipped in the displayed sum.
"""

from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta

from app.blueprints.scores.routes import _build_scores_context
from app.extensions import db
from app.models import CheckpointGroupLink, GlobalScoreRule, ScoreEntry, ScoreRule, Team
from app.resources.scores import recompute_scores_for_rule
from app.utils.time import utcnow_naive
from tests.support import (
    add_membership,
    assign_team_group,
    create_checkin,
    create_checkpoint,
    create_competition,
    create_group,
    create_team,
    create_user,
    login_as,
)

T0 = datetime(2026, 6, 20, 8, 0, 0)


def _link(group, checkpoint, position=0):
    db.session.add(CheckpointGroupLink(group_id=group.id, checkpoint_id=checkpoint.id, position=position))


def _add_entry(comp, team, checkpoint, *, total, raw_fields=None):
    db.session.add(
        ScoreEntry(
            competition_id=comp.id,
            team_id=team.id,
            checkpoint_id=checkpoint.id,
            raw_fields=raw_fields or {},
            total=total,
            created_at=utcnow_naive(),
        )
    )


def _seed_auto_dnf():
    """Competition where the global time rule's dq_multiplier puts the
    only team into auto-DNF territory (150 min > threshold 60 * 2)."""
    admin = create_user(username=None)
    comp = create_competition(public_results=True)
    add_membership(admin, comp, role="admin")
    group = create_group(comp)
    cp_start = create_checkpoint(comp)
    cp_end = create_checkpoint(comp)
    _link(group, cp_start, position=0)
    _link(group, cp_end, position=1)
    team = create_team(comp, number=1)
    assign_team_group(team, group)
    db.session.add(
        GlobalScoreRule(
            competition_id=comp.id,
            group_id=group.id,
            rules={
                "time": {
                    "start_checkpoint_id": cp_start.id,
                    "end_checkpoint_id": cp_end.id,
                    "max_points": 100,
                    "threshold_minutes": 60,
                    "penalty_minutes": 10,
                    "penalty_points": 5,
                    "min_points": 0,
                    "dq_multiplier": 2,
                }
            },
        )
    )
    db.session.commit()
    create_checkin(comp, team, cp_start, timestamp=T0)
    create_checkin(comp, team, cp_end, timestamp=T0 + timedelta(minutes=150))
    return {"admin": admin, "comp": comp, "team": team}


def test_public_scores_get_does_not_persist_auto_dnf(client, app):
    """The unauthenticated public endpoint must not write Team.dnf even
    when the timeline triggers an auto-DNF; the rendered row still shows
    the team as DNF."""
    s = _seed_auto_dnf()

    resp = client.get(f"/scores/public/{s['comp'].id}")
    assert resp.status_code == 200
    db.session.expire_all()
    assert not db.session.get(Team, s["team"].id).dnf

    # The computed auto-DNF still drives the rendered row state.
    ctx = _build_scores_context(s["comp"].id, None)
    row = next(r for r in ctx["rows"] if r["id"] == s["team"].id)
    assert row["dnf"] is True
    db.session.expire_all()
    assert not db.session.get(Team, s["team"].id).dnf


def test_admin_scores_view_does_not_persist_auto_dnf(client, app):
    s = _seed_auto_dnf()
    login_as(client, s["admin"], s["comp"])

    resp = client.get("/scores/view")
    assert resp.status_code == 200
    db.session.expire_all()
    assert not db.session.get(Team, s["team"].id).dnf


def test_csv_rank_restarts_per_group(client, app):
    admin = create_user(username="csv-admin")
    comp = create_competition(name="CSV Race")
    add_membership(admin, comp, role="admin")
    group_a = create_group(comp, name="Alpha")
    group_b = create_group(comp, name="Beta")
    cp = create_checkpoint(comp, name="CSV CP")
    _link(group_a, cp)
    _link(group_b, cp)
    for name, num, grp, total in [
        ("A1", 1, group_a, 10.0),
        ("A2", 2, group_a, 5.0),
        ("B1", 3, group_b, 8.0),
        ("B2", 4, group_b, 3.0),
    ]:
        team = create_team(comp, name=name, number=num)
        assign_team_group(team, grp)
        _add_entry(comp, team, cp, total=total)
    db.session.commit()

    login_as(client, admin, comp)
    resp = client.get("/scores/view/export.csv")
    assert resp.status_code == 200
    parsed = list(csv.reader(io.StringIO(resp.get_data(as_text=True))))
    header, data = parsed[0], parsed[1:]
    assert header[0] == "rank"
    # (group, rank, team) triples in on-screen order: rank restarts at 1
    # for every group, exactly like the web table's per-group place.
    assert [(r[1], r[0], r[3]) for r in data] == [
        ("Alpha", "1", "A1"),
        ("Alpha", "2", "A2"),
        ("Beta", "1", "B1"),
        ("Beta", "2", "B2"),
    ]


def test_time_race_submit_and_recompute_agree(client, app):
    """A checkpoint with both a base points field and a time_race rule
    must store the same canonical total (base + rank) from the submit
    path, the offline recompute, and the live scores view."""
    admin = create_user(username="tr-admin")
    comp = create_competition(name="TR Race")
    add_membership(admin, comp, role="admin")
    group = create_group(comp, name="TR Group")
    cp_start = create_checkpoint(comp, name="Leg Start")
    cp_end = create_checkpoint(comp, name="Leg End")
    cp_score = create_checkpoint(comp, name="Leg Scoring")
    for pos, cp in enumerate([cp_start, cp_end, cp_score]):
        _link(group, cp, position=pos)
    team_fast = create_team(comp, name="Fast", number=1)
    team_slow = create_team(comp, name="Slow", number=2)
    assign_team_group(team_fast, group)
    assign_team_group(team_slow, group)
    db.session.add(
        ScoreRule(
            competition_id=comp.id,
            checkpoint_id=cp_score.id,
            group_id=group.id,
            rules={
                "field_rules": {"task": {"type": "multiplier", "factor": 1}},
                "total_fields": ["task"],
                "time_race": {
                    "start_checkpoint_id": cp_start.id,
                    "end_checkpoint_id": cp_end.id,
                    "min_points": 0,
                    "max_points": 50,
                },
            },
        )
    )
    db.session.commit()

    create_checkin(comp, team_fast, cp_start, timestamp=T0)
    create_checkin(comp, team_fast, cp_end, timestamp=T0 + timedelta(minutes=10))
    create_checkin(comp, team_slow, cp_start, timestamp=T0)
    create_checkin(comp, team_slow, cp_end, timestamp=T0 + timedelta(minutes=20))

    login_as(client, admin, comp)
    resp = client.post(
        "/api/scores/submit",
        json={"team_id": team_fast.id, "checkpoint_id": cp_score.id, "fields": {"task": 30}},
    )
    assert resp.status_code == 201
    resp = client.post(
        "/api/scores/submit",
        json={"team_id": team_slow.id, "checkpoint_id": cp_score.id, "fields": {"task": 10}},
    )
    assert resp.status_code == 201

    def latest_totals():
        totals = {}
        entries = (
            ScoreEntry.query.filter(
                ScoreEntry.competition_id == comp.id,
                ScoreEntry.checkpoint_id == cp_score.id,
            )
            .order_by(ScoreEntry.created_at.desc())
            .all()
        )
        for entry in entries:
            totals.setdefault(entry.team_id, entry.total)
        return totals

    db.session.expire_all()
    after_submit = latest_totals()
    # Fast: base 30 + rank 50 (fastest). Slow: base 10 + rank 0 (slowest).
    assert after_submit[team_fast.id] == 80.0
    assert after_submit[team_slow.id] == 10.0

    recompute_scores_for_rule(comp.id, cp_score.id, group.id)
    db.session.expire_all()
    assert latest_totals() == after_submit

    # The live scores view shows the same canonical number.
    ctx = _build_scores_context(comp.id, None)
    assert ctx["per_team_points"][team_fast.id][cp_score.id] == 80.0
    assert ctx["per_team_points"][team_slow.id][cp_score.id] == 10.0


def test_negative_dead_time_rejected(client, app):
    admin = create_user(username="dt-admin")
    comp = create_competition(name="DT Race")
    add_membership(admin, comp, role="admin")
    cp = create_checkpoint(comp, name="DT CP")
    team = create_team(comp, name="DT Team", number=1)
    group = create_group(comp, name="DT Group")
    _link(group, cp)
    assign_team_group(team, group)
    db.session.commit()
    login_as(client, admin, comp)

    resp = client.post(
        "/api/scores/submit",
        json={"team_id": team.id, "checkpoint_id": cp.id, "fields": {"dead_time": -5}},
    )
    assert resp.status_code == 400
    assert "negative" in (resp.get_json().get("detail") or "").lower()

    # Positive control: a non-negative dead_time still goes through.
    resp = client.post(
        "/api/scores/submit",
        json={"team_id": team.id, "checkpoint_id": cp.id, "fields": {"dead_time": 5}},
    )
    assert resp.status_code == 201


def test_negative_legacy_dead_time_skipped_in_display(client, app):
    """Legacy rows may still hold negative dead_time; the displayed sum
    skips them to match _get_team_dead_time_total's positive-only filter."""
    admin = create_user(username="legacy-dt-admin")
    comp = create_competition(name="Legacy DT Race")
    add_membership(admin, comp, role="admin")
    group = create_group(comp, name="Legacy DT Group")
    cp = create_checkpoint(comp, name="Legacy DT CP")
    _link(group, cp)
    team = create_team(comp, name="Legacy DT Team", number=1)
    assign_team_group(team, group)
    _add_entry(comp, team, cp, total=5.0, raw_fields={"dead_time": -10, "task": 5})
    db.session.commit()

    ctx = _build_scores_context(comp.id, None)
    row = next(r for r in ctx["rows"] if r["id"] == team.id)
    assert row["dead_time"] == 0.0
