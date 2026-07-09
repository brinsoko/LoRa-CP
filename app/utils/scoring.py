# app/utils/scoring.py
"""Scoring engine core: field resolution, timed segments, category rules.

One computation path shared by the judge API, the leaderboard build, the
CSV export and the sheets sync, so submit/recompute/render can never
disagree (redesign plan 3.2/3.3).
"""

from __future__ import annotations

import math
from datetime import datetime

from sqlalchemy import func

from app.extensions import db
from app.models import (
    Checkin,
    Checkpoint,
    CheckpointGroup,
    GroupScoring,
    ScoreEntry,
    ScoreField,
    ScoreFieldGroup,
    Team,
    TimedSegment,
)
from app.utils.paths import resolve_route_ids


def _to_number(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _round_score(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value, 2)


def _clamp_non_negative(value: float | None) -> float | None:
    if value is None:
        return None
    return max(0.0, value)


# ---------------------------------------------------------------------------
# Fields
# ---------------------------------------------------------------------------


def field_rule_dict(field: ScoreField, override: dict | None = None) -> dict | None:
    """Legacy-shaped rule dict ({'type': ..., params...}) for a field.

    _apply_field_rule and judge_labels.enrich_field_def grew around this
    shape; building it from the structured columns keeps them unchanged.
    A ScoreFieldGroup.rule_override replaces type/params/max_input.
    """
    rule_type = field.rule_type or "none"
    params = dict(field.rule_params or {})
    max_input = field.max_input
    if override:
        rule_type = override.get("rule_type", rule_type) or "none"
        if "rule_params" in override:
            params = dict(override.get("rule_params") or {})
        if "max_input" in override:
            max_input = override.get("max_input")
    if rule_type == "none" and not params and max_input is None and not field.label and not field.hint:
        return None
    rule: dict = dict(params)
    if rule_type and rule_type != "none":
        rule["type"] = rule_type
    if field.label:
        rule["label"] = field.label
    if field.hint:
        rule["hint"] = field.hint
    if max_input is not None:
        rule["max_input"] = max_input
    return rule or None


def resolve_fields(checkpoint_id: int, group_id: int | None) -> list[dict]:
    """Enabled fields for a checkpoint as seen by one group, in position
    order: [{key, label, hint, rule, counts_in_total}]. rule is the
    legacy-shaped dict or None."""
    fields = (
        ScoreField.query.filter(ScoreField.checkpoint_id == checkpoint_id)
        .order_by(ScoreField.position.asc(), ScoreField.id.asc())
        .all()
    )
    overrides: dict[int, ScoreFieldGroup] = {}
    if group_id is not None and fields:
        rows = ScoreFieldGroup.query.filter(
            ScoreFieldGroup.score_field_id.in_([f.id for f in fields]),
            ScoreFieldGroup.group_id == group_id,
        ).all()
        overrides = {row.score_field_id: row for row in rows}

    resolved = []
    for field in fields:
        row = overrides.get(field.id)
        if row is not None and not row.enabled:
            continue
        resolved.append(
            {
                "key": field.key,
                "label": field.label or field.key,
                "hint": field.hint,
                "rule": field_rule_dict(field, row.rule_override if row else None),
                "counts_in_total": bool(field.counts_in_total),
            }
        )
    return resolved


def compute_entry_total(values: dict, fields: list[dict], context: dict) -> float | None:
    """Total for one ScoreEntry.raw_fields dict against resolved fields.

    Precedence mirrors the old engine: configured fields win; an explicit
    'points' value overrides (the synthetic points-only flow); otherwise
    the raw sum of numeric inputs except time/dead_time.
    """
    from app.resources.scores import _apply_field_rule

    base_total = None
    scored = [f for f in fields if f["key"] not in ("dead_time",)]
    if scored:
        total = 0.0
        used = False
        for f in scored:
            if not f.get("counts_in_total", True):
                continue
            if f["key"] not in values:
                continue
            val = values.get(f["key"])
            computed = _apply_field_rule(val, f.get("rule"), context) if f.get("rule") else _to_number(val)
            if computed is None:
                continue
            total += float(computed)
            used = True
        base_total = total if used else None

    if "points" in values:
        base_total = _to_number(values.get("points"))
    if base_total is None:
        total = 0.0
        used = False
        for key, val in values.items():
            if key in ("time", "dead_time"):
                continue
            num = _to_number(val)
            if num is None:
                continue
            total += num
            used = True
        base_total = total if used else None

    return _round_score(base_total)


# ---------------------------------------------------------------------------
# Timed segments
# ---------------------------------------------------------------------------


def resolve_group_segments(group: CheckpointGroup | None) -> list[dict]:
    """Segments of the group's path with endpoints in traversal direction.

    Returns [{id, label, start_checkpoint_id, end_checkpoint_id,
    max_points, min_points}] in stable id order.
    """
    if not group or not group.path_id:
        return []
    segments = (
        TimedSegment.query.filter(TimedSegment.path_id == group.path_id)
        .order_by(TimedSegment.id.asc())
        .all()
    )
    name_by_id = {}
    if segments:
        cp_ids = {s.start_checkpoint_id for s in segments} | {s.end_checkpoint_id for s in segments}
        name_by_id = {
            cp.id: cp.name for cp in Checkpoint.query.filter(Checkpoint.id.in_(cp_ids)).all()
        }
    out = []
    for segment in segments:
        start_id, end_id = segment.start_checkpoint_id, segment.end_checkpoint_id
        if group.direction == "reverse":
            start_id, end_id = end_id, start_id
        label = segment.name or f"{name_by_id.get(start_id, '?')}→{name_by_id.get(end_id, '?')}"
        out.append(
            {
                "id": segment.id,
                "label": label,
                "start_checkpoint_id": start_id,
                "end_checkpoint_id": end_id,
                "max_points": float(segment.max_points or 0.0),
                "min_points": float(segment.min_points or 0.0),
            }
        )
    return out


def _first_checkin_times(comp_id: int, team_ids: list[int], checkpoint_id: int) -> dict[int, datetime]:
    rows = (
        db.session.query(Checkin.team_id, func.min(Checkin.timestamp))
        .filter(
            Checkin.competition_id == comp_id,
            Checkin.checkpoint_id == checkpoint_id,
            Checkin.team_id.in_(team_ids),
        )
        .group_by(Checkin.team_id)
        .all()
    )
    return {team_id: ts for team_id, ts in rows if ts}


def compute_segment_results(comp_id: int, team_ids: list[int], segment: dict) -> dict[int, dict]:
    """Per-team result for one directed segment.

    Rank spread within the given pool (the category): fastest gets
    max_points, slowest min_points, linear in between; everyone equal
    gets max_points. Teams with only one endpoint reached still get
    their partial timestamps so displays can show 'A 10:03; B -'.
    Segment times never subtract dead time (decisions log).
    """
    if not team_ids:
        return {}
    start_map = _first_checkin_times(comp_id, team_ids, segment["start_checkpoint_id"])
    end_map = _first_checkin_times(comp_id, team_ids, segment["end_checkpoint_id"])

    durations: dict[int, float] = {}
    results: dict[int, dict] = {}
    for team_id in team_ids:
        start_ts = start_map.get(team_id)
        end_ts = end_map.get(team_id)
        if not start_ts and not end_ts:
            continue
        minutes = None
        if start_ts and end_ts and end_ts >= start_ts:
            minutes = (end_ts - start_ts).total_seconds() / 60.0
            durations[team_id] = minutes
        results[team_id] = {
            "segment_id": segment["id"],
            "label": segment["label"],
            "start_at": start_ts,
            "end_at": end_ts,
            "minutes": minutes,
            "points": None,
        }

    if durations:
        min_d = min(durations.values())
        max_d = max(durations.values())
        max_points = segment["max_points"]
        min_points = segment["min_points"]
        for team_id, duration in durations.items():
            if max_d == min_d:
                points = max_points
            else:
                t = (duration - min_d) / (max_d - min_d)
                points = max_points - t * (max_points - min_points)
            results[team_id]["points"] = _round_score(_clamp_non_negative(points))
    return results


# ---------------------------------------------------------------------------
# Category rules (found points + race time rule)
# ---------------------------------------------------------------------------


def get_team_dead_time_total(comp_id: int, team_id: int) -> float:
    """Latest per-CP dead_time entries plus Team.bonus_dead_time, minutes."""
    entries = (
        ScoreEntry.query.filter(
            ScoreEntry.competition_id == comp_id,
            ScoreEntry.team_id == team_id,
        )
        .order_by(ScoreEntry.created_at.desc())
        .all()
    )
    latest_by_cp: dict[int, ScoreEntry] = {}
    for entry in entries:
        if entry.checkpoint_id not in latest_by_cp:
            latest_by_cp[entry.checkpoint_id] = entry
    total_dead = 0.0
    for entry in latest_by_cp.values():
        raw = entry.raw_fields or {}
        num = _to_number(raw.get("dead_time", raw.get("Dead Time")))
        if num is not None and num > 0:
            total_dead += num
    team = db.session.get(Team, team_id)
    if team and team.bonus_dead_time:
        try:
            total_dead += float(team.bonus_dead_time)
        except (TypeError, ValueError):
            pass
    return total_dead


def compute_group_contrib(comp_id: int, team_id: int, group: CheckpointGroup | None) -> dict:
    """Found points + race time points for one team from GroupScoring.

    Race rule: elapsed = route finish - route start (traversal direction),
    minus accumulated dead time. Within threshold -> max points; over it,
    the deduction is stepped per FULL penalty block:
    floor(over / penalty_minutes) * penalty_points, floored at min_points.
    dq_multiplier over threshold auto-DNFs.
    """
    scoring: GroupScoring | None = group.scoring if group else None
    if not scoring:
        return {"total": None, "found_points": None, "time_points": None, "auto_dnf": False}

    total = 0.0
    used = False
    found_points = None
    time_points = None
    auto_dnf = False

    route = resolve_route_ids(group)

    points_per = _to_number(scoring.found_points_per)
    if points_per is not None and route:
        distinct_route = list(dict.fromkeys(route))
        eligible = {
            cp_id
            for (cp_id,) in db.session.query(Checkpoint.id).filter(
                Checkpoint.id.in_(distinct_route),
                Checkpoint.counts_for_found.is_(True),
            )
        }
        if eligible:
            found = (
                Checkin.query.filter(
                    Checkin.competition_id == comp_id,
                    Checkin.team_id == team_id,
                    Checkin.checkpoint_id.in_(eligible),
                )
                .with_entities(Checkin.checkpoint_id)
                .distinct()
                .count()
            )
            found_points = _round_score(points_per * found)
            total += found_points
            used = True

    max_points = _to_number(scoring.race_max_points)
    threshold = _to_number(scoring.race_threshold_minutes)
    penalty_minutes = _to_number(scoring.race_penalty_minutes)
    penalty_points = _to_number(scoring.race_penalty_points)
    min_points = _to_number(scoring.race_min_points) or 0.0
    dq_multiplier = _to_number(scoring.race_dq_multiplier)
    if (
        route
        and max_points is not None
        and threshold is not None
        and penalty_minutes
        and penalty_points is not None
    ):
        start_map = _first_checkin_times(comp_id, [team_id], route[0])
        end_map = _first_checkin_times(comp_id, [team_id], route[-1])
        start_ts = start_map.get(team_id)
        end_ts = end_map.get(team_id)
        if start_ts and end_ts:
            raw_duration = (end_ts - start_ts).total_seconds() / 60.0
            duration = max(0.0, raw_duration - get_team_dead_time_total(comp_id, team_id))
            if dq_multiplier is not None and dq_multiplier > 0 and duration > threshold * dq_multiplier:
                auto_dnf = True
            if duration <= threshold:
                time_points = max_points
            else:
                over = duration - threshold
                time_points = max_points - math.floor(over / penalty_minutes) * penalty_points
            time_points = _round_score(_clamp_non_negative(max(time_points, min_points)))
            total += time_points
            used = True

    return {
        "total": (_round_score(total) if used else None),
        "found_points": found_points,
        "time_points": time_points,
        "auto_dnf": auto_dnf,
    }


def segment_end_checkpoint_ids(comp_id: int) -> set[int]:
    """Checkpoints that are a segment END for at least one group.

    Dead time may be awarded at a segment's start but never at its end
    (decisions log): a directed end depends on each group's direction, so
    a path run both ways blocks both endpoints. A segment on a path with
    no groups blocks its stored (forward) end.
    """
    segments = TimedSegment.query.filter(TimedSegment.competition_id == comp_id).all()
    if not segments:
        return set()
    directions_by_path: dict[int, set[str]] = {}
    for group_path_id, direction in db.session.query(CheckpointGroup.path_id, CheckpointGroup.direction).filter(
        CheckpointGroup.competition_id == comp_id, CheckpointGroup.path_id.isnot(None)
    ):
        directions_by_path.setdefault(group_path_id, set()).add(direction)
    blocked: set[int] = set()
    for segment in segments:
        directions = directions_by_path.get(segment.path_id) or {"forward"}
        if "forward" in directions:
            blocked.add(segment.end_checkpoint_id)
        if "reverse" in directions:
            blocked.add(segment.start_checkpoint_id)
    return blocked
