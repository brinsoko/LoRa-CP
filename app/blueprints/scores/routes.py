# app/blueprints/scores/routes.py
from __future__ import annotations

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_babel import gettext as _
import json

from app.utils.perms import roles_required
from app.utils.competition import get_current_competition_id, get_current_competition_role
from app.resources.scores import recompute_scores_for_rule, _compute_global_contrib
from app.models import (
    JudgeCheckpoint,
    Checkpoint,
    ScoreRule,
    GlobalScoreRule,
    CheckpointGroup,
    CheckpointGroupLink,
    Team,
    TeamGroup,
    ScoreEntry,
    Checkin,
    Competition,
)
from app.extensions import db
from flask_login import current_user

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
                Checkpoint.query
                .filter(Checkpoint.competition_id == comp_id)
                .order_by(Checkpoint.name.asc())
                .all()
            )
            default_checkpoint_id = checkpoints[0].id if checkpoints else None
        else:
            assigned = (
                JudgeCheckpoint.query
                .join(Checkpoint, JudgeCheckpoint.checkpoint_id == Checkpoint.id)
                .filter(
                    JudgeCheckpoint.user_id == current_user.id,
                    Checkpoint.competition_id == comp_id,
                )
                .order_by(Checkpoint.name.asc())
                .all()
            )
            checkpoints = [jc.checkpoint for jc in assigned if jc.checkpoint]
            default_row = next((jc for jc in assigned if jc.is_default), None)
            default_checkpoint_id = default_row.checkpoint_id if default_row else None
        teams = (
            Team.query
            .filter(Team.competition_id == comp_id)
            .order_by(Team.name.asc())
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
        Checkpoint.query
        .filter(Checkpoint.competition_id == comp_id)
        .order_by(Checkpoint.name.asc())
        .all()
    )
    groups = (
        CheckpointGroup.query
        .filter(CheckpointGroup.competition_id == comp_id)
        .order_by(CheckpointGroup.position.asc().nulls_last(), CheckpointGroup.name.asc())
        .all()
    )
    rules = (
        ScoreRule.query
        .filter(ScoreRule.competition_id == comp_id)
        .order_by(ScoreRule.created_at.desc())
        .all()
    )
    global_rules = (
        GlobalScoreRule.query
        .filter(GlobalScoreRule.competition_id == comp_id)
        .order_by(GlobalScoreRule.created_at.desc())
        .all()
    )

    if request.method == "POST":
        checkpoint_id = request.form.get("checkpoint_id", type=int)
        group_id = request.form.get("group_id", type=int)
        rules_text = (request.form.get("rules_json") or "").strip()
        if not checkpoint_id or not group_id or not rules_text:
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
        existing = (
            ScoreRule.query
            .filter(
                ScoreRule.competition_id == comp_id,
                ScoreRule.checkpoint_id == checkpoint_id,
                ScoreRule.group_id == group_id,
            )
            .first()
        )
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
        rules["time"] = {
            "start_checkpoint_id": time_start,
            "end_checkpoint_id": time_end,
            "max_points": time_max or 0,
            "threshold_minutes": time_threshold or 0,
            "penalty_minutes": time_penalty_minutes or 1,
            "penalty_points": time_penalty_points or 0,
            "min_points": time_min or 0,
        }
    if not rules:
        flash(_("Select at least one global rule."), "warning")
        return redirect(url_for("scores.score_rules"))

    record = (
        GlobalScoreRule.query
        .filter(
            GlobalScoreRule.competition_id == comp_id,
            GlobalScoreRule.group_id == group_id,
        )
        .first()
    )
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
        CheckpointGroup.query
        .filter(CheckpointGroup.competition_id == comp_id)
        .order_by(CheckpointGroup.position.asc().nulls_last(), CheckpointGroup.name.asc())
        .all()
    )
    group_order = [g.name for g in groups if g.name]
    teams_query = Team.query.filter(Team.competition_id == comp_id)
    if group_id:
        teams_query = (
            teams_query
            .join(TeamGroup, TeamGroup.team_id == Team.id)
            .filter(TeamGroup.group_id == group_id, TeamGroup.active.is_(True))
        )
    teams = teams_query.order_by(Team.number.asc().nulls_last(), Team.name.asc()).all()
    team_ids = [t.id for t in teams]

    entries = []
    if team_ids:
        entries = (
            ScoreEntry.query
            .filter(ScoreEntry.competition_id == comp_id)
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
        links = (
            TeamGroup.query
            .filter(TeamGroup.team_id.in_(team_ids), TeamGroup.active.is_(True))
            .all()
        )
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
            CheckpointGroupLink.query
            .filter(CheckpointGroupLink.group_id.in_(unique_group_ids))
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

    team_time_minutes: dict[int, float | None] = {team_id: None for team_id in team_ids}
    if team_ids and group_start_checkpoint and group_final_checkpoint:
        start_ids = {cid for cid in group_start_checkpoint.values() if cid}
        end_ids = {cid for cid in group_final_checkpoint.values() if cid}
        checkins = (
            Checkin.query
            .filter(Checkin.competition_id == comp_id)
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

    global_rules = (
        GlobalScoreRule.query
        .filter(GlobalScoreRule.competition_id == comp_id)
        .all()
    )
    global_rules_map = {rule.group_id: rule.rules for rule in global_rules}
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
        rows.append({
            "id": team.id,
            "name": team.name,
            "number": team.number,
            "group": team_groups.get(team.id, ""),
            "total": (totals.get(team.id, 0.0) + global_totals.get(team.id, 0.0)),
            "dead_time": dead_times.get(team.id, 0.0),
            "global_time": global_time_points.get(team.id, 0.0),
            "global_found": global_found_points.get(team.id, 0.0),
            "time_minutes": team_time_minutes.get(team.id),
            "dnf": bool(team.dnf),
            "finished": finished_map.get(team.id, False),
            "organization": team.organization or "",
            "allowed_checkpoints": allowed_checkpoint_ids.get(team.id, set()),
            "excluded_checkpoints": excluded_checkpoints_by_team.get(team.id, set()),
        })

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

    group_totals_map = {}
    for row in rows:
        group_name = (row.get("group") or "").strip()
        if not group_name or row.get("dnf"):
            continue
        group_totals_map[group_name] = group_totals_map.get(group_name, 0.0) + float(row.get("total") or 0.0)
    def _group_rank_sort(item):
        name, total = item
        norm = name.lower().strip()
        order_idx = group_order_norm.index(norm) if norm in group_order_norm else len(group_order_norm)
        return (-total, order_idx, name)
    group_totals_sorted = sorted(group_totals_map.items(), key=_group_rank_sort)
    group_ranks = {name: idx + 1 for idx, (name, _total) in enumerate(group_totals_sorted)}
    group_totals = [{"name": name, "total": total, "rank": group_ranks.get(name)} for name, total in group_totals_sorted]

    org_totals_map = {}
    for row in rows:
        org = (row.get("organization") or "").strip()
        if not org or row.get("dnf"):
            continue
        org_totals_map[org] = org_totals_map.get(org, 0.0) + float(row.get("total") or 0.0)
    org_totals = [{"name": name, "total": total} for name, total in sorted(org_totals_map.items())]

    checkpoints_query = Checkpoint.query.filter(Checkpoint.competition_id == comp_id)
    if group_id:
        group_cp_ids = (
            db.session.query(CheckpointGroupLink.checkpoint_id)
            .filter(CheckpointGroupLink.group_id == group_id)
            .all()
        )
        cp_ids = [row[0] for row in group_cp_ids]
        if cp_ids:
            checkpoints_query = checkpoints_query.filter(Checkpoint.id.in_(cp_ids))
    checkpoints = checkpoints_query.order_by(Checkpoint.name.asc()).all()

    return {
        "rows": rows,
        "groups": groups,
        "selected_group_id": group_id,
        "checkpoints": checkpoints,
        "per_team_points": per_team_points,
        "org_totals": org_totals,
        "allowed_checkpoint_ids": allowed_checkpoint_ids,
        "group_totals": group_totals,
        "group_ranks": group_ranks,
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
        CheckpointGroup.query
        .filter(CheckpointGroup.competition_id == comp_id)
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
        TeamGroup.query
        .join(Team, TeamGroup.team_id == Team.id)
        .filter(Team.competition_id == comp_id, TeamGroup.active.is_(True))
        .all()
    )
    teams_by_group_id = {}
    for link in team_groups:
        teams_by_group_id.setdefault(link.group_id, set()).add(link.team_id)

    links = (
        CheckpointGroupLink.query
        .filter(CheckpointGroupLink.group_id.in_([g.id for g in groups]))
        .order_by(CheckpointGroupLink.group_id.asc(), CheckpointGroupLink.position.asc().nulls_last())
        .all()
    )
    checkpoint_order_by_group = {}
    for link in links:
        checkpoint_order_by_group.setdefault(link.group_id, []).append(link.checkpoint_id)

    stats = []
    for group in groups:
        team_ids = sorted(teams_by_group_id.get(group.id, set()))
        if not team_ids:
            stats.append({
                "id": group.id,
                "name": group.name,
                "team_count": 0,
                "finished_count": 0,
                "completion_rate": 0,
                "avg_points": None,
                "avg_time_minutes": None,
                "fastest_team": None,
                "fastest_minutes": None,
                "avg_checkpoint_count": None,
                "segments": [],
            })
            continue

        group_rows = rows_by_group.get(group.name, [])
        scored_rows = [r for r in group_rows if not r.get("dnf")]
        avg_points = None
        if scored_rows:
            avg_points = sum(float(r.get("total") or 0.0) for r in scored_rows) / len(scored_rows)

        finished_count = sum(1 for r in group_rows if r.get("finished"))
        completion_rate = (finished_count / len(group_rows)) if group_rows else 0

        cp_ids = checkpoint_order_by_group.get(group.id, [])
        checkins = []
        if cp_ids:
            checkins = (
                Checkin.query
                .filter(Checkin.competition_id == comp_id)
                .filter(Checkin.team_id.in_(team_ids))
                .filter(Checkin.checkpoint_id.in_(cp_ids))
                .order_by(Checkin.timestamp.asc())
                .all()
            )

        team_cp_times = {tid: {} for tid in team_ids}
        for c in checkins:
            if c.checkpoint_id not in team_cp_times.get(c.team_id, {}):
                team_cp_times.setdefault(c.team_id, {})[c.checkpoint_id] = c.timestamp

        avg_checkpoint_count = None
        if cp_ids:
            counts = []
            for tid in team_ids:
                counts.append(len(team_cp_times.get(tid, {})))
            avg_checkpoint_count = sum(counts) / len(counts) if counts else None

        avg_time_minutes = None
        median_time_minutes = None
        fastest_team = None
        fastest_minutes = None
        dropoff_checkpoint = None
        dropoff_rate = None
        if len(cp_ids) >= 2:
            start_id = cp_ids[0]
            end_id = cp_ids[-1]
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

        segments = []
        if len(cp_ids) >= 2:
            for idx in range(len(cp_ids) - 1):
                from_id = cp_ids[idx]
                to_id = cp_ids[idx + 1]
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
                segments.append({
                    "from_id": from_id,
                    "to_id": to_id,
                    "from_name": from_cp.checkpoint.name if from_cp and from_cp.checkpoint else "",
                    "to_name": to_cp.checkpoint.name if to_cp and to_cp.checkpoint else "",
                    "avg_minutes": avg_segment,
                    "sample_count": len(segment_durations),
                })

        stats.append({
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
        })

    chart_groups = [g["name"] for g in stats if g["team_count"]]
    chart_points = [g["avg_points"] or 0 for g in stats if g["team_count"]]
    chart_times = [g["avg_time_minutes"] or 0 for g in stats if g["team_count"]]

    return {
        "groups": stats,
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
        teams_query = (
            teams_query
            .join(TeamGroup, TeamGroup.team_id == Team.id)
            .filter(TeamGroup.group_id == group_id, TeamGroup.active.is_(True))
        )
    teams = teams_query.order_by(Team.number.asc().nulls_last(), Team.name.asc()).all()

    checkpoints = (
        Checkpoint.query
        .filter(Checkpoint.competition_id == comp_id)
        .order_by(Checkpoint.name.asc())
        .all()
    )
    groups = (
        CheckpointGroup.query
        .filter(CheckpointGroup.competition_id == comp_id)
        .order_by(CheckpointGroup.position.asc().nulls_last(), CheckpointGroup.name.asc())
        .all()
    )

    query = (
        ScoreEntry.query
        .filter(ScoreEntry.competition_id == comp_id)
        .order_by(ScoreEntry.created_at.desc())
    )
    if team_id:
        query = query.filter(ScoreEntry.team_id == team_id)
    if checkpoint_id:
        query = query.filter(ScoreEntry.checkpoint_id == checkpoint_id)
    entries = query.limit(300).all()

    entry_team_ids = sorted({e.team_id for e in entries if e.team_id})
    team_group_map = {}
    if entry_team_ids:
        links = (
            TeamGroup.query
            .filter(TeamGroup.team_id.in_(entry_team_ids), TeamGroup.active.is_(True))
            .all()
        )
        for link in links:
            if link.team_id not in team_group_map:
                team_group_map[link.team_id] = link.group_id

    global_rules = (
        GlobalScoreRule.query
        .filter(GlobalScoreRule.competition_id == comp_id)
        .all()
    )
    global_rules_map = {rule.group_id: rule.rules for rule in global_rules}
    team_time_points: dict[int, float | None] = {}
    for team_id in entry_team_ids:
        group_id_for_team = team_group_map.get(team_id)
        if not group_id_for_team:
            team_time_points[team_id] = None
            continue
        global_rule = global_rules_map.get(group_id_for_team)
        global_data = _compute_global_contrib(comp_id, team_id, group_id_for_team, global_rule)
        team_time_points[team_id] = global_data.get("time_points")

    rows = []
    for entry in entries:
        raw = entry.raw_fields or {}
        dead_val = raw.get("dead_time", raw.get("Dead Time"))
        dead_num = None
        try:
            dead_num = float(dead_val)
        except Exception:
            dead_num = None
        rows.append({
            "id": entry.id,
            "team": entry.team.name if entry.team else "",
            "team_id": entry.team_id,
            "checkpoint": entry.checkpoint.name if entry.checkpoint else "",
            "checkpoint_id": entry.checkpoint_id,
            "created_at": entry.created_at,
            "total": entry.total,
            "dead_time": dead_num,
            "time_points": team_time_points.get(entry.team_id),
            "raw_fields": raw,
        })

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
