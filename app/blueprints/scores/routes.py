# app/blueprints/scores/routes.py
from __future__ import annotations

import csv
import io
import json
from datetime import datetime

from flask import Blueprint, Response, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _
from flask_login import current_user
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import (
    Checkin,
    Checkpoint,
    CheckpointGroup,
    Competition,
    GroupScoring,
    JudgeCheckpoint,
    Path,
    ScoreEntry,
    ScoreField,
    ScoreFieldGroup,
    Team,
    TeamGroup,
    TimedSegment,
)
from app.resources.scores import recompute_entry_totals
from app.utils.audit import record_audit_event
from app.utils.competition import get_current_competition_id, get_current_competition_role
from app.utils.paths import (
    resolve_route_ids,
    resolve_route_ids_bulk,
)
from app.utils.perms import roles_required
from app.utils.time import format_datetime_display, format_time_display

scores_bp = Blueprint("scores", __name__, template_folder="../../templates")


@scores_bp.route("/judge", methods=["GET"])
@roles_required("judge", "admin")
def judge_score():
    # Superseded for judges by the /judge shell (phase 3); kept for
    # admins until the phase-5 cleanup removes it entirely.
    if get_current_competition_role() == "judge":
        return redirect(url_for("judge.home"))
    comp_id = get_current_competition_id()
    checkpoints = []
    default_checkpoint_id = None
    teams = []
    if comp_id:
        role = get_current_competition_role()
        if role == "admin":
            checkpoints = (
                Checkpoint.query.filter(Checkpoint.competition_id == comp_id)
                .order_by(Checkpoint.position.asc().nulls_last(), Checkpoint.name.asc())
                .all()
            )
            default_checkpoint_id = checkpoints[0].id if checkpoints else None
        else:
            assigned = (
                JudgeCheckpoint.query.join(Checkpoint, JudgeCheckpoint.checkpoint_id == Checkpoint.id)
                .filter(
                    JudgeCheckpoint.user_id == current_user.id,
                    Checkpoint.competition_id == comp_id,
                )
                .order_by(Checkpoint.position.asc().nulls_last(), Checkpoint.name.asc())
                .all()
            )
            checkpoints = [jc.checkpoint for jc in assigned if jc.checkpoint]
            default_row = next((jc for jc in assigned if jc.is_default), None)
            default_checkpoint_id = default_row.checkpoint_id if default_row else None
        teams = (
            Team.query.filter(Team.competition_id == comp_id)
            .order_by(Team.number.asc().nulls_last(), Team.name.asc())
            .all()
        )

    return render_template(
        "score_judge.html",
        checkpoints=checkpoints,
        default_checkpoint_id=default_checkpoint_id,
        teams=teams,
    )


@scores_bp.route("/setup", methods=["GET"])
@roles_required("admin")
def scoring_setup():
    """Scoring administration: per-checkpoint fields with the group
    matrix, timed segments per path, and category rules. Replaces the
    old JSON rule builder (/scores/rules)."""
    comp_id = get_current_competition_id()
    if not comp_id:
        flash(_("Select a competition first."), "warning")
        return redirect(url_for("main.select_competition"))

    checkpoints = (
        Checkpoint.query.filter(Checkpoint.competition_id == comp_id)
        .order_by(Checkpoint.position.asc().nulls_last(), Checkpoint.name.asc())
        .all()
    )
    groups = (
        CheckpointGroup.query.filter(CheckpointGroup.competition_id == comp_id)
        .order_by(CheckpointGroup.position.asc().nulls_last(), CheckpointGroup.name.asc())
        .all()
    )
    paths = Path.query.filter(Path.competition_id == comp_id).order_by(Path.name.asc()).all()
    fields = (
        ScoreField.query.filter(ScoreField.competition_id == comp_id)
        .order_by(ScoreField.checkpoint_id.asc(), ScoreField.position.asc(), ScoreField.id.asc())
        .all()
    )
    overrides = (
        ScoreFieldGroup.query.join(ScoreField, ScoreFieldGroup.score_field_id == ScoreField.id)
        .filter(ScoreField.competition_id == comp_id)
        .all()
    )
    override_map = {(o.score_field_id, o.group_id): o for o in overrides}
    segments = (
        TimedSegment.query.filter(TimedSegment.competition_id == comp_id)
        .order_by(TimedSegment.path_id.asc(), TimedSegment.id.asc())
        .all()
    )
    scoring_rows = GroupScoring.query.filter(GroupScoring.competition_id == comp_id).all()
    scoring_by_group = {row.group_id: row for row in scoring_rows}
    cp_name_by_id = {cp.id: cp.name for cp in checkpoints}
    path_stop_options = {
        path.id: [
            {"id": cp_id, "name": cp_name_by_id.get(cp_id, "?")}
            for cp_id in dict.fromkeys(stop.checkpoint_id for stop in path.stops)
        ]
        for path in paths
    }

    fields_by_cp: dict[int, list[ScoreField]] = {}
    for field in fields:
        fields_by_cp.setdefault(field.checkpoint_id, []).append(field)

    return render_template(
        "scoring_setup.html",
        checkpoints=checkpoints,
        groups=groups,
        paths=paths,
        fields_by_cp=fields_by_cp,
        override_map=override_map,
        segments=segments,
        scoring_by_group=scoring_by_group,
        path_stop_options=path_stop_options,
        cp_name_by_id=cp_name_by_id,
    )


def _parse_float(raw) -> float | None:
    raw = (raw or "").strip() if isinstance(raw, str) else raw
    if raw in (None, ""):
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


@scores_bp.route("/setup/fields", methods=["POST"])
@roles_required("admin")
def scoring_setup_fields():
    """Create/update/delete a checkpoint's fields and the group matrix.

    Form model: one submit per checkpoint. Existing fields arrive as
    field_<id>_* inputs; a filled new_key row adds a field; delete via
    the per-row delete checkbox. Group enablement arrives as
    enabled_<field_id>_<group_id> checkboxes (a hidden marker input tells
    checked-state apart from 'matrix not rendered').
    """
    comp_id = get_current_competition_id()
    if not comp_id:
        flash(_("Select a competition first."), "warning")
        return redirect(url_for("main.select_competition"))

    checkpoint_id = request.form.get("checkpoint_id", type=int)
    checkpoint = Checkpoint.query.filter(
        Checkpoint.competition_id == comp_id, Checkpoint.id == checkpoint_id
    ).first()
    if not checkpoint:
        flash(_("Checkpoint not found."), "warning")
        return redirect(url_for("scores.scoring_setup"))

    groups = CheckpointGroup.query.filter(CheckpointGroup.competition_id == comp_id).all()
    fields = ScoreField.query.filter(ScoreField.checkpoint_id == checkpoint_id).all()

    def _rule_params_from_form(prefix: str) -> tuple[str, dict | None, str | None]:
        rule_type = (request.form.get(f"{prefix}_rule_type") or "none").strip().lower()
        if rule_type not in ("none", "mapping", "interpolate", "multiplier", "deviation"):
            rule_type = "none"
        raw_params = (request.form.get(f"{prefix}_rule_params") or "").strip()
        params = None
        if raw_params:
            try:
                parsed = json.loads(raw_params)
            except Exception as exc:
                return rule_type, None, str(exc)
            if not isinstance(parsed, dict):
                return rule_type, None, "rule params must be a JSON object"
            params = parsed or None
        return rule_type, params, None

    for field in fields:
        prefix = f"field_{field.id}"
        if request.form.get(f"{prefix}_delete"):
            db.session.delete(field)
            continue
        if f"{prefix}_key" not in request.form:
            continue
        key = (request.form.get(f"{prefix}_key") or "").strip()
        if key:
            field.key = key[:80]
        field.label = (request.form.get(f"{prefix}_label") or "").strip()[:160] or None
        field.hint = (request.form.get(f"{prefix}_hint") or "").strip()[:255] or None
        field.position = request.form.get(f"{prefix}_position", type=int) or 0
        rule_type, params, err = _rule_params_from_form(prefix)
        if err:
            flash(_("Invalid rule params for %(key)s: %(error)s", key=field.key, error=err), "warning")
            return redirect(url_for("scores.scoring_setup"))
        field.rule_type = rule_type
        field.rule_params = params
        field.max_input = _parse_float(request.form.get(f"{prefix}_max_input"))
        field.counts_in_total = bool(request.form.get(f"{prefix}_counts"))

    new_key = (request.form.get("new_key") or "").strip()
    if new_key:
        rule_type, params, err = _rule_params_from_form("new")
        if err:
            flash(_("Invalid rule params for %(key)s: %(error)s", key=new_key, error=err), "warning")
            return redirect(url_for("scores.scoring_setup"))
        max_position = max((f.position for f in fields), default=-1)
        db.session.add(
            ScoreField(
                competition_id=comp_id,
                checkpoint_id=checkpoint_id,
                key=new_key[:80],
                label=(request.form.get("new_label") or "").strip()[:160] or None,
                hint=(request.form.get("new_hint") or "").strip()[:255] or None,
                position=max_position + 1,
                rule_type=rule_type,
                rule_params=params,
                max_input=_parse_float(request.form.get("new_max_input")),
                # No "on" default: an unchecked checkbox sends nothing, so
                # defaulting to "on" made counts_in_total always True and
                # a non-counting field impossible to create (matches the
                # existing-field update above).
                counts_in_total=bool(request.form.get("new_counts")),
            )
        )

    db.session.flush()
    # Group matrix: rows exist only where a group deviates from the
    # default (disabled). Overrides via JSON stay admin-side advanced.
    if request.form.get("matrix_present"):
        current_fields = ScoreField.query.filter(ScoreField.checkpoint_id == checkpoint_id).all()
        for field in current_fields:
            for group in groups:
                enabled = bool(request.form.get(f"enabled_{field.id}_{group.id}"))
                row = ScoreFieldGroup.query.filter_by(score_field_id=field.id, group_id=group.id).first()
                if enabled and row is not None and row.rule_override is None:
                    db.session.delete(row)
                elif enabled and row is not None:
                    row.enabled = True
                elif not enabled and row is None:
                    db.session.add(ScoreFieldGroup(score_field_id=field.id, group_id=group.id, enabled=False))
                elif not enabled and row is not None:
                    row.enabled = False

    record_audit_event(
        competition_id=comp_id,
        event_type="score_fields_updated",
        entity_type="checkpoint",
        entity_id=checkpoint_id,
        actor_user=current_user if current_user.is_authenticated else None,
        summary=f"Score fields updated for {checkpoint.name}.",
        details=None,
    )
    db.session.commit()

    for group in groups:
        try:
            recompute_entry_totals(comp_id, checkpoint_id, group.id)
        except Exception:
            pass
    flash(_("Score fields saved."), "success")
    return redirect(url_for("scores.scoring_setup"))


@scores_bp.route("/setup/segments", methods=["POST"])
@roles_required("admin")
def scoring_setup_segments():
    """Add a timed segment to a path (start/end checkpoint + points)."""
    comp_id = get_current_competition_id()
    if not comp_id:
        flash(_("Select a competition first."), "warning")
        return redirect(url_for("main.select_competition"))

    path_id = request.form.get("path_id", type=int)
    path = Path.query.filter(Path.competition_id == comp_id, Path.id == path_id).first()
    if not path:
        flash(_("Path not found."), "warning")
        return redirect(url_for("scores.scoring_setup"))

    start_id = request.form.get("start_checkpoint_id", type=int)
    end_id = request.form.get("end_checkpoint_id", type=int)
    stop_ids = {stop.checkpoint_id for stop in path.stops}
    if not start_id or not end_id or start_id == end_id or start_id not in stop_ids or end_id not in stop_ids:
        flash(_("Segment endpoints must be two different checkpoints on the path."), "warning")
        return redirect(url_for("scores.scoring_setup"))

    # Dead time may be awarded at a segment start but never at its end.
    # Either endpoint can be an end for some direction, so check both
    # against groups actually using the path (see segment_end_checkpoint_ids).

    directions = {g.direction for g in path.groups} or {"forward"}
    blocked = set()
    if "forward" in directions:
        blocked.add(end_id)
    if "reverse" in directions:
        blocked.add(start_id)
    dead_cps = {
        cp.id
        for cp in Checkpoint.query.filter(
            Checkpoint.id.in_(blocked), Checkpoint.dead_time_enabled.is_(True)
        ).all()
    }
    if dead_cps:
        flash(_("A segment cannot end at a checkpoint with dead time enabled."), "warning")
        return redirect(url_for("scores.scoring_setup"))

    # `or default` would rewrite an explicit 0 to the default; keep a real
    # 0 (e.g. a zero-weight segment) with an is-None check instead.
    max_points = _parse_float(request.form.get("max_points"))
    min_points = _parse_float(request.form.get("min_points"))
    db.session.add(
        TimedSegment(
            competition_id=comp_id,
            path_id=path.id,
            start_checkpoint_id=start_id,
            end_checkpoint_id=end_id,
            name=(request.form.get("name") or "").strip()[:120] or None,
            max_points=100.0 if max_points is None else max_points,
            min_points=0.0 if min_points is None else min_points,
        )
    )
    record_audit_event(
        competition_id=comp_id,
        event_type="timed_segment_created",
        entity_type="path",
        entity_id=path.id,
        actor_user=current_user if current_user.is_authenticated else None,
        summary=f"Timed segment added on path {path.name}.",
        details=None,
    )
    db.session.commit()
    flash(_("Timed segment added."), "success")
    return redirect(url_for("scores.scoring_setup"))


@scores_bp.route("/setup/segments/<int:segment_id>/delete", methods=["POST"])
@roles_required("admin")
def scoring_setup_segment_delete(segment_id: int):
    comp_id = get_current_competition_id()
    if not comp_id:
        flash(_("Select a competition first."), "warning")
        return redirect(url_for("main.select_competition"))
    segment = TimedSegment.query.filter(
        TimedSegment.competition_id == comp_id, TimedSegment.id == segment_id
    ).first()
    if segment:
        db.session.delete(segment)
        db.session.commit()
        flash(_("Timed segment deleted."), "success")
    return redirect(url_for("scores.scoring_setup"))


@scores_bp.route("/setup/group-scoring", methods=["POST"])
@roles_required("admin")
def scoring_setup_group():
    """Save one category's found points + race time rule."""
    comp_id = get_current_competition_id()
    if not comp_id:
        flash(_("Select a competition first."), "warning")
        return redirect(url_for("main.select_competition"))

    group_id = request.form.get("group_id", type=int)
    group = CheckpointGroup.query.filter(
        CheckpointGroup.competition_id == comp_id, CheckpointGroup.id == group_id
    ).first()
    if not group:
        flash(_("Group not found."), "warning")
        return redirect(url_for("scores.scoring_setup"))

    values = {
        "found_points_per": _parse_float(request.form.get("found_points_per")),
        "race_max_points": _parse_float(request.form.get("race_max_points")),
        "race_threshold_minutes": _parse_float(request.form.get("race_threshold_minutes")),
        "race_penalty_minutes": _parse_float(request.form.get("race_penalty_minutes")),
        "race_penalty_points": _parse_float(request.form.get("race_penalty_points")),
        "race_min_points": _parse_float(request.form.get("race_min_points")),
        "race_dq_multiplier": _parse_float(request.form.get("race_dq_multiplier")),
    }
    race_values = [v for k, v in values.items() if k.startswith("race_")]
    if any(v is not None for v in race_values) and (
        values["race_max_points"] is None
        or values["race_threshold_minutes"] is None
        or values["race_penalty_minutes"] in (None, 0)
        or values["race_penalty_points"] is None
    ):
        flash(
            _("The race time rule needs max points, threshold, penalty minutes and penalty points."),
            "warning",
        )
        return redirect(url_for("scores.scoring_setup"))

    row = GroupScoring.query.filter_by(group_id=group.id).first()
    if all(v is None for v in values.values()):
        if row:
            db.session.delete(row)
    else:
        if not row:
            row = GroupScoring(group_id=group.id, competition_id=comp_id)
            db.session.add(row)
        for key, value in values.items():
            setattr(row, key, value)
    record_audit_event(
        competition_id=comp_id,
        event_type="group_scoring_updated",
        entity_type="group",
        entity_id=group.id,
        actor_user=current_user if current_user.is_authenticated else None,
        summary=f"Category scoring updated for {group.name}.",
        details=values,
    )
    db.session.commit()
    flash(_("Category scoring saved."), "success")
    return redirect(url_for("scores.scoring_setup"))


def _build_scores_context(comp_id: int, group_id: int | None, persist_auto_dnf: bool = False) -> dict:
    # persist_auto_dnf: whether to durably mark auto-DNF teams. Defaults
    # False so read-only surfaces (the PUBLIC unauthenticated results
    # page, CSV export, stats, judge results, the Sheets worker) never
    # write to the DB just because someone rendered the leaderboard. Only
    # the authenticated admin leaderboard passes True. Auto-DNF is still
    # shown everywhere; it is just not materialized on a read.
    groups = (
        CheckpointGroup.query.filter(CheckpointGroup.competition_id == comp_id)
        .order_by(CheckpointGroup.position.asc().nulls_last(), CheckpointGroup.name.asc())
        .all()
    )
    group_order = [g.name for g in groups if g.name]
    teams_query = Team.query.filter(Team.competition_id == comp_id)
    if group_id:
        teams_query = teams_query.join(TeamGroup, TeamGroup.team_id == Team.id).filter(
            TeamGroup.group_id == group_id, TeamGroup.active.is_(True)
        )
    teams = teams_query.order_by(Team.number.asc().nulls_last(), Team.name.asc()).all()
    team_ids = [t.id for t in teams]

    entries = []
    if team_ids:
        entries = (
            ScoreEntry.query.filter(ScoreEntry.competition_id == comp_id)
            .filter(ScoreEntry.team_id.in_(team_ids))
            .order_by(ScoreEntry.created_at.desc())
            .all()
        )

    latest = {}
    for entry in entries:
        key = (entry.team_id, entry.checkpoint_id)
        if key not in latest:
            latest[key] = entry

    team_groups = {}
    team_group_ids = {}
    if team_ids:
        links = TeamGroup.query.filter(TeamGroup.team_id.in_(team_ids), TeamGroup.active.is_(True)).all()
        for link in links:
            if link.team_id not in team_groups and link.group:
                team_groups[link.team_id] = link.group.name
                team_group_ids[link.team_id] = link.group_id

    group_checkpoint_ids = {}
    group_checkpoint_order = {}
    group_final_checkpoint = {}
    group_start_checkpoint = {}
    if team_group_ids:
        # Directed routes from the path resolver: start/finish now follow
        # the group's traversal direction (the old link-order derivation
        # ignored reversed groups and computed elapsed time backwards).
        routes = resolve_route_ids_bulk(comp_id)
        unique_group_ids = sorted({gid for gid in team_group_ids.values() if gid})
        for gid in unique_group_ids:
            route = routes.get(gid) or []
            if not route:
                continue
            group_checkpoint_order[gid] = route
            group_checkpoint_ids[gid] = set(route)
            group_start_checkpoint[gid] = route[0]
            group_final_checkpoint[gid] = route[-1]

    # Per-CP totals from the latest ScoreEntry per (team, checkpoint),
    # restricted to checkpoints on the team's route.
    totals = {team_id: 0.0 for team_id in team_ids}
    dead_times = {team_id: 0.0 for team_id in team_ids}
    per_team_points: dict[int, dict[int, float | None]] = {team_id: {} for team_id in team_ids}
    allowed_checkpoint_ids = {team_id: set() for team_id in team_ids}
    for (team_id, _checkpoint_id), entry in latest.items():
        group_id_for_team = team_group_ids.get(team_id)
        if group_id_for_team:
            allowed = group_checkpoint_ids.get(group_id_for_team, set())
            if entry.checkpoint_id not in allowed:
                continue
        if entry.total is not None:
            totals[team_id] += float(entry.total)
        per_team_points.setdefault(team_id, {})[entry.checkpoint_id] = entry.total
        raw = entry.raw_fields or {}
        dead_val = raw.get("dead_time", raw.get("Dead Time"))
        try:
            dead_num = float(dead_val)
        except Exception:
            dead_num = None
        if dead_num is not None:
            dead_times[team_id] += dead_num
    for team_id in team_ids:
        group_id_for_team = team_group_ids.get(team_id)
        if group_id_for_team:
            allowed_checkpoint_ids[team_id] = group_checkpoint_ids.get(group_id_for_team, set())

    # Whole-race elapsed minutes between the route's directed start/finish.
    team_time_minutes: dict[int, float | None] = {team_id: None for team_id in team_ids}
    if team_ids and group_start_checkpoint and group_final_checkpoint:
        start_ids = {cid for cid in group_start_checkpoint.values() if cid}
        end_ids = {cid for cid in group_final_checkpoint.values() if cid}
        checkins = (
            Checkin.query.filter(Checkin.competition_id == comp_id)
            .filter(Checkin.team_id.in_(team_ids))
            .filter(Checkin.checkpoint_id.in_(start_ids.union(end_ids)))
            .order_by(Checkin.timestamp.asc())
            .all()
        )
        team_cp_times: dict[int, dict[int, datetime]] = {tid: {} for tid in team_ids}
        for c in checkins:
            if c.checkpoint_id not in team_cp_times.get(c.team_id, {}):
                team_cp_times.setdefault(c.team_id, {})[c.checkpoint_id] = c.timestamp
        for team_id in team_ids:
            group_id_for_team = team_group_ids.get(team_id)
            if not group_id_for_team:
                continue
            start_id = group_start_checkpoint.get(group_id_for_team)
            end_id = group_final_checkpoint.get(group_id_for_team)
            if not start_id or not end_id:
                continue
            start_ts = team_cp_times.get(team_id, {}).get(start_id)
            end_ts = team_cp_times.get(team_id, {}).get(end_id)
            if start_ts and end_ts and end_ts >= start_ts:
                team_time_minutes[team_id] = (end_ts - start_ts).total_seconds() / 60.0

    # Timed segments per group (endpoints already direction-swapped by the
    # resolver) and their per-team results, computed at read time, never
    # stored in ScoreEntry, so the leaderboard is always live.
    from app.utils.scoring import compute_group_contrib, compute_segment_results, resolve_group_segments

    group_by_id = {g.id: g for g in groups}
    segment_results: dict[int, dict[int, list[dict]]] = {}  # group_id -> team_id -> [results]
    if team_group_ids:
        teams_by_group: dict[int, list[int]] = {}
        for tid, gid in team_group_ids.items():
            if gid:
                teams_by_group.setdefault(gid, []).append(tid)
        for gid, pool in teams_by_group.items():
            segments = resolve_group_segments(group_by_id.get(gid))
            per_team: dict[int, list[dict]] = {}
            for segment in segments:
                results = compute_segment_results(comp_id, pool, segment)
                for tid, result in results.items():
                    per_team.setdefault(tid, []).append(result)
            segment_results[gid] = per_team

    # Category-level contribution (found points + race time rule).
    global_totals = {}
    global_time_points = {}
    global_found_points = {}
    auto_dnf_ids: set[int] = set()
    for team_id in team_ids:
        group_id_for_team = team_group_ids.get(team_id)
        if not group_id_for_team:
            global_totals[team_id] = 0.0
            global_time_points[team_id] = 0.0
            global_found_points[team_id] = 0.0
            continue
        contrib = compute_group_contrib(comp_id, team_id, group_by_id.get(group_id_for_team))
        global_totals[team_id] = contrib["total"] or 0.0
        global_time_points[team_id] = contrib["time_points"] or 0.0
        global_found_points[team_id] = contrib["found_points"] or 0.0
        # Auto-DNF from the race time rule's dq multiplier. Shown on every
        # surface via auto_dnf_ids; only materialized when the caller is
        # allowed to write (the authenticated admin view).
        if contrib.get("auto_dnf"):
            auto_dnf_ids.add(team_id)
    if persist_auto_dnf and auto_dnf_ids:
        changed = False
        for team_id in auto_dnf_ids:
            team_obj = db.session.get(Team, team_id)
            if team_obj and not team_obj.dnf:
                team_obj.dnf = True
                changed = True
        if changed:
            db.session.commit()

    rows = []
    finished_map = {team_id: False for team_id in team_ids}
    final_checkpoint_ids = {cp_id for cp_id in group_final_checkpoint.values() if cp_id}
    if team_ids and final_checkpoint_ids:
        checkins = (
            db.session.query(Checkin.team_id, Checkin.checkpoint_id)
            .filter(Checkin.competition_id == comp_id)
            .filter(Checkin.team_id.in_(team_ids))
            .filter(Checkin.checkpoint_id.in_(final_checkpoint_ids))
            .all()
        )
        for team_id, checkpoint_id in checkins:
            group_id_for_team = team_group_ids.get(team_id)
            if group_id_for_team and group_final_checkpoint.get(group_id_for_team) == checkpoint_id:
                finished_map[team_id] = True

    for team in teams:
        team_group_id = team_group_ids.get(team.id)
        raw_min = team_time_minutes.get(team.id)
        team_obj = next((t for t in teams if t.id == team.id), None)
        # Timed segments: four values per segment (contract from the
        # redesign plan 3.3): start/end arrival, diff, points. Segment
        # points join the total here; they are never stored in ScoreEntry.
        team_segments = []
        segment_points_total = 0.0
        for result in (segment_results.get(team_group_id, {}) or {}).get(team.id, []):
            if result.get("points") is not None:
                segment_points_total += float(result["points"])
            team_segments.append(
                {
                    "label": result["label"],
                    "start_at": result["start_at"],
                    "end_at": result["end_at"],
                    "start_hms": format_time_display(result["start_at"]),
                    "end_hms": format_time_display(result["end_at"]),
                    "minutes": result["minutes"],
                    "points": result["points"],
                }
            )
        team_total = totals.get(team.id, 0.0) + global_totals.get(team.id, 0.0) + segment_points_total
        rows.append(
            {
                "id": team.id,
                "name": team.name,
                "number": team.number,
                "group": team_groups.get(team.id, ""),
                "total": team_total,
                "dead_time": dead_times.get(team.id, 0.0),
                "bonus_dead_time": float(team_obj.bonus_dead_time) if (team_obj and team_obj.bonus_dead_time) else 0.0,
                "global_time": global_time_points.get(team.id, 0.0),
                "global_found": global_found_points.get(team.id, 0.0),
                "time_minutes": raw_min,
                "notes": team_obj.notes if team_obj and team_obj.notes else "",
                "segments": team_segments,
                "segment_points": segment_points_total,
                "dnf": bool(team.dnf) or team.id in auto_dnf_ids,
                "finished": finished_map.get(team.id, False),
                "organization": team.organization or "",
                "allowed_checkpoints": allowed_checkpoint_ids.get(team.id, set()),
            }
        )

    group_order_norm = [g.lower().strip() for g in group_order]

    def _row_sort_key(row: dict):
        group_name = (row.get("group") or "").strip()
        group_norm = group_name.lower().strip()
        group_idx = group_order_norm.index(group_norm) if group_norm in group_order_norm else len(group_order_norm)
        total_val = float(row.get("total") or 0.0)
        return (group_idx, group_name, 1 if row.get("dnf") else 0, -total_val, row.get("name") or "")

    rows.sort(key=_row_sort_key)
    place_by_team = {}
    current_group = None
    current_place = 0
    for row in rows:
        group_name = row.get("group") or ""
        if group_name != current_group:
            current_group = group_name
            current_place = 0
        current_place += 1
        place_by_team[row["id"]] = current_place
        row["place"] = current_place

    org_totals_map: dict[str, dict] = {}
    for row in rows:
        org = (row.get("organization") or "").strip()
        if not org or row.get("dnf"):
            continue
        entry = org_totals_map.setdefault(org, {"name": org, "total": 0.0, "team_count": 0})
        entry["total"] += float(row.get("total") or 0.0)
        entry["team_count"] += 1
    # Sort by total descending so the org-totals table reads as a
    # leaderboard, not an alphabetical list. Tie-break on name for stability.
    org_totals = sorted(
        org_totals_map.values(),
        key=lambda e: (-float(e["total"] or 0.0), e["name"].lower()),
    )

    checkpoints_query = Checkpoint.query.filter(Checkpoint.competition_id == comp_id)
    if group_id:
        selected_group = db.session.get(CheckpointGroup, group_id)
        cp_ids = resolve_route_ids(selected_group)
        if cp_ids:
            checkpoints_query = checkpoints_query.filter(Checkpoint.id.in_(cp_ids))
    checkpoints = checkpoints_query.order_by(Checkpoint.position.asc().nulls_last(), Checkpoint.name.asc()).all()

    return {
        "rows": rows,
        "groups": groups,
        "selected_group_id": group_id,
        "checkpoints": checkpoints,
        "per_team_points": per_team_points,
        "org_totals": org_totals,
        "allowed_checkpoint_ids": allowed_checkpoint_ids,
    }


@scores_bp.route("/view", methods=["GET"])
@roles_required("judge", "admin")
def view_scores():
    comp_id = get_current_competition_id()
    if not comp_id:
        flash(_("Select a competition first."), "warning")
        return redirect(url_for("main.select_competition"))

    group_id = request.args.get("group_id", type=int)
    # Authenticated interactive leaderboard: OK to materialize auto-DNF.
    context = _build_scores_context(comp_id, group_id, persist_auto_dnf=True)
    context["show_actions"] = True
    return render_template("scores_view.html", **context)


@scores_bp.route("/view/export.csv", methods=["GET"])
@roles_required("judge", "admin")
def view_scores_export_csv():
    """Same data as /scores/view's extended (per-CP) table, flattened
    to CSV. Honors ?group_id=. Columns mirror the on-screen extended
    table: rank/group/number/team/finished/time_minutes, one column per
    checkpoint, four columns per timed segment (label+start/end arrival/
    minutes/points), then time_points/found_points/dead_time/total. Use
    this as the canonical export to recreate the leaderboard in Sheets
    when sheets sync is unavailable."""
    comp_id = get_current_competition_id()
    if not comp_id:
        flash(_("Select a competition first."), "warning")
        return redirect(url_for("main.select_competition"))

    group_id = request.args.get("group_id", type=int)
    context = _build_scores_context(comp_id, group_id)
    rows = context.get("rows") or []
    checkpoints = context.get("checkpoints") or []
    per_team_points = context.get("per_team_points") or {}

    def fmt(val, spec):
        return format(val, spec) if val is not None else ""

    buf = io.StringIO()
    w = csv.writer(buf)
    header = ["rank", "group", "number", "team", "organization", "finished", "dnf", "time_minutes"]
    # Per-CP header: "<name> — <description>" if a description exists,
    # else just the name. Was "cp.<name>" before — operators wanted the
    # raw CP name plus context, without a synthetic prefix muddying it.
    header += [
        f"{cp.name} — {cp.description.strip()}" if (cp.description and cp.description.strip()) else cp.name
        for cp in checkpoints
    ]
    # Four values per timed segment (redesign plan 3.3); rows carry as
    # many segments as their group's path defines, so the column count is
    # the maximum across the export.
    max_segments = max((len(r.get("segments") or []) for r in rows), default=0)
    for idx in range(1, max_segments + 1):
        header += [
            f"time_trial_{idx}",
            f"time_trial_{idx}_start",
            f"time_trial_{idx}_end",
            f"time_trial_{idx}_minutes",
            f"time_trial_{idx}_points",
        ]
    header += ["time_points", "found_points", "dead_time", "total"]
    w.writerow(header)

    for i, r in enumerate(rows, 1):
        team_id = r.get("id")
        team_cp_points = per_team_points.get(team_id, {}) if team_id else {}
        allowed = r.get("allowed_checkpoints") or set()
        per_cp_cells = []
        for cp in checkpoints:
            val = team_cp_points.get(cp.id)
            if r.get("dnf"):
                per_cp_cells.append("DNF")
            elif val is not None:
                per_cp_cells.append(fmt(val, ".2f"))
            elif cp.id in allowed:
                per_cp_cells.append("0")
            else:
                per_cp_cells.append("")
        segment_cells = []
        for segment in r.get("segments") or []:
            segment_cells += [
                segment.get("label", ""),
                format_datetime_display(segment.get("start_at")),
                format_datetime_display(segment.get("end_at")),
                fmt(segment.get("minutes"), ".2f"),
                fmt(segment.get("points"), ".2f"),
            ]
        segment_cells += [""] * (5 * max_segments - len(segment_cells))
        w.writerow(
            [
                i,
                r.get("group", ""),
                r.get("number", "") if r.get("number") is not None else "",
                r.get("name", ""),
                r.get("organization", ""),
                "YES" if r.get("finished") else "",
                "YES" if r.get("dnf") else "",
                fmt(r.get("time_minutes"), ".2f"),
                *per_cp_cells,
                *segment_cells,
                fmt(r.get("global_time"), ".2f"),
                fmt(r.get("global_found"), ".2f"),
                fmt(r.get("dead_time"), ".0f"),
                fmt(r.get("total"), ".2f"),
            ]
        )

    filename = f"scores_comp{comp_id}"
    if group_id:
        filename += f"_group{group_id}"
    filename += ".csv"
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename={filename}"},
    )


@scores_bp.route("/public/<int:competition_id>", methods=["GET"])
def public_scores(competition_id: int):
    competition = Competition.query.filter(Competition.id == competition_id).first()
    if not competition or not competition.public_results:
        flash(_("Public results are not enabled for this competition."), "warning")
        return redirect(url_for("main.index"))
    group_id = request.args.get("group_id", type=int)
    context = _build_scores_context(competition_id, group_id)
    context["show_actions"] = False
    context["public_competition"] = competition
    return render_template("scores_view.html", **context)


@scores_bp.route("/public/<int:competition_id>/qr.svg", methods=["GET"])
def public_scores_qr(competition_id: int):
    """Generate a QR code SVG for the public scores URL of this competition.

    Refuses to render when public_results is off so we don't accidentally
    leak a scannable handle to a private competition. SVG is pure-Python
    in the qrcode library (no PIL), and inline-able by the browser, so
    it's a one-API-call zero-dependency feature.
    """
    from flask import make_response

    competition = Competition.query.filter(Competition.id == competition_id).first()
    if not competition or not competition.public_results:
        return make_response(("", 404))

    import io

    import qrcode
    from qrcode.image.svg import SvgPathImage

    public_url = url_for(
        "scores.public_scores",
        competition_id=competition_id,
        _external=True,
    )
    img = qrcode.make(public_url, image_factory=SvgPathImage, box_size=10)
    buf = io.BytesIO()
    img.save(buf)
    resp = make_response(buf.getvalue())
    resp.headers["Content-Type"] = "image/svg+xml; charset=utf-8"
    # Spectators stay on the page; 5-minute cache is plenty and keeps load light.
    resp.headers["Cache-Control"] = "public, max-age=300"
    return resp


@scores_bp.route("/stats", methods=["GET"])
@roles_required("judge", "admin")
def score_stats():
    comp_id = get_current_competition_id()
    if not comp_id:
        flash(_("Select a competition first."), "warning")
        return redirect(url_for("main.select_competition"))

    context = _build_stats_context(comp_id)
    context["public_competition"] = None
    return render_template("scores_stats.html", **context)


def _build_stats_context(comp_id: int) -> dict:
    groups = (
        CheckpointGroup.query.filter(CheckpointGroup.competition_id == comp_id)
        .order_by(CheckpointGroup.position.asc().nulls_last(), CheckpointGroup.name.asc())
        .all()
    )
    context = _build_scores_context(comp_id, None)
    rows = context.get("rows", [])

    rows_by_group = {}
    for row in rows:
        group_name = (row.get("group") or "").strip()
        if not group_name:
            continue
        rows_by_group.setdefault(group_name, []).append(row)

    team_groups = (
        TeamGroup.query.join(Team, TeamGroup.team_id == Team.id)
        .filter(Team.competition_id == comp_id, TeamGroup.active.is_(True))
        .all()
    )
    teams_by_group_id = {}
    for link in team_groups:
        teams_by_group_id.setdefault(link.group_id, set()).add(link.team_id)

    # Virtual checkpoints award points but have no check-in timestamps,
    # so they must be excluded from any arrival-time based stat
    # (overall/segment durations, fastest team, drop-off, checkpoint count).
    virtual_cp_ids = {
        cp_id
        for (cp_id,) in db.session.query(Checkpoint.id)
        .filter(Checkpoint.competition_id == comp_id, Checkpoint.is_virtual.is_(True))
        .all()
    }

    # Directed per-group routes; stats segments now follow the traversal
    # direction instead of raw link order.
    checkpoint_order_by_group = resolve_route_ids_bulk(comp_id)
    checkpoint_names = {
        cp_id: name
        for (cp_id, name) in db.session.query(Checkpoint.id, Checkpoint.name)
        .filter(Checkpoint.competition_id == comp_id)
        .all()
    }

    overall_durations: list[tuple[int, str, str | None, float]] = []
    overall_checkpoint_counts: list[int] = []

    stats = []
    for group in groups:
        team_ids = sorted(teams_by_group_id.get(group.id, set()))
        if not team_ids:
            stats.append(
                {
                    "id": group.id,
                    "name": group.name,
                    "team_count": 0,
                    "finished_count": 0,
                    "completion_rate": 0,
                    "avg_points": None,
                    "avg_time_minutes": None,
                    "median_time_minutes": None,
                    "fastest_team": None,
                    "fastest_minutes": None,
                    "avg_checkpoint_count": None,
                    "segments": [],
                    "dropoff_checkpoint": None,
                    "dropoff_rate": None,
                }
            )
            continue

        group_rows = rows_by_group.get(group.name, [])
        scored_rows = [r for r in group_rows if not r.get("dnf")]
        avg_points = None
        if scored_rows:
            avg_points = sum(float(r.get("total") or 0.0) for r in scored_rows) / len(scored_rows)

        finished_count = sum(1 for r in group_rows if r.get("finished"))
        completion_rate = (finished_count / len(group_rows)) if group_rows else 0

        cp_ids = checkpoint_order_by_group.get(group.id, [])
        physical_cp_ids = [cid for cid in cp_ids if cid not in virtual_cp_ids]
        checkins = []
        if physical_cp_ids:
            checkins = (
                Checkin.query.filter(Checkin.competition_id == comp_id)
                .filter(Checkin.team_id.in_(team_ids))
                .filter(Checkin.checkpoint_id.in_(physical_cp_ids))
                .order_by(Checkin.timestamp.asc())
                .all()
            )

        team_cp_times = {tid: {} for tid in team_ids}
        for c in checkins:
            if c.checkpoint_id not in team_cp_times.get(c.team_id, {}):
                team_cp_times.setdefault(c.team_id, {})[c.checkpoint_id] = c.timestamp

        avg_checkpoint_count = None
        if physical_cp_ids:
            counts = []
            for tid in team_ids:
                counts.append(len(team_cp_times.get(tid, {})))
            avg_checkpoint_count = sum(counts) / len(counts) if counts else None
            overall_checkpoint_counts.extend(counts)

        avg_time_minutes = None
        median_time_minutes = None
        fastest_team = None
        fastest_minutes = None
        dropoff_checkpoint = None
        dropoff_rate = None
        if len(physical_cp_ids) >= 2:
            start_id = physical_cp_ids[0]
            end_id = physical_cp_ids[-1]
            durations = []
            for tid in team_ids:
                start_ts = team_cp_times.get(tid, {}).get(start_id)
                end_ts = team_cp_times.get(tid, {}).get(end_id)
                if start_ts and end_ts and end_ts >= start_ts:
                    minutes = (end_ts - start_ts).total_seconds() / 60.0
                    durations.append((tid, minutes))
            if durations:
                avg_time_minutes = sum(d[1] for d in durations) / len(durations)
                sorted_minutes = sorted(d[1] for d in durations)
                mid = len(sorted_minutes) // 2
                if len(sorted_minutes) % 2 == 1:
                    median_time_minutes = sorted_minutes[mid]
                else:
                    median_time_minutes = (sorted_minutes[mid - 1] + sorted_minutes[mid]) / 2.0
                fastest_tid, fastest_minutes = min(durations, key=lambda d: d[1])
                fastest_row = next((r for r in group_rows if r.get("id") == fastest_tid), None)
                fastest_team = fastest_row.get("name") if fastest_row else None
                for tid, minutes in durations:
                    team_row = next((r for r in group_rows if r.get("id") == tid), None)
                    team_name = team_row.get("name") if team_row else None
                    overall_durations.append((tid, group.name, team_name, minutes))

        segments = []
        if len(physical_cp_ids) >= 2:
            for idx in range(len(physical_cp_ids) - 1):
                from_id = physical_cp_ids[idx]
                to_id = physical_cp_ids[idx + 1]
                from_count = 0
                to_count = 0
                segment_durations = []
                for tid in team_ids:
                    start_ts = team_cp_times.get(tid, {}).get(from_id)
                    end_ts = team_cp_times.get(tid, {}).get(to_id)
                    if start_ts:
                        from_count += 1
                    if end_ts:
                        to_count += 1
                    if start_ts and end_ts and end_ts >= start_ts:
                        segment_durations.append((end_ts - start_ts).total_seconds() / 60.0)
                avg_segment = None
                if segment_durations:
                    avg_segment = sum(segment_durations) / len(segment_durations)
                if from_count > 0:
                    rate = max(0.0, (from_count - to_count) / from_count)
                    if dropoff_rate is None or rate > dropoff_rate:
                        dropoff_rate = rate
                        dropoff_checkpoint = checkpoint_names.get(to_id, "")
                segments.append(
                    {
                        "from_id": from_id,
                        "to_id": to_id,
                        "from_name": checkpoint_names.get(from_id, ""),
                        "to_name": checkpoint_names.get(to_id, ""),
                        "avg_minutes": avg_segment,
                        "sample_count": len(segment_durations),
                    }
                )

        stats.append(
            {
                "id": group.id,
                "name": group.name,
                "team_count": len(group_rows),
                "finished_count": finished_count,
                "completion_rate": completion_rate,
                "avg_points": avg_points,
                "avg_time_minutes": avg_time_minutes,
                "median_time_minutes": median_time_minutes,
                "fastest_team": fastest_team,
                "fastest_minutes": fastest_minutes,
                "avg_checkpoint_count": avg_checkpoint_count,
                "segments": segments,
                "dropoff_checkpoint": dropoff_checkpoint,
                "dropoff_rate": dropoff_rate,
            }
        )

    overall_team_count = len(rows)
    overall_finished = sum(1 for r in rows if r.get("finished"))
    overall_completion_rate = (overall_finished / overall_team_count) if overall_team_count else 0
    scored_rows_all = [r for r in rows if not r.get("dnf")]
    overall_avg_points = (
        sum(float(r.get("total") or 0.0) for r in scored_rows_all) / len(scored_rows_all) if scored_rows_all else None
    )
    overall_avg_time = None
    overall_median_time = None
    overall_fastest_team = None
    overall_fastest_group = None
    overall_fastest_minutes = None
    if overall_durations:
        minutes_list = [d[3] for d in overall_durations]
        overall_avg_time = sum(minutes_list) / len(minutes_list)
        sorted_minutes = sorted(minutes_list)
        mid = len(sorted_minutes) // 2
        if len(sorted_minutes) % 2 == 1:
            overall_median_time = sorted_minutes[mid]
        else:
            overall_median_time = (sorted_minutes[mid - 1] + sorted_minutes[mid]) / 2.0
        fastest = min(overall_durations, key=lambda d: d[3])
        overall_fastest_team = fastest[2]
        overall_fastest_group = fastest[1]
        overall_fastest_minutes = fastest[3]
    overall_avg_checkpoint_count = (
        sum(overall_checkpoint_counts) / len(overall_checkpoint_counts) if overall_checkpoint_counts else None
    )

    overall = {
        "team_count": overall_team_count,
        "finished_count": overall_finished,
        "completion_rate": overall_completion_rate,
        "avg_points": overall_avg_points,
        "avg_time_minutes": overall_avg_time,
        "median_time_minutes": overall_median_time,
        "fastest_team": overall_fastest_team,
        "fastest_group": overall_fastest_group,
        "fastest_minutes": overall_fastest_minutes,
        "avg_checkpoint_count": overall_avg_checkpoint_count,
        "group_count": sum(1 for g in stats if g["team_count"]),
    }

    chart_groups = [g["name"] for g in stats if g["team_count"]]
    chart_points = [g["avg_points"] or 0 for g in stats if g["team_count"]]
    chart_times = [g["avg_time_minutes"] or 0 for g in stats if g["team_count"]]

    return {
        "groups": stats,
        "overall": overall,
        "chart_groups": chart_groups,
        "chart_points": chart_points,
        "chart_times": chart_times,
    }


@scores_bp.route("/public/<int:competition_id>/stats", methods=["GET"])
def public_stats(competition_id: int):
    competition = Competition.query.filter(Competition.id == competition_id).first()
    if not competition or not competition.public_results:
        flash(_("Public results are not enabled for this competition."), "warning")
        return redirect(url_for("main.index"))
    context = _build_stats_context(competition_id)
    context["public_competition"] = competition
    return render_template("scores_stats.html", **context)


@scores_bp.route("/submissions", methods=["GET"])
@roles_required("judge", "admin")
def score_submissions():
    comp_id = get_current_competition_id()
    if not comp_id:
        flash(_("Select a competition first."), "warning")
        return redirect(url_for("main.select_competition"))

    team_id = request.args.get("team_id", type=int)
    checkpoint_id = request.args.get("checkpoint_id", type=int)
    group_id = request.args.get("group_id", type=int)

    teams_query = Team.query.filter(Team.competition_id == comp_id)
    if group_id:
        teams_query = teams_query.join(TeamGroup, TeamGroup.team_id == Team.id).filter(
            TeamGroup.group_id == group_id, TeamGroup.active.is_(True)
        )
    teams = teams_query.order_by(Team.number.asc().nulls_last(), Team.name.asc()).all()

    checkpoints = (
        Checkpoint.query.filter(Checkpoint.competition_id == comp_id)
        .order_by(Checkpoint.position.asc().nulls_last(), Checkpoint.name.asc())
        .all()
    )
    groups = (
        CheckpointGroup.query.filter(CheckpointGroup.competition_id == comp_id)
        .order_by(CheckpointGroup.position.asc().nulls_last(), CheckpointGroup.name.asc())
        .all()
    )

    query = (
        ScoreEntry.query.filter(ScoreEntry.competition_id == comp_id)
        .options(
            joinedload(ScoreEntry.team),
            joinedload(ScoreEntry.checkpoint),
            joinedload(ScoreEntry.judge_user),
        )
        .order_by(ScoreEntry.created_at.desc())
    )
    if team_id:
        query = query.filter(ScoreEntry.team_id == team_id)
    if checkpoint_id:
        query = query.filter(ScoreEntry.checkpoint_id == checkpoint_id)
    entries = query.limit(300).all()

    rows = []
    for entry in entries:
        raw = entry.raw_fields or {}
        dead_val = raw.get("dead_time", raw.get("Dead Time"))
        dead_num = None
        try:
            dead_num = float(dead_val)
        except Exception:
            dead_num = None
        # Display team as "<number> - <name>" when a number is assigned,
        # otherwise fall back to the bare name. Lets operators scan
        # submissions by team number without flipping to the roster.
        team_label = ""
        if entry.team:
            if entry.team.number is not None:
                team_label = f"{entry.team.number} - {entry.team.name}"
            else:
                team_label = entry.team.name
        rows.append(
            {
                "id": entry.id,
                "team": team_label,
                "team_id": entry.team_id,
                "checkpoint": entry.checkpoint.name if entry.checkpoint else "",
                "checkpoint_id": entry.checkpoint_id,
                "created_at": entry.created_at,
                "submitted_by": entry.judge_user.username if entry.judge_user else _("Legacy or unknown"),
                "total": entry.total,
                "dead_time": dead_num,
                "raw_fields": raw,
            }
        )

    return render_template(
        "scores_submissions.html",
        rows=rows,
        teams=teams,
        checkpoints=checkpoints,
        groups=groups,
        selected_team_id=team_id,
        selected_checkpoint_id=checkpoint_id,
        selected_group_id=group_id,
    )
