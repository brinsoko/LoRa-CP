# app/resources/score_rules.py
from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.extensions import db
from app.models import ScoreRule
from app.utils.competition import require_current_competition_id
from app.utils.rest_auth import json_roles_required

score_rules_api_bp = Blueprint("api_score_rules", __name__)


@score_rules_api_bp.get("/api/score-rules")
@json_roles_required("admin")
def score_rule_list():
        comp_id = require_current_competition_id()
        if not comp_id:
            return jsonify({"error": "no_competition"}), 400
        checkpoint_id = request.args.get("checkpoint_id", type=int)
        group_id = request.args.get("group_id", type=int)

        query = ScoreRule.query.filter(ScoreRule.competition_id == comp_id)
        if checkpoint_id:
            query = query.filter(ScoreRule.checkpoint_id == checkpoint_id)
        if group_id:
            query = query.filter(ScoreRule.group_id == group_id)
        rules = query.order_by(ScoreRule.created_at.desc()).all()

        return {
            "rules": [
                {
                    "id": r.id,
                    "checkpoint_id": r.checkpoint_id,
                    "group_id": r.group_id,
                    "rules": r.rules,
                    "created_at": r.created_at.isoformat() if r.created_at else None,
                }
                for r in rules
            ]
        }, 200


@score_rules_api_bp.post("/api/score-rules")
@json_roles_required("admin")
def score_rule_create():
        comp_id = require_current_competition_id()
        if not comp_id:
            return jsonify({"error": "no_competition"}), 400
        payload = request.get_json(silent=True) or {}
        checkpoint_id = payload.get("checkpoint_id")
        group_id = payload.get("group_id")
        rules = payload.get("rules") or {}

        try:
            checkpoint_id = int(checkpoint_id)
            group_id = int(group_id)
        except Exception:
            return jsonify({"error": "invalid_request", "detail": "checkpoint_id and group_id are required."}), 400

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
            existing.rules = rules
            db.session.commit()
            try:
                from app.resources.scores import recompute_scores_for_rule
                recompute_scores_for_rule(comp_id, checkpoint_id, group_id)
            except Exception:
                pass
            return {"ok": True, "rule_id": existing.id, "updated": True}, 200

        record = ScoreRule(
            competition_id=comp_id,
            checkpoint_id=checkpoint_id,
            group_id=group_id,
            rules=rules,
        )
        db.session.add(record)
        db.session.commit()
        try:
            from app.resources.scores import recompute_scores_for_rule
            recompute_scores_for_rule(comp_id, checkpoint_id, group_id)
        except Exception:
            pass
        return {"ok": True, "rule_id": record.id, "created": True}, 201


@score_rules_api_bp.delete("/api/score-rules/<int:rule_id>")
@json_roles_required("admin")
def score_rule_delete(rule_id: int):
        comp_id = require_current_competition_id()
        if not comp_id:
            return jsonify({"error": "no_competition"}), 400
        rule = ScoreRule.query.filter(ScoreRule.competition_id == comp_id, ScoreRule.id == rule_id).first()
        if not rule:
            return jsonify({"error": "not_found"}), 404
        db.session.delete(rule)
        db.session.commit()
        return {"ok": True}, 200


@score_rules_api_bp.get("/api/score-rules/fields")
@json_roles_required("admin")
def score_rule_fields():
        comp_id = require_current_competition_id()
        if not comp_id:
            return jsonify({"error": "no_competition"}), 400
        checkpoint_id = request.args.get("checkpoint_id", type=int)
        group_id = request.args.get("group_id", type=int)
        if not checkpoint_id or not group_id:
            return jsonify({"error": "invalid_request", "detail": "checkpoint_id and group_id are required."}), 400

        from app.models import SheetConfig, CheckpointGroup

        cfg = (
            SheetConfig.query
            .filter(
                SheetConfig.competition_id == comp_id,
                SheetConfig.checkpoint_id == checkpoint_id,
                SheetConfig.tab_type == "checkpoint",
            )
            .order_by(SheetConfig.created_at.desc())
            .first()
        )
        if not cfg:
            return {"fields": [], "headers": {}}, 200

        group = CheckpointGroup.query.filter(
            CheckpointGroup.competition_id == comp_id,
            CheckpointGroup.id == group_id,
        ).first()
        group_name = group.name if group else ""

        headers = {
            "dead_time": (cfg.config or {}).get("dead_time_header"),
            "time": (cfg.config or {}).get("time_header"),
            "points": (cfg.config or {}).get("points_header"),
        }
        group_defs = (cfg.config or {}).get("groups", [])
        group_def = next(
            (g for g in group_defs if (g.get("name") or "").strip().lower() == group_name.strip().lower()),
            None,
        )
        fields = list(group_def.get("fields") or []) if group_def else []
        return {
            "fields": fields,
            "headers": headers,
            "config": cfg.config or {},
        }, 200
