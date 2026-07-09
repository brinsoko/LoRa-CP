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
    CheckpointGroupLink,
    Competition,
    GlobalScoreRule,
    JudgeCheckpoint,
    ScoreEntry,
    ScoreRule,
    Team,
    TeamGroup,
)
from app.resources.scores import (
    _compute_global_contrib,
    _compute_time_race_scores_from_checkins,
    recompute_scores_for_rule,
)
from app.utils.competition import get_current_competition_id, get_current_competition_role
from app.utils.perms import roles_required
from app.utils.time import format_datetime_display, format_time_display

scores_bp = Blueprint("scores", __name__, template_folder="../../templates")


@scores_bp.route("/judge", methods=["GET"])
@roles_required("judge", "admin")
def judge_score():
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


@scores_bp.route("/rules", methods=["GET", "POST"])
@roles_required("admin")
def score_rules():
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
    rules = ScoreRule.query.filter(ScoreRule.competition_id == comp_id).order_by(ScoreRule.created_at.desc()).all()
    global_rules = (
        GlobalScoreRule.query.filter(GlobalScoreRule.competition_id == comp_id)
        .order_by(GlobalScoreRule.created_at.desc())
        .all()
    )

    if request.method == "POST":
        checkpoint_id = request.form.get("checkpoint_id", type=int)
        raw_group_id = (request.form.get("group_id") or "").strip()
        apply_all = raw_group_id == "__all__"
        group_id = None
        if not apply_all:
            try:
                group_id = int(raw_group_id) if raw_group_id else None
            except ValueError:
                group_id = None
        rules_text = (request.form.get("rules_json") or "").strip()
        if not checkpoint_id or (not apply_all and not group_id) or not rules_text:
            flash(_("Checkpoint, group, and rules JSON are required."), "warning")
            return redirect(url_for("scores.score_rules"))

        try:
            rules_json = json.loads(rules_text)
        except Exception as exc:
            flash(_("Invalid JSON: %(error)s", error=exc), "warning")
            return redirect(url_for("scores.score_rules"))

        if not isinstance(rules_json, dict):
            flash(_("Rules JSON must be an object."), "warning")
            return redirect(url_for("scores.score_rules"))

        if "global_rules" in rules_json:
            rules_json.pop("global_rules", None)

        if rules_json.get("time_race"):
            tr = rules_json.get("time_race") or {}
            if not tr.get("start_checkpoint_id") or not tr.get("end_checkpoint_id"):
                flash(_("Time-based scoring requires start and end checkpoints."), "warning")
                return redirect(url_for("scores.score_rules"))

        if apply_all:
            # Fan out the rule to every group linked to this checkpoint via
            # CheckpointGroupLink. Groups that don't participate at this
            # checkpoint are skipped so we don't leave dead rule rows behind.
            linked_group_ids = [
                row[0]
                for row in db.session.query(CheckpointGroupLink.group_id)
                .join(CheckpointGroup, CheckpointGroupLink.group_id == CheckpointGroup.id)
                .filter(
                    CheckpointGroupLink.checkpoint_id == checkpoint_id,
                    CheckpointGroup.competition_id == comp_id,
                )
                .all()
            ]
            if not linked_group_ids:
                flash(
                    _("No groups are linked to this checkpoint, nothing to apply."),
                    "warning",
                )
                return redirect(url_for("scores.score_rules"))

            created = 0
            updated = 0
            for gid in linked_group_ids:
                existing = ScoreRule.query.filter(
                    ScoreRule.competition_id == comp_id,
                    ScoreRule.checkpoint_id == checkpoint_id,
                    ScoreRule.group_id == gid,
                ).first()
                if existing:
                    existing.rules = rules_json
                    updated += 1
                else:
                    db.session.add(
                        ScoreRule(
                            competition_id=comp_id,
                            checkpoint_id=checkpoint_id,
                            group_id=gid,
                            rules=rules_json,
                        )
                    )
                    created += 1
            db.session.commit()
            flash(
                _(
                    "Applied to %(count)s groups (created %(created)s, updated %(updated)s).",
                    count=len(linked_group_ids),
                    created=created,
                    updated=updated,
                ),
                "success",
            )
            for gid in linked_group_ids:
                try:
                    recompute_scores_for_rule(comp_id, checkpoint_id, gid)
                except Exception:
                    pass
            return redirect(url_for("scores.score_rules"))

        existing = ScoreRule.query.filter(
            ScoreRule.competition_id == comp_id,
            ScoreRule.checkpoint_id == checkpoint_id,
            ScoreRule.group_id == group_id,
        ).first()
        if existing:
            existing.rules = rules_json
            db_msg = _("Score rule updated.")
        else:
            existing = ScoreRule(
                competition_id=comp_id,
                checkpoint_id=checkpoint_id,
                group_id=group_id,
                rules=rules_json,
            )
            db.session.add(existing)
            db_msg = _("Score rule created.")

        db.session.commit()
        flash(db_msg, "success")
        try:
            recompute_scores_for_rule(comp_id, checkpoint_id, group_id)
        except Exception:
            pass
        return redirect(url_for("scores.score_rules"))

    return render_template(
        "score_rules.html",
        checkpoints=checkpoints,
        groups=groups,
        rules=rules,
        global_rules=global_rules,
    )


@scores_bp.route("/rules/<int:rule_id>/delete", methods=["POST"])
@roles_required("admin")
def delete_score_rule(rule_id: int):
    comp_id = get_current_competition_id()
    if not comp_id:
        flash(_("Select a competition first."), "warning")
        return redirect(url_for("main.select_competition"))
    rule = ScoreRule.query.filter(ScoreRule.competition_id == comp_id, ScoreRule.id == rule_id).first()
    if not rule:
        flash(_("Score rule not found."), "warning")
        return redirect(url_for("scores.score_rules"))

    db.session.delete(rule)
    db.session.commit()
    flash(_("Score rule deleted."), "success")
    return redirect(url_for("scores.score_rules"))


@scores_bp.route("/global-rules", methods=["POST"])
@roles_required("admin")
def save_global_rules():
    comp_id = get_current_competition_id()
    if not comp_id:
        flash(_("Select a competition first."), "warning")
        return redirect(url_for("main.select_competition"))

    group_id = request.form.get("global_group_id", type=int)
    found_enabled = bool(request.form.get("global_found_enabled"))
    found_points = request.form.get("global_found_points", type=float)
    found_exclude_start = bool(request.form.get("global_found_exclude_start"))
    found_exclude_end = bool(request.form.get("global_found_exclude_end"))
    time_enabled = bool(request.form.get("global_time_enabled"))
    time_start = request.form.get("global_time_start_checkpoint_id", type=int)
    time_end = request.form.get("global_time_end_checkpoint_id", type=int)
    time_max = request.form.get("global_time_max_points", type=float)
    time_threshold = request.form.get("global_time_threshold", type=float)
    time_penalty_minutes = request.form.get("global_time_penalty_minutes", type=float)
    time_penalty_points = request.form.get("global_time_penalty_points", type=float)
    time_min = request.form.get("global_time_min_points", type=float)
    time_dq_multiplier = request.form.get("global_time_dq_multiplier", type=float)

    if not group_id:
        flash(_("Group is required for global rules."), "warning")
        return redirect(url_for("scores.score_rules"))

    rules = {}
    if found_enabled:
        rules["found"] = {
            "points_per": found_points or 0,
            "exclude_start_checkpoint": found_exclude_start,
            "exclude_end_checkpoint": found_exclude_end,
        }
    if time_enabled:
        if not time_start or not time_end:
            flash(_("Global time rule requires start and end checkpoints."), "warning")
            return redirect(url_for("scores.score_rules"))
        time_config = {
            "start_checkpoint_id": time_start,
            "end_checkpoint_id": time_end,
            "max_points": time_max or 0,
            "threshold_minutes": time_threshold or 0,
            "penalty_minutes": time_penalty_minutes or 1,
            "penalty_points": time_penalty_points or 0,
            "min_points": time_min or 0,
        }
        if time_dq_multiplier is not None and time_dq_multiplier > 0:
            time_config["dq_multiplier"] = time_dq_multiplier
        rules["time"] = time_config
    if not rules:
        flash(_("Select at least one global rule."), "warning")
        return redirect(url_for("scores.score_rules"))

    record = GlobalScoreRule.query.filter(
        GlobalScoreRule.competition_id == comp_id,
        GlobalScoreRule.group_id == group_id,
    ).first()
    if record:
        record.rules = rules
        db_msg = _("Global rules updated.")
    else:
        record = GlobalScoreRule(
            competition_id=comp_id,
            group_id=group_id,
            rules=rules,
        )
        db.session.add(record)
        db_msg = _("Global rules created.")

    db.session.commit()
    flash(db_msg, "success")
    return redirect(url_for("scores.score_rules"))


@scores_bp.route("/global-rules/<int:rule_id>/delete", methods=["POST"])
@roles_required("admin")
def delete_global_rule(rule_id: int):
    comp_id = get_current_competition_id()
    if not comp_id:
        flash(_("Select a competition first."), "warning")
        return redirect(url_for("main.select_competition"))
    rule = GlobalScoreRule.query.filter(
        GlobalScoreRule.competition_id == comp_id,
        GlobalScoreRule.id == rule_id,
    ).first()
    if not rule:
        flash(_("Global rule not found."), "warning")
        return redirect(url_for("scores.score_rules"))

    db.session.delete(rule)
    db.session.commit()
    flash(_("Global rule deleted."), "success")
    return redirect(url_for("scores.score_rules"))


def _build_scores_context(comp_id: int, group_id: int | None) -> dict:
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
        unique_group_ids = sorted({gid for gid in team_group_ids.values() if gid})
        group_links = (
            CheckpointGroupLink.query.filter(CheckpointGroupLink.group_id.in_(unique_group_ids))
            .order_by(
                CheckpointGroupLink.group_id.asc(),
                CheckpointGroupLink.position.asc().nulls_last(),
                CheckpointGroupLink.checkpoint_id.asc(),
            )
            .all()
        )
        for link in group_links:
            group_checkpoint_ids.setdefault(link.group_id, set()).add(link.checkpoint_id)
            group_checkpoint_order.setdefault(link.group_id, []).append(link.checkpoint_id)
            if link.checkpoint_id:
                group_final_checkpoint[link.group_id] = link.checkpoint_id
                if link.group_id not in group_start_checkpoint:
                    group_start_checkpoint[link.group_id] = link.checkpoint_id

    # Load global rules early so the per-group start/end CP overrides apply
    # to time_minutes too — mirroring live_arrivals.py:78-85. Without this
    # the scores view computes elapsed time against the link-order first/last
    # checkpoint, which is wrong whenever operators configure a different
    # start/finish CP via the global time rule form.
    global_rules = GlobalScoreRule.query.filter(GlobalScoreRule.competition_id == comp_id).all()
    global_rules_map = {rule.group_id: rule.rules for rule in global_rules}
    # Whole-race time (Start → Cilj) start/end overrides — apply to
    # team_time_minutes display. This is the existing GlobalScoreRule.time
    # field and is NOT the time-trial leg (which is a separate config
    # below).
    for rule in global_rules:
        time_rule = (rule.rules or {}).get("time") or {}
        try:
            start_override = int(time_rule.get("start_checkpoint_id")) if time_rule.get("start_checkpoint_id") else None
            end_override = int(time_rule.get("end_checkpoint_id")) if time_rule.get("end_checkpoint_id") else None
        except (TypeError, ValueError):
            start_override = end_override = None
        if start_override:
            group_start_checkpoint[rule.group_id] = start_override
        if end_override:
            group_final_checkpoint[rule.group_id] = end_override

    # Per-group TIME-TRIAL LEG metadata, sourced from the existing
    # ScoreRule.time_race configuration on /scores/rules. Each time_race
    # rule defines a leg between two chosen CPs (the rank-based scoring
    # is applied at rule.checkpoint_id with start/end CPs in the rules
    # JSON). The leaderboard surfaces this as a single "Time-trial leg"
    # column with the leg's points + time taken, and hides the leg
    # endpoints from the per-CP iteration so we don't render "B: 0".
    group_leg_info: dict[int, dict] = {}
    time_race_rules_for_leg = ScoreRule.query.filter(ScoreRule.competition_id == comp_id).all()
    leg_cp_ids_all: set[int] = set()
    for rule in time_race_rules_for_leg:
        tr = (rule.rules or {}).get("time_race") or {}
        try:
            leg_start = int(tr.get("start_checkpoint_id")) if tr.get("start_checkpoint_id") else None
            leg_end = int(tr.get("end_checkpoint_id")) if tr.get("end_checkpoint_id") else None
        except (TypeError, ValueError):
            leg_start = leg_end = None
        if not (leg_start and leg_end):
            continue
        # First time_race rule per group wins for the leg display. If
        # operators ever configure two legs for the same group we still
        # score both via per_team_points; the display shows the first.
        if rule.group_id in group_leg_info:
            continue
        group_leg_info[rule.group_id] = {
            "start_cp_id": leg_start,
            "end_cp_id": leg_end,
            "scoring_cp_id": rule.checkpoint_id,
        }
        leg_cp_ids_all.update({leg_start, leg_end})
    if leg_cp_ids_all:
        leg_cp_names = {
            cp.id: cp.name
            for cp in Checkpoint.query.filter(Checkpoint.id.in_(leg_cp_ids_all)).all()
        }
        for leg in group_leg_info.values():
            start_name = leg_cp_names.get(leg["start_cp_id"], "?")
            end_name = leg_cp_names.get(leg["end_cp_id"], "?")
            leg["label"] = f"{start_name}→{end_name}"
            leg["cp_ids"] = frozenset({leg["start_cp_id"], leg["end_cp_id"]})

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
        dead_num = None
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

    # Live time_race scoring: rank-based ScoreRule.time_race rules compute
    # points from the spread between fastest and slowest finished team. The
    # original code path only ran via recompute_scores_for_rule (triggered by
    # judge form submit or rule edit), so LoRa auto-arrivals never made the
    # points appear. Recomputing on every render keeps the leaderboard live
    # as teams finish — same authoritative source the offline recompute uses.
    time_race_rules = ScoreRule.query.filter(ScoreRule.competition_id == comp_id).all()
    for rule in time_race_rules:
        tr = (rule.rules or {}).get("time_race") or {}
        start_cp = tr.get("start_checkpoint_id")
        end_cp = tr.get("end_checkpoint_id")
        if not start_cp or not end_cp:
            continue
        try:
            start_cp_i = int(start_cp)
            end_cp_i = int(end_cp)
        except (TypeError, ValueError):
            continue
        max_points = float(tr.get("max_points") or 0)
        min_points = float(tr.get("min_points") or 0)
        group_team_ids = [tid for tid in team_ids if team_group_ids.get(tid) == rule.group_id]
        if not group_team_ids:
            continue
        live_scores = _compute_time_race_scores_from_checkins(
            group_team_ids,
            comp_id,
            start_cp_i,
            end_cp_i,
            min_points,
            max_points,
        )
        cp_id = rule.checkpoint_id
        for team_id, new_total in live_scores.items():
            # Live rank-based value supersedes any ScoreEntry total from the
            # offline recompute — subtract the stale contribution from totals
            # before writing the fresh one so we never double-count.
            old = per_team_points.get(team_id, {}).get(cp_id)
            if old is not None:
                totals[team_id] = totals.get(team_id, 0.0) - float(old)
            per_team_points.setdefault(team_id, {})[cp_id] = new_total
            totals[team_id] = totals.get(team_id, 0.0) + float(new_total)

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

    # Per-team leg elapsed minutes. Points come from per_team_points
    # (populated by the live time_race block below — same source as the
    # offline recompute), so we don't need to recompute scoring here.
    leg_minutes_by_team: dict[int, float] = {}
    # Raw arrival timestamps at the leg endpoints, kept even when only one
    # endpoint has a check-in so the display can show a partial leg
    # ("A 10:03; B —") while the team is still on course.
    leg_times_by_team: dict[int, tuple[datetime | None, datetime | None]] = {}
    if group_leg_info:
        leg_cp_ids_query = {
            cid
            for leg in group_leg_info.values()
            for cid in (leg["start_cp_id"], leg["end_cp_id"])
        }
        leg_checkins = (
            Checkin.query.filter(Checkin.competition_id == comp_id)
            .filter(Checkin.team_id.in_(team_ids))
            .filter(Checkin.checkpoint_id.in_(leg_cp_ids_query))
            .order_by(Checkin.timestamp.asc())
            .all()
        )
        leg_cp_times: dict[int, dict[int, datetime]] = {tid: {} for tid in team_ids}
        for c in leg_checkins:
            if c.checkpoint_id not in leg_cp_times.get(c.team_id, {}):
                leg_cp_times.setdefault(c.team_id, {})[c.checkpoint_id] = c.timestamp
        for gid, leg in group_leg_info.items():
            group_team_ids = [tid for tid in team_ids if team_group_ids.get(tid) == gid]
            for tid in group_team_ids:
                start_ts = leg_cp_times.get(tid, {}).get(leg["start_cp_id"])
                end_ts = leg_cp_times.get(tid, {}).get(leg["end_cp_id"])
                if start_ts or end_ts:
                    leg_times_by_team[tid] = (start_ts, end_ts)
                if start_ts and end_ts and end_ts >= start_ts:
                    leg_minutes_by_team[tid] = (end_ts - start_ts).total_seconds() / 60.0

    global_totals = {}
    global_time_points = {}
    global_found_points = {}
    excluded_checkpoints_by_team: dict[int, set[int]] = {team_id: set() for team_id in team_ids}
    for team_id in team_ids:
        group_id_for_team = team_group_ids.get(team_id)
        if not group_id_for_team:
            global_totals[team_id] = 0.0
            global_time_points[team_id] = 0.0
            global_found_points[team_id] = 0.0
            continue
        global_rule = global_rules_map.get(group_id_for_team)
        global_data = _compute_global_contrib(comp_id, team_id, group_id_for_team, global_rule)
        global_totals[team_id] = global_data["total"] or 0.0
        global_time_points[team_id] = global_data["time_points"] or 0.0
        global_found_points[team_id] = global_data["found_points"] or 0.0
        # Auto-DQ from timeline
        if global_data.get("auto_dnf"):
            team_obj = Team.query.get(team_id)
            if team_obj and not team_obj.dnf:
                team_obj.dnf = True
                db.session.commit()
        if global_rule:
            found_rule = global_rule.get("found") or {}
            time_rule = global_rule.get("time") or {}
            if found_rule.get("exclude_start_checkpoint") and time_rule.get("start_checkpoint_id"):
                try:
                    excluded_checkpoints_by_team[team_id].add(int(time_rule.get("start_checkpoint_id")))
                except Exception:
                    pass
            if found_rule.get("exclude_end_checkpoint") and time_rule.get("end_checkpoint_id"):
                try:
                    excluded_checkpoints_by_team[team_id].add(int(time_rule.get("end_checkpoint_id")))
                except Exception:
                    pass

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
        leg_for_team = group_leg_info.get(team_group_id) if team_group_id else None
        raw_min = team_time_minutes.get(team.id)
        team_obj = next((t for t in teams if t.id == team.id), None)
        # Leg points already flow into `totals` via per_team_points (the
        # live time_race block puts them in per_team_points[team][rule.cp]
        # and we sum that into totals). Pull the same value back here for
        # the leg-display column so the cell can show points + time.
        leg_points = None
        leg_minutes = leg_minutes_by_team.get(team.id)
        leg_start_ts, leg_end_ts = leg_times_by_team.get(team.id, (None, None))
        if leg_for_team and leg_for_team.get("scoring_cp_id"):
            leg_points = per_team_points.get(team.id, {}).get(leg_for_team["scoring_cp_id"])
        team_total = totals.get(team.id, 0.0) + global_totals.get(team.id, 0.0)
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
                "leg_label": leg_for_team.get("label") if leg_for_team else "",
                "leg_cp_ids": leg_for_team.get("cp_ids") if leg_for_team else frozenset(),
                "leg_minutes": leg_minutes,
                "leg_points": leg_points,
                "leg_start_at": leg_start_ts,
                "leg_end_at": leg_end_ts,
                "leg_start_hms": format_time_display(leg_start_ts),
                "leg_end_hms": format_time_display(leg_end_ts),
                "dnf": bool(team.dnf),
                "finished": finished_map.get(team.id, False),
                "organization": team.organization or "",
                "allowed_checkpoints": allowed_checkpoint_ids.get(team.id, set()),
                "excluded_checkpoints": excluded_checkpoints_by_team.get(team.id, set()),
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
        group_cp_ids = (
            db.session.query(CheckpointGroupLink.checkpoint_id).filter(CheckpointGroupLink.group_id == group_id).all()
        )
        cp_ids = [row[0] for row in group_cp_ids]
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
    context = _build_scores_context(comp_id, group_id)
    context["show_actions"] = True
    return render_template("scores_view.html", **context)


@scores_bp.route("/view/export.csv", methods=["GET"])
@roles_required("judge", "admin")
def view_scores_export_csv():
    """Same data as /scores/view's extended (per-CP) table, flattened
    to CSV. Honors ?group_id=. Columns mirror the on-screen extended
    table: rank/group/number/team/finished/time_minutes, one column per
    checkpoint, the time-trial leg (label, start/end arrival, minutes,
    points), then time_points/found_points/dead_time/total. Use this
    as the canonical export to recreate the leaderboard in Sheets when
    sheets sync is unavailable."""
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
    header += [
        "time_trial",
        "time_trial_start",
        "time_trial_end",
        "time_trial_minutes",
        "time_trial_points",
    ]
    header += ["time_points", "found_points", "dead_time", "total"]
    w.writerow(header)

    for i, r in enumerate(rows, 1):
        team_id = r.get("id")
        team_cp_points = per_team_points.get(team_id, {}) if team_id else {}
        allowed = r.get("allowed_checkpoints") or set()
        excluded = r.get("excluded_checkpoints") or set()
        leg_cp_ids = r.get("leg_cp_ids") or frozenset()
        per_cp_cells = []
        for cp in checkpoints:
            val = team_cp_points.get(cp.id)
            if r.get("dnf"):
                per_cp_cells.append("DNF")
            elif cp.id in leg_cp_ids:
                # Leg endpoints are consolidated into the time_trial_*
                # columns, same as the on-screen extended table.
                per_cp_cells.append("")
            elif cp.id in excluded:
                per_cp_cells.append("")
            elif val is not None:
                per_cp_cells.append(fmt(val, ".2f"))
            elif cp.id in allowed:
                per_cp_cells.append("0")
            else:
                per_cp_cells.append("")
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
                r.get("leg_label", ""),
                format_datetime_display(r.get("leg_start_at")),
                format_datetime_display(r.get("leg_end_at")),
                fmt(r.get("leg_minutes"), ".2f"),
                fmt(r.get("leg_points"), ".2f"),
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

    links = (
        CheckpointGroupLink.query.filter(CheckpointGroupLink.group_id.in_([g.id for g in groups]))
        .order_by(CheckpointGroupLink.group_id.asc(), CheckpointGroupLink.position.asc().nulls_last())
        .all()
    )
    checkpoint_order_by_group = {}
    for link in links:
        checkpoint_order_by_group.setdefault(link.group_id, []).append(link.checkpoint_id)

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
                from_cp = next((cp for cp in group.checkpoint_links if cp.checkpoint_id == from_id), None)
                to_cp = next((cp for cp in group.checkpoint_links if cp.checkpoint_id == to_id), None)
                if from_count > 0:
                    rate = max(0.0, (from_count - to_count) / from_count)
                    if dropoff_rate is None or rate > dropoff_rate:
                        dropoff_rate = rate
                        dropoff_checkpoint = to_cp.checkpoint.name if to_cp and to_cp.checkpoint else ""
                segments.append(
                    {
                        "from_id": from_id,
                        "to_id": to_id,
                        "from_name": from_cp.checkpoint.name if from_cp and from_cp.checkpoint else "",
                        "to_name": to_cp.checkpoint.name if to_cp and to_cp.checkpoint else "",
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
