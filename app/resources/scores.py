# app/resources/scores.py
from __future__ import annotations

from flask import Blueprint, jsonify, request
from flask_login import current_user
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    Checkin,
    Checkpoint,
    CompetitionMember,
    JudgeCheckpoint,
    RFIDCard,
    ScoreEntry,
    Team,
    TeamGroup,
)
from app.utils.audit import record_audit_event
from app.utils.card_tokens import compute_card_digest, looks_like_card_uid
from app.utils.competition import require_current_competition_id
from app.utils.rest_auth import json_roles_required
from app.utils.scoring import compute_entry_total, resolve_fields
from app.utils.serial_helpers import normalize_uid
from app.utils.sheets_sync import mark_arrival_checkbox, update_checkpoint_scores
from app.utils.time import utcnow_naive

scores_api_bp = Blueprint("api_scores", __name__)


def _norm_name(value: str | None) -> str:
    return (value or "").strip().casefold()


def _get_active_group(team_id: int) -> TeamGroup | None:
    return TeamGroup.query.filter(TeamGroup.team_id == team_id, TeamGroup.active.is_(True)).first()


def _to_number(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except Exception:
        return None


def _score_entry_snapshot(entry: ScoreEntry) -> dict:
    return {
        "id": entry.id,
        "team_id": entry.team_id,
        "team_name": entry.team.name if entry.team else None,
        "checkpoint_id": entry.checkpoint_id,
        "checkpoint_name": entry.checkpoint.name if entry.checkpoint else None,
        "judge_user_id": entry.judge_user_id,
        "judge_username": entry.judge_user.username if entry.judge_user else None,
        "total": entry.total,
        "raw_fields": entry.raw_fields or {},
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
    }


def _round_score(value: float | None) -> float | None:
    """Round score to 2 decimal places. Returns None for None input."""
    if value is None:
        return None
    return round(value, 2)


def _clamp_non_negative(value: float | None) -> float | None:
    """Clamp score to be >= 0. Returns None for None input."""
    if value is None:
        return None
    return max(0.0, value)


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
        return _round_score(_clamp_non_negative(_to_number(mapping.get(str(value)))))
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
            return _round_score(_clamp_non_negative(pts[0][1]))
        if x >= pts[-1][0]:
            return _round_score(_clamp_non_negative(pts[-1][1]))
        for (x1, y1), (x2, y2) in zip(pts, pts[1:], strict=False):
            if x1 <= x <= x2:
                if x2 == x1:
                    return _round_score(_clamp_non_negative(y1))
                t = (x - x1) / (x2 - x1)
                return _round_score(_clamp_non_negative(y1 + t * (y2 - y1)))
        return None
    if rule_type == "multiplier":
        factor = _to_number(rule.get("factor"))
        base = _to_number(value)
        if factor is None or base is None:
            return None
        return _round_score(_clamp_non_negative(base * factor))
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
            Checkin.query.filter(
                Checkin.competition_id == competition_id,
                Checkin.team_id == team_id,
                Checkin.checkpoint_id.in_(checkpoint_ids),
            )
            .with_entities(Checkin.checkpoint_id)
            .distinct()
            .all()
        )
        count = len(found)
        return _round_score(_clamp_non_negative(points_per * count))
    if value is None or value == "":
        return None
    if rule_type == "deviation":
        base = _to_number(value)
        target = _to_number(rule.get("target"))
        max_points = _to_number(rule.get("max_points"))
        penalty_points = _to_number(rule.get("penalty_points"))
        penalty_distance = _to_number(rule.get("penalty_distance"))
        min_points = _to_number(rule.get("min_points"))
        if min_points is None:
            min_points = 0.0
        if (
            base is None
            or target is None
            or max_points is None
            or penalty_points is None
            or penalty_distance in (None, 0)
        ):
            return None
        distance = abs(base - target)
        penalty = (distance / penalty_distance) * penalty_points
        points = max(max_points - penalty, min_points)
        return _round_score(_clamp_non_negative(points))
    return _round_score(_to_number(value))


def recompute_entry_totals(competition_id: int, checkpoint_id: int, group_id: int) -> None:
    """Re-derive ScoreEntry.total for the latest entry per team after the
    checkpoint's fields changed, and push the values to sheets. Timed
    segments are computed at read time and never touch entries."""
    from app.models import CheckpointGroup

    group = CheckpointGroup.query.filter(
        CheckpointGroup.competition_id == competition_id,
        CheckpointGroup.id == group_id,
    ).first()
    if not group:
        return
    group_name = group.name or ""
    fields = resolve_fields(checkpoint_id, group_id)

    latest_entries = (
        ScoreEntry.query.join(TeamGroup, TeamGroup.team_id == ScoreEntry.team_id)
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

    for entry in entries:
        total = compute_entry_total(
            entry.raw_fields or {},
            fields,
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


@scores_api_bp.post("/api/scores/resolve")
@json_roles_required("judge", "admin")
def score_resolve():
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400

    payload = request.get_json(silent=True) or {}
    # Same normalization as /api/ingest: drop any "|HMAC" suffix from v2
    # LoRa-format clients, strip ':'/'-', uppercase — so Web NFC scans
    # (colon-separated UIDs) match the canonical form stored on rfid_cards.
    uid = normalize_uid((payload.get("uid") or "").split("|", 1)[0])
    team_id = payload.get("team_id")
    checkpoint_id = payload.get("checkpoint_id")
    try:
        checkpoint_id = int(checkpoint_id)
    except Exception:
        return jsonify({"error": "invalid_request", "detail": "checkpoint_id is required."}), 400

    checkpoint = Checkpoint.query.filter(
        Checkpoint.competition_id == comp_id,
        Checkpoint.id == checkpoint_id,
    ).first()
    if not checkpoint:
        return jsonify({"error": "not_found", "detail": "Checkpoint not found."}), 404

    # Enforce judge assignment
    if CompetitionMember.query.filter(
        CompetitionMember.competition_id == comp_id,
        CompetitionMember.user_id == current_user.id,
        CompetitionMember.active.is_(True),
        CompetitionMember.role == "judge",
    ).first():
        assigned = JudgeCheckpoint.query.filter(
            JudgeCheckpoint.user_id == current_user.id,
            JudgeCheckpoint.checkpoint_id == checkpoint_id,
        ).first()
        if not assigned:
            return jsonify({"error": "forbidden", "detail": "Checkpoint not assigned."}), 403

    team = None
    if uid:
        card = RFIDCard.query.filter_by(competition_id=comp_id, uid=uid).first()
        if not card:
            return jsonify({"error": "not_found", "detail": "Card not mapped to a team."}), 404
        team = Team.query.filter(Team.competition_id == comp_id, Team.id == card.team_id).first()
    else:
        try:
            team_id = int(team_id)
        except Exception:
            return jsonify({"error": "invalid_request", "detail": "uid or team_id is required."}), 400
        team = Team.query.filter(Team.competition_id == comp_id, Team.id == team_id).first()
    if not team:
        return jsonify({"error": "not_found", "detail": "Team not found."}), 404

    group_link = _get_active_group(team.id)
    group_name = group_link.group.name if group_link and group_link.group else ""
    if not group_name:
        return jsonify({"error": "invalid_request", "detail": "Team has no active group."}), 400
    group_id = group_link.group_id if group_link else None

    # Fields come from ScoreField + per-group overrides. Scoring no longer
    # depends on any Sheets configuration; a checkpoint with no fields
    # falls back to a single points-only input.
    resolved = resolve_fields(checkpoint_id, group_id)

    from app.utils.judge_labels import auto_scoring_text, enrich_field_def

    field_defs = []
    if checkpoint.dead_time_enabled:
        field_defs.append(
            enrich_field_def({"key": "dead_time", "label": "Dead Time", "type": "number"}, None)
        )
    for field in resolved:
        field_defs.append(
            enrich_field_def({"key": field["key"], "label": field["label"], "type": "number"}, field.get("rule"))
        )
    has_score_input = any(fd.get("key") in ("score", "points") for fd in field_defs)
    has_scored_fields = any(fd.get("key") not in ("dead_time",) for fd in field_defs)
    if not has_score_input and not has_scored_fields:
        field_defs.append(enrich_field_def({"key": "points", "label": "Score", "type": "number"}, None))

    existing = (
        ScoreEntry.query.filter(
            ScoreEntry.competition_id == comp_id,
            ScoreEntry.team_id == team.id,
            ScoreEntry.checkpoint_id == checkpoint_id,
        )
        .order_by(ScoreEntry.created_at.desc())
        .first()
    )

    checkin = Checkin.query.filter_by(
        competition_id=comp_id, team_id=team.id, checkpoint_id=checkpoint_id
    ).first()
    checkin_created = False
    # The judge shell records the arrival at scan time (scan = the team is
    # standing in front of the judge), not at score submit. Same savepoint
    # pattern as submit for the concurrent-judges race.
    if checkin is None and payload.get("create_checkin") and not checkpoint.is_virtual:
        new_checkin = Checkin(
            competition_id=comp_id,
            team_id=team.id,
            checkpoint_id=checkpoint_id,
            timestamp=utcnow_naive(),
            created_by_user_id=current_user.id,
        )
        try:
            with db.session.begin_nested():
                db.session.add(new_checkin)
        except IntegrityError:
            checkin = Checkin.query.filter_by(
                competition_id=comp_id, team_id=team.id, checkpoint_id=checkpoint_id
            ).first()
        else:
            checkin = new_checkin
            checkin_created = True
            record_audit_event(
                competition_id=comp_id,
                event_type="checkin_created",
                entity_type="checkin",
                entity_id=checkin.id,
                actor_user=current_user,
                summary=f"Check-in recorded at scan for team {team.name} at {checkpoint.name}.",
                details={
                    "id": checkin.id,
                    "team_id": team.id,
                    "team_name": team.name,
                    "checkpoint_id": checkpoint.id,
                    "checkpoint_name": checkpoint.name,
                    "timestamp": checkin.timestamp.isoformat() if checkin.timestamp else None,
                },
                created_at=checkin.timestamp,
            )
        db.session.commit()
        if checkin_created:
            try:
                mark_arrival_checkbox(team.id, checkpoint_id, checkin.timestamp)
            except Exception:
                pass

    checkin_exists = checkin is not None

    # Checkpoint scoring_text: curated admin override wins; otherwise
    # auto-generate from the rule so the judge always sees something
    # actionable instead of a bare description.
    scoring_text = (checkpoint.scoring_text or "").strip()
    if not scoring_text and resolved:
        pseudo_rule = {"field_rules": {f["key"]: (f.get("rule") or {"label": f["label"]}) for f in resolved}}
        scoring_text = auto_scoring_text(pseudo_rule, field_keys=[f["key"] for f in resolved])

    return {
        "ok": True,
        "uid": uid or None,
        "team": {"id": team.id, "name": team.name, "number": team.number},
        "checkpoint": {
            "id": checkpoint.id,
            "name": checkpoint.name,
            "description": checkpoint.description or "",
            "scoring_text": scoring_text,
        },
        "group": {"name": group_name},
        "fields": field_defs,
        "latest_score": existing.raw_fields if existing else None,
        "latest_total": existing.total if existing else None,
        "checkin_created": checkin_created,
        "checkin_exists": checkin_exists,
    }, 200


@scores_api_bp.post("/api/scores/submit")
@json_roles_required("judge", "admin")
def score_submit():
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    payload = request.get_json(silent=True) or {}
    team_id = payload.get("team_id")
    checkpoint_id = payload.get("checkpoint_id")
    fields = payload.get("fields") or {}
    # Same normalization as /api/ingest — see note in score_resolve.
    uid = normalize_uid((payload.get("uid") or "").split("|", 1)[0])

    # Validate no negative numeric inputs (dead_time excluded — it's always >= 0)
    for key, val in fields.items():
        num = _to_number(val)
        if num is not None and num < 0 and key != "dead_time":
            from flask_babel import gettext as _

            return jsonify({"error": "validation_error", "detail": _("Score cannot be negative.")}), 400

    try:
        team_id = int(team_id)
        checkpoint_id = int(checkpoint_id)
    except Exception:
        return jsonify({"error": "invalid_request", "detail": "team_id and checkpoint_id are required."}), 400

    team = Team.query.filter(Team.competition_id == comp_id, Team.id == team_id).first()
    checkpoint = Checkpoint.query.filter(Checkpoint.competition_id == comp_id, Checkpoint.id == checkpoint_id).first()
    if not team or not checkpoint:
        return jsonify({"error": "invalid_request", "detail": "Invalid team or checkpoint."}), 400

    if CompetitionMember.query.filter(
        CompetitionMember.competition_id == comp_id,
        CompetitionMember.user_id == current_user.id,
        CompetitionMember.active.is_(True),
        CompetitionMember.role == "judge",
    ).first():
        assigned = JudgeCheckpoint.query.filter(
            JudgeCheckpoint.user_id == current_user.id,
            JudgeCheckpoint.checkpoint_id == checkpoint_id,
        ).first()
        if not assigned:
            return jsonify({"error": "forbidden", "detail": "Checkpoint not assigned."}), 403

    group_link = _get_active_group(team.id)
    group_name = group_link.group.name if group_link and group_link.group else ""
    if not group_name:
        return jsonify({"error": "invalid_request", "detail": "Team has no active group."}), 400
    group_id = group_link.group_id if group_link else None
    resolved = resolve_fields(checkpoint_id, group_id)

    checkin = Checkin.query.filter_by(competition_id=comp_id, team_id=team.id, checkpoint_id=checkpoint_id).first()
    created_checkin = False
    if not checkin and not checkpoint.is_virtual:
        new_checkin = Checkin(
            competition_id=comp_id,
            team_id=team.id,
            checkpoint_id=checkpoint_id,
            timestamp=utcnow_naive(),
            created_by_user_id=current_user.id,
        )
        # SAVEPOINT: another judge submitting a score for the same
        # team/checkpoint at the same moment may have inserted the
        # checkin row first. Treat the uq_team_checkpoint violation as
        # "use the existing checkin" instead of leaking a 500.
        try:
            with db.session.begin_nested():
                db.session.add(new_checkin)
        except IntegrityError:
            checkin = Checkin.query.filter_by(
                competition_id=comp_id, team_id=team.id, checkpoint_id=checkpoint_id
            ).first()
        else:
            checkin = new_checkin
            record_audit_event(
                competition_id=comp_id,
                event_type="checkin_created",
                entity_type="checkin",
                entity_id=checkin.id,
                actor_user=current_user,
                summary=f"Check-in auto-created during scoring for team {team.name} at {checkpoint.name}.",
                details={
                    "id": checkin.id,
                    "team_id": team.id,
                    "team_name": team.name,
                    "checkpoint_id": checkpoint.id,
                    "checkpoint_name": checkpoint.name,
                    "timestamp": checkin.timestamp.isoformat() if checkin.timestamp else None,
                },
                created_at=checkin.timestamp,
            )
            created_checkin = True
        db.session.commit()
        if created_checkin:
            try:
                mark_arrival_checkbox(team.id, checkpoint_id, checkin.timestamp)
            except Exception:
                pass

    total = compute_entry_total(
        fields,
        resolved,
        {"team_id": team.id, "competition_id": comp_id, "group_id": group_id},
    )
    entry = ScoreEntry(
        competition_id=comp_id,
        checkin_id=checkin.id if checkin else None,
        team_id=team.id,
        checkpoint_id=checkpoint_id,
        judge_user_id=current_user.id,
        raw_fields=fields,
        total=total,
        created_at=utcnow_naive(),
    )
    db.session.add(entry)
    db.session.flush()
    record_audit_event(
        competition_id=comp_id,
        event_type="score_submitted",
        entity_type="score_entry",
        entity_id=entry.id,
        actor_user=current_user,
        summary=f"Score submitted for team {team.name} at {checkpoint.name}.",
        details=_score_entry_snapshot(entry),
        created_at=entry.created_at,
    )
    db.session.commit()

    # Update Google Sheets
    try:
        values = dict(fields)
        if total is not None:
            values["points"] = total
        update_checkpoint_scores(team.id, checkpoint_id, group_name, values, entry.created_at)
    except Exception:
        pass

    card_writeback = None
    writeback_error = None
    # Only produce a writeback for inputs that actually look like a scanned
    # card UID — see looks_like_card_uid for the heuristic. Manual judge
    # entries (selected via team dropdown) used to trigger a writeback
    # attempt that always failed and flashed "Card write-back failed" in
    # the UI, which was confusing.
    if created_checkin and looks_like_card_uid(uid):
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
