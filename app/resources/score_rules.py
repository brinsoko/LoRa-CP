# app/resources/score_rules.py
"""REST API for score fields (phase-2 scoring model).

Replaces the old /api/score-rules JSON-blob CRUD. Fields are structured
rows per checkpoint with per-group enable/override; see app/models.py
ScoreField / ScoreFieldGroup and app/utils/scoring.resolve_fields.
"""

from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.extensions import db
from app.models import Checkpoint, CheckpointGroup, ScoreField, ScoreFieldGroup
from app.utils.competition import require_current_competition_id
from app.utils.rest_auth import json_roles_required
from app.utils.scoring import resolve_fields

score_rules_api_bp = Blueprint("api_score_rules", __name__)

_RULE_TYPES = ("none", "mapping", "interpolate", "multiplier", "deviation")


def _serialize_field(field: ScoreField) -> dict:
    return {
        "id": field.id,
        "checkpoint_id": field.checkpoint_id,
        "key": field.key,
        "label": field.label,
        "hint": field.hint,
        "position": field.position,
        "rule_type": field.rule_type,
        "rule_params": field.rule_params,
        "max_input": field.max_input,
        "counts_in_total": bool(field.counts_in_total),
        "groups": [
            {
                "group_id": row.group_id,
                "enabled": bool(row.enabled),
                "rule_override": row.rule_override,
            }
            for row in field.group_overrides
        ],
    }


@score_rules_api_bp.get("/api/score-fields")
@json_roles_required("admin")
def score_field_list():
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    checkpoint_id = request.args.get("checkpoint_id", type=int)
    query = ScoreField.query.filter(ScoreField.competition_id == comp_id)
    if checkpoint_id:
        query = query.filter(ScoreField.checkpoint_id == checkpoint_id)
    fields = query.order_by(
        ScoreField.checkpoint_id.asc(), ScoreField.position.asc(), ScoreField.id.asc()
    ).all()
    return {"fields": [_serialize_field(f) for f in fields]}, 200


@score_rules_api_bp.post("/api/score-fields")
@json_roles_required("admin")
def score_field_upsert():
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    payload = request.get_json(silent=True) or {}
    try:
        checkpoint_id = int(payload.get("checkpoint_id"))
    except Exception:
        return jsonify({"error": "invalid_request", "detail": "checkpoint_id is required."}), 400
    key = (payload.get("key") or "").strip()
    if not key:
        return jsonify({"error": "invalid_request", "detail": "key is required."}), 400
    checkpoint = Checkpoint.query.filter(
        Checkpoint.competition_id == comp_id, Checkpoint.id == checkpoint_id
    ).first()
    if not checkpoint:
        return jsonify({"error": "not_found", "detail": "Checkpoint not found."}), 404

    # rule_type + rule_params are a unit and are only touched when
    # rule_type is present. A partial update that omits rule_type (e.g.
    # a reorder sending only position) must NOT silently reset the field
    # to the raw-input 'none' rule and blow away rule_params - every
    # other attribute below is guarded with `in payload` the same way.
    rule_type = None
    rule_params = None
    if "rule_type" in payload:
        rule_type = (payload.get("rule_type") or "none").strip().lower()
        if rule_type not in _RULE_TYPES:
            return jsonify({"error": "validation_error", "detail": "invalid rule_type"}), 400
        rule_params = payload.get("rule_params")
        if rule_params is not None and not isinstance(rule_params, dict):
            return jsonify({"error": "validation_error", "detail": "rule_params must be an object"}), 400

    field = ScoreField.query.filter_by(checkpoint_id=checkpoint_id, key=key[:80]).first()
    created = field is None
    if field is None:
        max_position = (
            db.session.query(db.func.max(ScoreField.position))
            .filter(ScoreField.checkpoint_id == checkpoint_id)
            .scalar()
        )
        field = ScoreField(
            competition_id=comp_id,
            checkpoint_id=checkpoint_id,
            key=key[:80],
            position=(max_position if max_position is not None else -1) + 1,
        )
        db.session.add(field)
    if "label" in payload:
        field.label = (payload.get("label") or "").strip()[:160] or None
    if "hint" in payload:
        field.hint = (payload.get("hint") or "").strip()[:255] or None
    if "position" in payload:
        try:
            field.position = int(payload.get("position"))
        except (TypeError, ValueError):
            pass
    if "rule_type" in payload:
        field.rule_type = rule_type
        field.rule_params = rule_params or None
    if "max_input" in payload:
        try:
            field.max_input = float(payload.get("max_input")) if payload.get("max_input") is not None else None
        except (TypeError, ValueError):
            field.max_input = None
    if "counts_in_total" in payload:
        field.counts_in_total = bool(payload.get("counts_in_total"))
    db.session.flush()

    for g_data in payload.get("groups") or []:
        try:
            group_id = int(g_data.get("group_id"))
        except Exception:
            continue
        group = CheckpointGroup.query.filter(
            CheckpointGroup.competition_id == comp_id, CheckpointGroup.id == group_id
        ).first()
        if not group:
            continue
        row = ScoreFieldGroup.query.filter_by(score_field_id=field.id, group_id=group_id).first()
        enabled = bool(g_data.get("enabled", True))
        override = g_data.get("rule_override")
        if override is not None and not isinstance(override, dict):
            override = None
        if enabled and override is None:
            if row:
                db.session.delete(row)
            continue
        if not row:
            row = ScoreFieldGroup(score_field_id=field.id, group_id=group_id)
            db.session.add(row)
        row.enabled = enabled
        row.rule_override = override

    db.session.commit()

    from app.resources.scores import recompute_entry_totals

    for group in CheckpointGroup.query.filter(CheckpointGroup.competition_id == comp_id).all():
        try:
            recompute_entry_totals(comp_id, checkpoint_id, group.id)
        except Exception:
            pass
    return {"ok": True, "field": _serialize_field(field), "created": created}, (201 if created else 200)


@score_rules_api_bp.delete("/api/score-fields/<int:field_id>")
@json_roles_required("admin")
def score_field_delete(field_id: int):
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    field = ScoreField.query.filter(
        ScoreField.competition_id == comp_id, ScoreField.id == field_id
    ).first()
    if not field:
        return jsonify({"error": "not_found"}), 404
    db.session.delete(field)
    db.session.commit()
    return {"ok": True}, 200


@score_rules_api_bp.get("/api/score-fields/resolved")
@json_roles_required("admin")
def score_field_resolved():
    """Resolved field list as one group sees it (or the union across
    groups when group_id is '__all__' / omitted), the successor of the
    old /api/score-rules/fields endpoint used by admin tooling."""
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    checkpoint_id = request.args.get("checkpoint_id", type=int)
    if not checkpoint_id:
        return jsonify({"error": "invalid_request", "detail": "checkpoint_id is required."}), 400
    # Scope the checkpoint to the caller's competition: without this an
    # admin of one competition could read another competition's resolved
    # scoring config by passing a foreign checkpoint_id.
    if not Checkpoint.query.filter(
        Checkpoint.competition_id == comp_id, Checkpoint.id == checkpoint_id
    ).first():
        return jsonify({"error": "not_found", "detail": "Checkpoint not found."}), 404
    raw_group_id = (request.args.get("group_id") or "").strip()

    if raw_group_id and raw_group_id != "__all__":
        try:
            group_id = int(raw_group_id)
        except ValueError:
            return jsonify({"error": "invalid_request", "detail": "group_id must be an integer."}), 400
        if not CheckpointGroup.query.filter(
            CheckpointGroup.competition_id == comp_id, CheckpointGroup.id == group_id
        ).first():
            return jsonify({"error": "not_found", "detail": "Group not found."}), 404
        fields = resolve_fields(checkpoint_id, group_id)
        return {"fields": [f["key"] for f in fields], "resolved": fields}, 200

    # Union across all groups, first-seen order.
    seen: dict[str, dict] = {}
    groups = CheckpointGroup.query.filter(CheckpointGroup.competition_id == comp_id).all()
    for group in groups:
        for f in resolve_fields(checkpoint_id, group.id):
            seen.setdefault(f["key"], f)
    if not groups:
        for f in resolve_fields(checkpoint_id, None):
            seen.setdefault(f["key"], f)
    return {"fields": list(seen.keys()), "resolved": list(seen.values()), "all_groups": True}, 200
