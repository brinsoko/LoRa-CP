# app/utils/scoring_backfill.py
"""Convert legacy scoring blobs into the phase-2 tables, model level.

The alembic migration f9a0b1c2d3e4 does the same conversion in raw SQL for
in-place upgrades; this module covers pre-1.2.0 transfer files (import and
merge), where the legacy sections arrive as dicts:

- sheet_configs[].config.groups[].fields  -> ScoreField + ScoreFieldGroup
- score_rules[].rules.field_rules         -> default rules / overrides
- score_rules[].rules.time_race           -> TimedSegment (deduplicated)
- global_score_rules[].rules              -> GroupScoring + counts_for_found
- sheet_configs[].config.dead_time_enabled -> Checkpoint.dead_time_enabled
"""

from __future__ import annotations

from app.extensions import db
from app.models import (
    Checkpoint,
    CheckpointGroup,
    GroupScoring,
    ScoreField,
    ScoreFieldGroup,
    TimedSegment,
)

_META_KEYS = {"label", "hint", "max", "max_input"}


def _norm(name: str | None) -> str:
    return (name or "").strip().casefold()


def split_rule(rule) -> tuple[str, dict | None, str | None, str | None, float | None]:
    """Legacy field-rule dict -> (rule_type, params, label, hint, max_input)."""
    if isinstance(rule, list):
        rule = rule[0] if rule else {}
    if not isinstance(rule, dict):
        return "none", None, None, None, None
    rule_type = (rule.get("type") or "none").strip().lower()
    if rule_type not in ("mapping", "interpolate", "multiplier", "deviation"):
        rule_type = "none"
    params = {k: v for k, v in rule.items() if k not in _META_KEYS and k != "type"}
    label = rule.get("label")
    hint = rule.get("hint")
    max_input = rule.get("max_input", rule.get("max"))
    try:
        max_input = float(max_input) if max_input is not None else None
    except (TypeError, ValueError):
        max_input = None
    return rule_type, (params or None), label, hint, max_input


def _to_float(value, default=None):
    try:
        return float(value) if value is not None else default
    except (TypeError, ValueError):
        return default


def convert_legacy_scoring(
    comp_id: int,
    cp_map: dict[str, Checkpoint],
    group_map: dict[str, CheckpointGroup],
    sheet_configs: list[dict],
    score_rules: list[dict],
    global_score_rules: list[dict],
) -> None:
    """Create phase-2 scoring rows from legacy transfer sections.

    Add-only: checkpoints that already have ScoreField rows and groups
    that already have GroupScoring are left alone, so merges never
    clobber local configuration.
    """
    groups_ordered = sorted(group_map.values(), key=lambda g: (g.position, g.id))

    # fields_by_cp[cp_id][group_id] = ordered keys
    fields_by_cp: dict[int, dict[int, list[str]]] = {}
    dead_time_cp_ids: set[int] = set()
    for cfg in sheet_configs or []:
        if (cfg.get("tab_type") or "checkpoint") != "checkpoint":
            continue
        cp = cp_map.get(cfg.get("checkpoint_name"))
        if not cp:
            continue
        config = cfg.get("config") or {}
        if config.get("dead_time_enabled"):
            dead_time_cp_ids.add(cp.id)
        for group_def in config.get("groups") or []:
            group = None
            gid = group_def.get("group_id")
            if gid is not None:
                group = next((g for g in groups_ordered if g.id == gid), None)
            if group is None:
                group = next(
                    (g for g in groups_ordered if _norm(g.name) == _norm(group_def.get("name"))), None
                )
            if group is None:
                continue
            keys = [k for k in (group_def.get("fields") or []) if isinstance(k, str) and k.strip()]
            fields_by_cp.setdefault(cp.id, {})[group.id] = keys

    rules_by_cp_group: dict[tuple[int, int], dict] = {}
    for sr in score_rules or []:
        cp = cp_map.get(sr.get("checkpoint_name"))
        group = group_map.get(sr.get("group_name"))
        if cp and group:
            rules_by_cp_group[(cp.id, group.id)] = sr.get("rules") or {}

    for (cp_id, gid), rules in rules_by_cp_group.items():
        for key in (rules.get("field_rules") or {}).keys():
            existing = fields_by_cp.setdefault(cp_id, {}).setdefault(gid, [])
            if key not in existing:
                existing.append(key)

    for cp_id, per_group in fields_by_cp.items():
        if ScoreField.query.filter_by(checkpoint_id=cp_id).first():
            continue
        ordered_keys: list[str] = []
        for group in groups_ordered:
            for key in per_group.get(group.id, []):
                if key not in ordered_keys:
                    ordered_keys.append(key)
        for key in sorted({k for keys in per_group.values() for k in keys} - set(ordered_keys)):
            ordered_keys.append(key)

        for position, key in enumerate(ordered_keys):
            default_rule = None
            for group in groups_ordered:
                rule = (rules_by_cp_group.get((cp_id, group.id)) or {}).get("field_rules", {}).get(key)
                if rule is not None:
                    default_rule = rule
                    break
            rule_type, params, label, hint, max_input = split_rule(default_rule)

            counts = True
            for group in groups_ordered:
                totals = (rules_by_cp_group.get((cp_id, group.id)) or {}).get("total_fields")
                if totals:
                    counts = key in totals
                    break
            if key == "dead_time":
                counts = False

            field = ScoreField(
                competition_id=comp_id,
                checkpoint_id=cp_id,
                key=key[:80],
                label=label,
                hint=hint,
                position=position,
                rule_type=rule_type,
                rule_params=params,
                max_input=max_input,
                counts_in_total=counts,
            )
            db.session.add(field)
            db.session.flush()

            for group in groups_ordered:
                if group.id not in per_group:
                    continue
                enabled = key in per_group.get(group.id, [])
                rule = (rules_by_cp_group.get((cp_id, group.id)) or {}).get("field_rules", {}).get(key)
                g_rtype, g_params, _l, _h, g_maxin = split_rule(rule)
                override = None
                if rule is not None and (g_rtype, g_params, g_maxin) != (rule_type, params, max_input):
                    override = {"rule_type": g_rtype, "rule_params": g_params, "max_input": g_maxin}
                if not enabled or override is not None:
                    db.session.add(
                        ScoreFieldGroup(
                            score_field_id=field.id,
                            group_id=group.id,
                            enabled=enabled,
                            rule_override=override,
                        )
                    )

    # time_race -> segments (deduplicated per path, forward-normalized)
    seen: set[tuple[int, int, int]] = {
        (s.path_id, s.start_checkpoint_id, s.end_checkpoint_id)
        for s in TimedSegment.query.filter_by(competition_id=comp_id).all()
    }
    for (_cp_id, gid), rules in rules_by_cp_group.items():
        tr = rules.get("time_race") or {}
        start_id = _to_float(tr.get("start_checkpoint_id"))
        end_id = _to_float(tr.get("end_checkpoint_id"))
        start_id = int(start_id) if start_id else None
        end_id = int(end_id) if end_id else None
        # Name-based references from hand-authored payloads.
        if tr.get("start_checkpoint_name") and cp_map.get(tr["start_checkpoint_name"]):
            start_id = cp_map[tr["start_checkpoint_name"]].id
        if tr.get("end_checkpoint_name") and cp_map.get(tr["end_checkpoint_name"]):
            end_id = cp_map[tr["end_checkpoint_name"]].id
        if not (start_id and end_id):
            continue
        group = next((g for g in groups_ordered if g.id == gid), None)
        if not group or not group.path_id:
            continue
        if group.direction == "reverse":
            start_id, end_id = end_id, start_id
        key = (group.path_id, start_id, end_id)
        if key in seen or (group.path_id, end_id, start_id) in seen:
            continue
        seen.add(key)
        db.session.add(
            TimedSegment(
                competition_id=comp_id,
                path_id=group.path_id,
                start_checkpoint_id=start_id,
                end_checkpoint_id=end_id,
                max_points=_to_float(tr.get("max_points"), 100.0),
                min_points=_to_float(tr.get("min_points"), 0.0),
            )
        )

    # global rules -> GroupScoring + counts_for_found
    for gr in global_score_rules or []:
        group = group_map.get(gr.get("group_name"))
        if not group:
            continue
        if GroupScoring.query.filter_by(group_id=group.id).first():
            continue
        rules = gr.get("rules") or {}
        found = rules.get("found") or {}
        time_rule = rules.get("time") or {}
        db.session.add(
            GroupScoring(
                group_id=group.id,
                competition_id=comp_id,
                found_points_per=_to_float(found.get("points_per")),
                race_max_points=_to_float(time_rule.get("max_points")),
                race_threshold_minutes=_to_float(time_rule.get("threshold_minutes")),
                race_penalty_minutes=_to_float(time_rule.get("penalty_minutes")),
                race_penalty_points=_to_float(time_rule.get("penalty_points")),
                race_min_points=_to_float(time_rule.get("min_points")),
                race_dq_multiplier=_to_float(time_rule.get("dq_multiplier")),
            )
        )
        for flag, name_key, id_key in (
            ("exclude_start_checkpoint", "start_checkpoint_name", "start_checkpoint_id"),
            ("exclude_end_checkpoint", "end_checkpoint_name", "end_checkpoint_id"),
        ):
            if not found.get(flag):
                continue
            cp = None
            if time_rule.get(name_key):
                cp = cp_map.get(time_rule.get(name_key))
            elif time_rule.get(id_key):
                cp = db.session.get(Checkpoint, int(time_rule.get(id_key)))
            if cp:
                cp.counts_for_found = False

    for cp_id in dead_time_cp_ids:
        cp = db.session.get(Checkpoint, cp_id)
        if cp:
            cp.dead_time_enabled = True
    for cp in cp_map.values():
        if cp.is_virtual:
            cp.counts_for_found = False
