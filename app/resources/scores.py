# app/resources/scores.py
from __future__ import annotations

from datetime import datetime

from flask import request
from flask_login import current_user
from flask_restful import Resource
from app.extensions import db
from app.models import (
    Checkin,
    Checkpoint,
    CompetitionMember,
    JudgeCheckpoint,
    RFIDCard,
    ScoreEntry,
    ScoreRule,
    GlobalScoreRule,
    Team,
    TeamGroup,
)
from app.utils.competition import require_current_competition_id
from app.utils.rest_auth import json_roles_required
from app.utils.sheets_sync import mark_arrival_checkbox, update_checkpoint_scores
from app.utils.card_tokens import compute_card_digest


def _get_active_group(team_id: int) -> TeamGroup | None:
    return (
        TeamGroup.query
        .filter(TeamGroup.team_id == team_id, TeamGroup.active.is_(True))
        .first()
    )


def _get_checkpoint_fields(competition_id: int, checkpoint_id: int, group_name: str) -> dict:
    from app.models import SheetConfig

    cfg = (
        SheetConfig.query
        .filter(
            SheetConfig.competition_id == competition_id,
            SheetConfig.checkpoint_id == checkpoint_id,
            SheetConfig.tab_type == "checkpoint",
        )
        .order_by(SheetConfig.created_at.desc())
        .first()
    )
    if not cfg:
        return {"fields": [], "headers": {}, "config": None}

    headers = {
        "dead_time": (cfg.config or {}).get("dead_time_header"),
        "time": (cfg.config or {}).get("time_header"),
        "points": (cfg.config or {}).get("points_header"),
    }
    group_defs = (cfg.config or {}).get("groups", [])
    group_def = next(
        (g for g in group_defs if (g.get("name") or "").strip().lower() == (group_name or "").strip().lower()),
        None,
    )
    fields = list(group_def.get("fields") or []) if group_def else []
    return {"fields": fields, "headers": headers, "config": cfg.config or {}}


def _get_score_rule(competition_id: int, checkpoint_id: int, group_id: int) -> dict | None:
    rule = (
        ScoreRule.query
        .filter(
            ScoreRule.competition_id == competition_id,
            ScoreRule.checkpoint_id == checkpoint_id,
            ScoreRule.group_id == group_id,
        )
        .first()
    )
    return rule.rules if rule else None


def _get_global_score_rule(competition_id: int, group_id: int) -> dict | None:
    rule = (
        GlobalScoreRule.query
        .filter(
            GlobalScoreRule.competition_id == competition_id,
            GlobalScoreRule.group_id == group_id,
        )
        .first()
    )
    return rule.rules if rule else None


def _to_number(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _apply_field_rule(value, rule, context: dict) -> float | None:
    if isinstance(rule, list):
        current = value
        for item in rule:
            current = _apply_field_rule(current, item, context)
        return _to_number(current) if current is not None else None
    if not isinstance(rule, dict):
        return _to_number(value)
    rule_type = (rule.get("type") or "").strip().lower()
    if rule_type == "mapping":
        mapping = rule.get("map") or {}
        return _to_number(mapping.get(str(value)))
    if rule_type == "interpolate":
        pts = rule.get("points") or []
        try:
            pts = sorted([(float(x), float(y)) for x, y in pts], key=lambda p: p[0])
        except Exception:
            return None
        x = _to_number(value)
        if x is None or not pts:
            return None
        if x <= pts[0][0]:
            return pts[0][1]
        if x >= pts[-1][0]:
            return pts[-1][1]
        for (x1, y1), (x2, y2) in zip(pts, pts[1:]):
            if x1 <= x <= x2:
                if x2 == x1:
                    return y1
                t = (x - x1) / (x2 - x1)
                return y1 + t * (y2 - y1)
        return None
    if rule_type == "multiplier":
        factor = _to_number(rule.get("factor"))
        base = _to_number(value)
        if factor is None or base is None:
            return None
        return base * factor
    if rule_type == "found":
        team_id = context.get("team_id")
        competition_id = context.get("competition_id")
        if not team_id or not competition_id:
            return None
        checkpoint_ids = []
        for val in rule.get("checkpoint_ids") or []:
            try:
                checkpoint_ids.append(int(val))
            except Exception:
                continue
        points_per = _to_number(rule.get("points_per"))
        if not checkpoint_ids or points_per is None:
            return None
        found = (
            Checkin.query
            .filter(
                Checkin.competition_id == competition_id,
                Checkin.team_id == team_id,
                Checkin.checkpoint_id.in_(checkpoint_ids),
            )
            .with_entities(Checkin.checkpoint_id)
            .distinct()
            .all()
        )
        count = len(found)
        return points_per * count
    if value is None or value == "":
        return None
    if rule_type == "deviation":
        base = _to_number(value)
        target = _to_number(rule.get("target"))
        max_points = _to_number(rule.get("max_points"))
        penalty_points = _to_number(rule.get("penalty_points"))
        penalty_distance = _to_number(rule.get("penalty_distance"))
        min_points = _to_number(rule.get("min_points"))
        if base is None or target is None or max_points is None or penalty_points is None or penalty_distance in (None, 0):
            return None
        distance = abs(base - target)
        penalty = (distance / penalty_distance) * penalty_points
        points = max_points - penalty
        if min_points is not None:
            points = max(points, min_points)
        return points
    return _to_number(value)


def _parse_time_to_seconds(value) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if ":" in text:
        parts = text.split(":")
        try:
            parts = [float(p) for p in parts]
        except Exception:
            return None
        while len(parts) < 3:
            parts.insert(0, 0.0)
        hours, minutes, seconds = parts[-3], parts[-2], parts[-1]
        return hours * 3600 + minutes * 60 + seconds
    try:
        return float(text)
    except Exception:
        return None


def _compute_global_contrib(competition_id: int, team_id: int, group_id: int, global_rule: dict | None) -> dict:
    if not global_rule:
        return {"total": None, "found_points": None, "time_points": None}

    total = 0.0
    used = False
    found_points = None
    time_points = None

    found_rule = global_rule.get("found") or {}
    points_per = _to_number(found_rule.get("points_per"))
    if points_per is not None:
        from app.models import CheckpointGroupLink
        cp_ids = (
            db.session.query(CheckpointGroupLink.checkpoint_id)
            .filter(CheckpointGroupLink.group_id == group_id)
            .all()
        )
        checkpoint_ids = [row[0] for row in cp_ids]
        time_rule = global_rule.get("time") or {}
        if found_rule.get("exclude_start_checkpoint") and time_rule.get("start_checkpoint_id"):
            try:
                checkpoint_ids.remove(int(time_rule.get("start_checkpoint_id")))
            except ValueError:
                pass
        if found_rule.get("exclude_end_checkpoint") and time_rule.get("end_checkpoint_id"):
            try:
                checkpoint_ids.remove(int(time_rule.get("end_checkpoint_id")))
            except ValueError:
                pass
        if checkpoint_ids:
            found = (
                Checkin.query
                .filter(
                    Checkin.competition_id == competition_id,
                    Checkin.team_id == team_id,
                    Checkin.checkpoint_id.in_(checkpoint_ids),
                )
                .with_entities(Checkin.checkpoint_id)
                .distinct()
                .count()
            )
            found_points = points_per * found
            total += found_points
            used = True

    time_rule = global_rule.get("time") or {}
    start_cp = time_rule.get("start_checkpoint_id")
    end_cp = time_rule.get("end_checkpoint_id")
    max_points = _to_number(time_rule.get("max_points"))
    threshold_minutes = _to_number(time_rule.get("threshold_minutes"))
    penalty_minutes = _to_number(time_rule.get("penalty_minutes"))
    penalty_points = _to_number(time_rule.get("penalty_points"))
    min_points = _to_number(time_rule.get("min_points")) or 0.0
    if start_cp and end_cp and max_points is not None and threshold_minutes is not None and penalty_minutes and penalty_points is not None:
        start_row = (
            Checkin.query
            .filter(
                Checkin.competition_id == competition_id,
                Checkin.team_id == team_id,
                Checkin.checkpoint_id == int(start_cp),
            )
            .order_by(Checkin.timestamp.asc())
            .first()
        )
        end_row = (
            Checkin.query
            .filter(
                Checkin.competition_id == competition_id,
                Checkin.team_id == team_id,
                Checkin.checkpoint_id == int(end_cp),
            )
            .order_by(Checkin.timestamp.asc())
            .first()
        )
        if start_row and end_row:
            duration_minutes = (end_row.timestamp - start_row.timestamp).total_seconds() / 60.0
            if duration_minutes <= threshold_minutes:
                time_points = max_points
            else:
                over = max(0.0, duration_minutes - threshold_minutes)
                time_points = max_points - (over / penalty_minutes) * penalty_points
            if time_points < min_points:
                time_points = min_points
            total += time_points
            used = True

    return {"total": (total if used else None), "found_points": found_points, "time_points": time_points}


def _compute_total(
    values: dict,
    points_header: str | None,
    rule: dict | None,
    context: dict,
) -> float | None:
    base_total = None
    if rule and rule.get("field_rules"):
        computed = {}
        for key, val in values.items():
            field_rule = rule["field_rules"].get(key)
            computed[key] = _apply_field_rule(val, field_rule, context) if field_rule else _to_number(val)
        total_fields = rule.get("total_fields") or list(computed.keys())
        total_fields = [key for key in total_fields if key != "dead_time"]
        total = 0.0
        used = False
        for key in total_fields:
            val = computed.get(key)
            if val is None:
                continue
            total += float(val)
            used = True
        base_total = total if used else None

    if points_header and points_header in values:
        base_total = _to_number(values.get(points_header))
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

    return base_total


def _compute_time_race_scores_from_checkins(
    team_ids: list[int],
    competition_id: int,
    start_checkpoint_id: int,
    end_checkpoint_id: int,
    min_points: float,
    max_points: float,
) -> dict[int, float]:
    from sqlalchemy import func

    if not team_ids:
        return {}

    start_rows = (
        db.session.query(Checkin.team_id, func.min(Checkin.timestamp))
        .filter(
            Checkin.competition_id == competition_id,
            Checkin.checkpoint_id == start_checkpoint_id,
            Checkin.team_id.in_(team_ids),
        )
        .group_by(Checkin.team_id)
        .all()
    )
    end_rows = (
        db.session.query(Checkin.team_id, func.min(Checkin.timestamp))
        .filter(
            Checkin.competition_id == competition_id,
            Checkin.checkpoint_id == end_checkpoint_id,
            Checkin.team_id.in_(team_ids),
        )
        .group_by(Checkin.team_id)
        .all()
    )

    start_map = {team_id: ts for team_id, ts in start_rows if ts}
    end_map = {team_id: ts for team_id, ts in end_rows if ts}

    durations = {}
    for team_id in team_ids:
        start_ts = start_map.get(team_id)
        end_ts = end_map.get(team_id)
        if not start_ts or not end_ts:
            continue
        duration = (end_ts - start_ts).total_seconds()
        if duration < 0:
            continue
        durations[team_id] = duration

    if not durations:
        return {}
    min_d = min(durations.values())
    max_d = max(durations.values())
    if max_d == min_d:
        return {team_id: max_points for team_id in durations.keys()}

    scores = {}
    for team_id, duration in durations.items():
        t = (duration - min_d) / (max_d - min_d)
        scores[team_id] = max_points - t * (max_points - min_points)
    return scores


def recompute_scores_for_rule(competition_id: int, checkpoint_id: int, group_id: int) -> None:
    from app.models import CheckpointGroup

    group = CheckpointGroup.query.filter(
        CheckpointGroup.competition_id == competition_id,
        CheckpointGroup.id == group_id,
    ).first()
    if not group:
        return
    group_name = group.name or ""
    rule = _get_score_rule(competition_id, checkpoint_id, group_id)

    latest_entries = (
        ScoreEntry.query
        .join(TeamGroup, TeamGroup.team_id == ScoreEntry.team_id)
        .filter(
            ScoreEntry.competition_id == competition_id,
            ScoreEntry.checkpoint_id == checkpoint_id,
            TeamGroup.group_id == group_id,
            TeamGroup.active.is_(True),
        )
        .order_by(ScoreEntry.created_at.desc())
        .all()
    )
    latest_by_team = {}
    for entry in latest_entries:
        if entry.team_id not in latest_by_team:
            latest_by_team[entry.team_id] = entry
    entries = list(latest_by_team.values())
    if not entries:
        return

    if rule and rule.get("time_race"):
        tr = rule["time_race"] or {}
        start_checkpoint_id = tr.get("start_checkpoint_id")
        end_checkpoint_id = tr.get("end_checkpoint_id")
        if start_checkpoint_id and end_checkpoint_id:
            min_points = _to_number(tr.get("min_points")) or 0.0
            max_points = _to_number(tr.get("max_points")) or 0.0
            team_ids = [entry.team_id for entry in entries]
            scores = _compute_time_race_scores_from_checkins(
                team_ids,
                competition_id,
                int(start_checkpoint_id),
                int(end_checkpoint_id),
                min_points,
                max_points,
            )
            for entry in entries:
                if entry.team_id in scores:
                    entry.total = scores[entry.team_id]
            db.session.commit()

            for entry in entries:
                if entry.team_id in scores:
                    try:
                        values = dict(entry.raw_fields or {})
                        values["points"] = scores[entry.team_id]
                        update_checkpoint_scores(entry.team_id, checkpoint_id, group_name, values, entry.created_at)
                    except Exception:
                        pass
            return

    for entry in entries:
        total = _compute_total(
            entry.raw_fields or {},
            None,
            rule,
            {"team_id": entry.team_id, "competition_id": competition_id, "group_id": group_id},
        )
        entry.total = total
        try:
            values = dict(entry.raw_fields or {})
            if total is not None:
                values["points"] = total
            update_checkpoint_scores(entry.team_id, checkpoint_id, group_name, values, entry.created_at)
        except Exception:
            pass
    db.session.commit()


class ScoreResolve(Resource):
    method_decorators = [json_roles_required("judge", "admin")]

    def post(self):
        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400

        payload = request.get_json(silent=True) or {}
        uid = (payload.get("uid") or "").strip().upper()
        team_id = payload.get("team_id")
        checkpoint_id = payload.get("checkpoint_id")
        try:
            checkpoint_id = int(checkpoint_id)
        except Exception:
            return {"error": "invalid_request", "detail": "checkpoint_id is required."}, 400

        checkpoint = Checkpoint.query.filter(
            Checkpoint.competition_id == comp_id,
            Checkpoint.id == checkpoint_id,
        ).first()
        if not checkpoint:
            return {"error": "not_found", "detail": "Checkpoint not found."}, 404

        # Enforce judge assignment
        if (CompetitionMember.query
            .filter(
                CompetitionMember.competition_id == comp_id,
                CompetitionMember.user_id == current_user.id,
                CompetitionMember.active.is_(True),
                CompetitionMember.role == "judge",
            )
            .first()):
            assigned = (
                JudgeCheckpoint.query
                .filter(
                    JudgeCheckpoint.user_id == current_user.id,
                    JudgeCheckpoint.checkpoint_id == checkpoint_id,
                )
                .first()
            )
            if not assigned:
                return {"error": "forbidden", "detail": "Checkpoint not assigned."}, 403

        team = None
        if uid:
            card = RFIDCard.query.filter_by(uid=uid).first()
            if not card:
                return {"error": "not_found", "detail": "Card not mapped to a team."}, 404
            team = Team.query.filter(Team.competition_id == comp_id, Team.id == card.team_id).first()
        else:
            try:
                team_id = int(team_id)
            except Exception:
                return {"error": "invalid_request", "detail": "uid or team_id is required."}, 400
            team = Team.query.filter(Team.competition_id == comp_id, Team.id == team_id).first()
        if not team:
            return {"error": "not_found", "detail": "Team not found."}, 404

        group_link = _get_active_group(team.id)
        group_name = group_link.group.name if group_link and group_link.group else ""
        if not group_name:
            return {"error": "invalid_request", "detail": "Team has no active group."}, 400
        group_id = group_link.group_id if group_link else None

        fields_info = _get_checkpoint_fields(comp_id, checkpoint_id, group_name)
        config = fields_info.get("config")
        if config is None:
            return {"error": "invalid_request", "detail": "No scoring fields configured for this checkpoint."}, 400
        if not fields_info["fields"] and not config.get("dead_time_enabled") and not config.get("time_enabled"):
            # Points-only is allowed; require at least the checkpoint config to exist.
            pass
        field_defs = []
        if config.get("dead_time_enabled"):
            field_defs.append({"key": "dead_time", "label": fields_info["headers"].get("dead_time") or "Dead Time", "type": "number"})
        for field in fields_info["fields"]:
            field_defs.append({"key": field, "label": field, "type": "number"})
        has_score_input = any(fd.get("key") in ("score", "points") for fd in field_defs)
        has_scored_fields = any(fd.get("key") not in ("dead_time",) for fd in field_defs)
        if not has_score_input and not has_scored_fields:
            field_defs.append({"key": "points", "label": "Score", "type": "number"})

        existing = (
            ScoreEntry.query
            .filter(
                ScoreEntry.competition_id == comp_id,
                ScoreEntry.team_id == team.id,
                ScoreEntry.checkpoint_id == checkpoint_id,
            )
            .order_by(ScoreEntry.created_at.desc())
            .first()
        )

        checkin_exists = (
            Checkin.query
            .filter_by(competition_id=comp_id, team_id=team.id, checkpoint_id=checkpoint_id)
            .first()
            is not None
        )

        rule = _get_score_rule(comp_id, checkpoint_id, group_id) if group_id else None

        return {
            "ok": True,
            "uid": uid or None,
            "team": {"id": team.id, "name": team.name, "number": team.number},
            "checkpoint": {"id": checkpoint.id, "name": checkpoint.name},
            "group": {"name": group_name},
            "fields": field_defs,
            "latest_score": existing.raw_fields if existing else None,
            "latest_total": existing.total if existing else None,
            "checkin_created": False,
            "checkin_exists": checkin_exists,
            "rules": rule,
        }, 200


class ScoreSubmit(Resource):
    method_decorators = [json_roles_required("judge", "admin")]

    def post(self):
        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400
        payload = request.get_json(silent=True) or {}
        team_id = payload.get("team_id")
        checkpoint_id = payload.get("checkpoint_id")
        fields = payload.get("fields") or {}
        uid = (payload.get("uid") or "").strip().upper()

        try:
            team_id = int(team_id)
            checkpoint_id = int(checkpoint_id)
        except Exception:
            return {"error": "invalid_request", "detail": "team_id and checkpoint_id are required."}, 400

        team = Team.query.filter(Team.competition_id == comp_id, Team.id == team_id).first()
        checkpoint = Checkpoint.query.filter(Checkpoint.competition_id == comp_id, Checkpoint.id == checkpoint_id).first()
        if not team or not checkpoint:
            return {"error": "invalid_request", "detail": "Invalid team or checkpoint."}, 400

        if (CompetitionMember.query
            .filter(
                CompetitionMember.competition_id == comp_id,
                CompetitionMember.user_id == current_user.id,
                CompetitionMember.active.is_(True),
                CompetitionMember.role == "judge",
            )
            .first()):
            assigned = (
                JudgeCheckpoint.query
                .filter(
                    JudgeCheckpoint.user_id == current_user.id,
                    JudgeCheckpoint.checkpoint_id == checkpoint_id,
                )
                .first()
            )
            if not assigned:
                return {"error": "forbidden", "detail": "Checkpoint not assigned."}, 403

        group_link = _get_active_group(team.id)
        group_name = group_link.group.name if group_link and group_link.group else ""
        if not group_name:
            return {"error": "invalid_request", "detail": "Team has no active group."}, 400
        group_id = group_link.group_id if group_link else None
        fields_info = _get_checkpoint_fields(comp_id, checkpoint_id, group_name)
        points_header = (fields_info.get("headers") or {}).get("points")
        rule = _get_score_rule(comp_id, checkpoint_id, group_id) if group_id else None

        checkin = Checkin.query.filter_by(
            competition_id=comp_id, team_id=team.id, checkpoint_id=checkpoint_id
        ).first()
        created_checkin = False
        if not checkin:
            checkin = Checkin(
                competition_id=comp_id,
                team_id=team.id,
                checkpoint_id=checkpoint_id,
                timestamp=datetime.utcnow(),
            )
            db.session.add(checkin)
            db.session.commit()
            created_checkin = True
            try:
                mark_arrival_checkbox(team.id, checkpoint_id, checkin.timestamp)
            except Exception:
                pass

        total = _compute_total(
            fields,
            points_header,
            rule,
            {"team_id": team.id, "competition_id": comp_id, "group_id": group_id},
        )
        entry = ScoreEntry(
            competition_id=comp_id,
            checkin_id=checkin.id,
            team_id=team.id,
            checkpoint_id=checkpoint_id,
            judge_user_id=current_user.id,
            raw_fields=fields,
            total=total,
            created_at=datetime.utcnow(),
        )
        db.session.add(entry)
        db.session.commit()

        # Update Google Sheets
        try:
            values = dict(fields)
            if total is not None:
                values["points"] = total
            update_checkpoint_scores(team.id, checkpoint_id, group_name, values, entry.created_at)
        except Exception:
            pass

        # Time-based race scoring: recompute latest entries for this checkpoint+group.
        if rule and rule.get("time_race"):
            tr = rule["time_race"] or {}
            start_checkpoint_id = tr.get("start_checkpoint_id")
            end_checkpoint_id = tr.get("end_checkpoint_id")
            if start_checkpoint_id and end_checkpoint_id:
                min_points = _to_number(tr.get("min_points")) or 0.0
                max_points = _to_number(tr.get("max_points")) or (total if total is not None else 0.0)
                latest_entries = (
                    ScoreEntry.query
                    .join(TeamGroup, TeamGroup.team_id == ScoreEntry.team_id)
                    .filter(
                        ScoreEntry.competition_id == comp_id,
                        ScoreEntry.checkpoint_id == checkpoint_id,
                        TeamGroup.group_id == group_id,
                        TeamGroup.active.is_(True),
                    )
                    .order_by(ScoreEntry.created_at.desc())
                    .all()
                )
                latest_by_team = {}
                for e in latest_entries:
                    if e.team_id not in latest_by_team:
                        latest_by_team[e.team_id] = e
                entries = list(latest_by_team.values())
                team_ids = [e.team_id for e in entries]
                scores = _compute_time_race_scores_from_checkins(
                    team_ids,
                    comp_id,
                    int(start_checkpoint_id),
                    int(end_checkpoint_id),
                    min_points,
                    max_points,
                )
                for e in entries:
                    base_total = _compute_total(
                        e.raw_fields or {},
                        points_header,
                        rule,
                        {"team_id": e.team_id, "competition_id": comp_id, "group_id": group_id},
                    )
                    if e.team_id in scores:
                        base_val = base_total or 0.0
                        e.total = base_val + scores[e.team_id]
                    else:
                        e.total = base_total
                db.session.commit()

                for e in entries:
                    if e.team_id in scores:
                        try:
                            values = dict(e.raw_fields or {})
                            values["points"] = e.total
                            update_checkpoint_scores(e.team_id, checkpoint_id, group_name, values, e.created_at)
                        except Exception:
                            pass

        card_writeback = None
        writeback_error = None
        if created_checkin and uid:
            digest = compute_card_digest(uid, int(checkpoint_id))
            if digest:
                card_writeback = {
                    "payload": digest,
                    "hmac": digest,
                    "device_id": int(checkpoint_id),
                    "card_uid": uid,
                    "checkpoint_id": checkpoint_id,
                    "checkpoint": checkpoint.name,
                    "team_id": team.id,
                    "team": team.name,
                }
            else:
                writeback_error = "writeback_unavailable"

        return {
            "ok": True,
            "score_id": entry.id,
            "total": entry.total,
            "checkin_created": created_checkin,
            "card_writeback": card_writeback,
            "writeback_error": writeback_error,
        }, 201
