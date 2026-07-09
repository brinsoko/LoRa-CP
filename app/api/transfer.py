# app/api/transfer.py
"""Export / Import / Merge competition data as JSON."""

from __future__ import annotations

import json
from datetime import datetime

from flask import Blueprint, jsonify, make_response, request
from flask_login import current_user

from app.api.helpers import json_ok
from app.extensions import db
from app.models import (
    Checkin,
    Checkpoint,
    CheckpointGroup,
    Competition,
    CompetitionMember,
    GroupScoring,
    LoRaDevice,
    Path,
    PathStop,
    RFIDCard,
    ScoreEntry,
    ScoreField,
    ScoreFieldGroup,
    SheetConfig,
    Team,
    TeamGroup,
    TeamMember,
    TimedSegment,
    User,
)
from app.utils.paths import resolve_route_ids
from app.utils.rest_auth import json_roles_required
from app.utils.scoring_backfill import convert_legacy_scoring
from app.utils.serial_helpers import normalize_uid
from app.utils.time import utcnow_naive

transfer_api_bp = Blueprint("api_transfer", __name__)

# 1.1.0: paths/path_stops become first-class; groups carry path_name +
# direction. group_checkpoint_links is still exported as a derived legacy
# view (per-group resolved route) and still accepted on import for old
# files, where it is converted into paths.
# 1.2.0: scoring becomes first-class (score_fields / timed_segments /
# group_scoring, checkpoint counts_for_found + dead_time_enabled).
# Legacy score_rules / global_score_rules sections are still accepted on
# import and converted (app/utils/scoring_backfill.py).
SCHEMA_VERSION = "1.2.0"


def _get_or_create_path_for_sequence(
    comp_id: int,
    preferred_name: str,
    checkpoint_ids: list[int],
    cache: dict[tuple[int, ...], Path],
) -> tuple[Path | None, str]:
    """Return (path, direction) for a checkpoint sequence, creating if new.

    Same merge rule as the phase-1 migration backfill: an existing path
    with the identical sequence is shared forward; one with the exact
    reversed sequence is shared with direction='reverse'.
    """
    if not checkpoint_ids:
        return None, "forward"
    seq = tuple(checkpoint_ids)
    if seq in cache:
        return cache[seq], "forward"
    if tuple(reversed(seq)) in cache:
        return cache[tuple(reversed(seq))], "reverse"

    name = preferred_name
    existing_names = {p.name for p in cache.values()}
    counter = 2
    while name in existing_names:
        name = f"{preferred_name} ({counter})"
        counter += 1
    path = Path(competition_id=comp_id, name=name)
    db.session.add(path)
    db.session.flush()
    for position, checkpoint_id in enumerate(checkpoint_ids):
        db.session.add(PathStop(path_id=path.id, checkpoint_id=checkpoint_id, position=position))
    cache[seq] = path
    return path, "forward"


# ---- serialisation helpers ----


def _export_competition(comp: Competition) -> dict:
    """Build the full export payload for a competition."""
    teams = Team.query.filter_by(competition_id=comp.id).all()
    groups = CheckpointGroup.query.filter_by(competition_id=comp.id).all()
    checkpoints = Checkpoint.query.filter_by(competition_id=comp.id).all()
    checkins = Checkin.query.filter_by(competition_id=comp.id).all()
    devices = LoRaDevice.query.filter_by(competition_id=comp.id).all()
    scores = ScoreEntry.query.filter_by(competition_id=comp.id).all()
    sheet_configs = SheetConfig.query.filter_by(competition_id=comp.id).all()
    score_fields = ScoreField.query.filter_by(competition_id=comp.id).order_by(
        ScoreField.checkpoint_id.asc(), ScoreField.position.asc()
    ).all()
    field_group_rows = (
        ScoreFieldGroup.query.join(ScoreField, ScoreFieldGroup.score_field_id == ScoreField.id)
        .filter(ScoreField.competition_id == comp.id)
        .all()
    )
    field_groups_by_field: dict[int, list] = {}
    for row in field_group_rows:
        field_groups_by_field.setdefault(row.score_field_id, []).append(row)
    timed_segments = TimedSegment.query.filter_by(competition_id=comp.id).all()
    group_scoring_rows = GroupScoring.query.filter_by(competition_id=comp.id).all()
    group_name_by_id = {g.id: g.name for g in groups}
    rfid_cards = RFIDCard.query.join(Team, RFIDCard.team_id == Team.id).filter(Team.competition_id == comp.id).all()
    team_groups = TeamGroup.query.join(Team, TeamGroup.team_id == Team.id).filter(Team.competition_id == comp.id).all()
    paths = Path.query.filter_by(competition_id=comp.id).all()
    path_name_by_id = {path.id: path.name for path in paths}
    cp_name_by_id = {cp.id: cp.name for cp in checkpoints}

    # Derived legacy view of each group's resolved directed route, kept so
    # older importers and scripts/json_export_to_csv.py keep working. The
    # authoritative data is the "paths" section; import prefers it.
    legacy_links = []
    for g in groups:
        seen: set[int] = set()
        position = 0
        for cp_id in resolve_route_ids(g):
            if cp_id in seen:
                continue
            seen.add(cp_id)
            legacy_links.append(
                {
                    "group_name": g.name,
                    "checkpoint_name": cp_name_by_id.get(cp_id),
                    "position": position,
                }
            )
            position += 1

    return {
        "schema_version": SCHEMA_VERSION,
        "exported_at": utcnow_naive().isoformat() + "Z",
        "competition": {
            "name": comp.name,
            "settings": {
                "public_results": comp.public_results,
                "hide_gps_map": comp.hide_gps_map,
                "hide_dev_map": comp.hide_dev_map,
                "hide_audit_messages": comp.hide_audit_messages,
                "hide_score_submissions": comp.hide_score_submissions,
            },
        },
        "teams": [
            {
                "name": t.name,
                "number": t.number,
                "organization": t.organization,
                "dnf": bool(t.dnf),
                "members": [
                    {"name": m.name, "role": m.role, "position": m.position}
                    for m in sorted(t.members or [], key=lambda m: m.position)
                ],
            }
            for t in teams
        ],
        "groups": [
            {
                "name": g.name,
                "prefix": g.prefix,
                "description": g.description,
                "position": g.position,
                "path_name": g.path.name if g.path else None,
                "direction": g.direction,
            }
            for g in groups
        ],
        "paths": [
            {
                "name": p.name,
                "notes": p.notes,
                "stops": [
                    {
                        "checkpoint_name": cp_name_by_id.get(stop.checkpoint_id),
                        "position": stop.position,
                        "expected_leg_minutes": stop.expected_leg_minutes,
                    }
                    for stop in p.stops
                ],
            }
            for p in paths
        ],
        "checkpoints": [
            {
                "name": cp.name,
                "location": cp.location,
                "description": cp.description,
                "scoring_text": cp.scoring_text,
                "judges_note": cp.judges_note,
                "easting": cp.easting,
                "northing": cp.northing,
                "is_virtual": bool(cp.is_virtual),
                "counts_for_found": bool(cp.counts_for_found),
                "dead_time_enabled": bool(cp.dead_time_enabled),
            }
            for cp in checkpoints
        ],
        "checkins": [
            {
                "team_name": c.team.name if c.team else None,
                "checkpoint_name": c.checkpoint.name if c.checkpoint else None,
                "timestamp": c.timestamp.isoformat() if c.timestamp else None,
                "created_by_username": (c.created_by_user.username if c.created_by_user else None),
                "created_by_dev_num": (c.created_by_device.dev_num if c.created_by_device else None),
            }
            for c in checkins
        ],
        "devices": [
            {
                "dev_num": d.dev_num,
                "name": d.name,
                "note": d.note,
                "model": d.model,
                "active": bool(d.active),
            }
            for d in devices
        ],
        "scores": [
            {
                "team_name": s.team.name if s.team else None,
                "checkpoint_name": s.checkpoint.name if s.checkpoint else None,
                "raw_fields": s.raw_fields,
                "total": s.total,
                # Matches the Checkin export fidelity (created_by_username
                # + timestamp): preserve who scored and when so a
                # round-trip doesn't lose audit context.
                "judge_username": s.judge_user.username if s.judge_user else None,
                "created_at": s.created_at.isoformat() if s.created_at else None,
            }
            for s in scores
        ],
        # Scoring configuration: fields per checkpoint (with per-group
        # enable/override), timed segments per path, category rules.
        "score_fields": [
            {
                "checkpoint_name": cp_name_by_id.get(f.checkpoint_id),
                "key": f.key,
                "label": f.label,
                "hint": f.hint,
                "position": f.position,
                "rule_type": f.rule_type,
                "rule_params": f.rule_params,
                "max_input": f.max_input,
                "counts_in_total": bool(f.counts_in_total),
                "groups": [
                    {
                        "group_name": group_name_by_id.get(row.group_id),
                        "enabled": bool(row.enabled),
                        "rule_override": row.rule_override,
                    }
                    for row in field_groups_by_field.get(f.id, [])
                ],
            }
            for f in score_fields
        ],
        "timed_segments": [
            {
                "path_name": path_name_by_id.get(s.path_id),
                "start_checkpoint_name": cp_name_by_id.get(s.start_checkpoint_id),
                "end_checkpoint_name": cp_name_by_id.get(s.end_checkpoint_id),
                "name": s.name,
                "max_points": s.max_points,
                "min_points": s.min_points,
            }
            for s in timed_segments
        ],
        "group_scoring": [
            {
                "group_name": group_name_by_id.get(row.group_id),
                "found_points_per": row.found_points_per,
                "race_max_points": row.race_max_points,
                "race_threshold_minutes": row.race_threshold_minutes,
                "race_penalty_minutes": row.race_penalty_minutes,
                "race_penalty_points": row.race_penalty_points,
                "race_min_points": row.race_min_points,
                "race_dq_multiplier": row.race_dq_multiplier,
            }
            for row in group_scoring_rows
        ],
        # SheetConfig holds the per-checkpoint scoring layout (which fields
        # the judge UI exposes, dead-time / time toggles, headers, per-group
        # field lists). The spreadsheet_id is environment-specific, so the
        # export omits it; the import re-creates each config with a
        # local-only spreadsheet_id so it never auto-syncs to a Google Sheet
        # the destination installation doesn't own.
        "sheet_configs": [
            {
                "tab_name": cfg.tab_name,
                "tab_type": cfg.tab_type,
                "checkpoint_name": cfg.checkpoint.name if cfg.checkpoint else None,
                "config": cfg.config,
            }
            for cfg in sheet_configs
        ],
        "rfid_cards": [
            {
                "uid": card.uid,
                "team_name": card.team.name if card.team else None,
                "number": card.number,
            }
            for card in rfid_cards
        ],
        "team_groups": [
            {
                "team_name": tg.team.name if tg.team else None,
                "group_name": tg.group.name if tg.group else None,
                "active": bool(tg.active),
            }
            for tg in team_groups
        ],
        "group_checkpoint_links": legacy_links,
    }


def _local_spreadsheet_id(comp_id: int) -> str:
    """Match the convention used by app/blueprints/sheets/routes.py for
    SheetConfig records that have no remote spreadsheet to sync to."""
    return f"local:{comp_id}"


def _remap_score_rule_blob(
    rule_blob: dict | None,
    cp_map: dict[str, Checkpoint],
) -> dict | None:
    """Resolve name-based checkpoint references inside a ScoreRule /
    GlobalScoreRule.rules blob to local checkpoint IDs.

    Hand-authored import payloads use names because the source's
    checkpoint IDs don't exist in the destination DB. Supported
    name fields (each takes precedence over the corresponding _id
    if present):

    - rules["time_race"]["start_checkpoint_name"] -> start_checkpoint_id
    - rules["time_race"]["end_checkpoint_name"]   -> end_checkpoint_id
    - rules["time"]["start_checkpoint_name"]      -> start_checkpoint_id
    - rules["time"]["end_checkpoint_name"]        -> end_checkpoint_id
    - rules["field_rules"][f]["checkpoint_names"] -> checkpoint_ids (for type="found")
      (and the same inside list-form field rules)

    Names that don't resolve are dropped so the runtime's name-based
    fallback can kick in, mirroring _remap_sheet_config.
    """
    if not rule_blob or not isinstance(rule_blob, dict):
        return rule_blob
    out = dict(rule_blob)

    for key in ("time_race", "time"):
        block = out.get(key)
        if not isinstance(block, dict):
            continue
        block = dict(block)
        for name_key, id_key in (
            ("start_checkpoint_name", "start_checkpoint_id"),
            ("end_checkpoint_name", "end_checkpoint_id"),
        ):
            ref = block.pop(name_key, None)
            if not ref:
                continue
            local = cp_map.get(ref)
            if local is not None:
                block[id_key] = local.id
        out[key] = block

    field_rules = out.get("field_rules")
    if isinstance(field_rules, dict):
        new_fr = {}
        for field_name, raw_rule in field_rules.items():
            new_fr[field_name] = _remap_field_rule(raw_rule, cp_map)
        out["field_rules"] = new_fr

    return out


def _remap_field_rule(rule, cp_map: dict[str, Checkpoint]):
    """field_rules entries can be a single rule dict or a list of rule
    dicts (the chain form). Recurse for the list form; for the dict
    form, swap checkpoint_names -> checkpoint_ids when present.
    """
    if isinstance(rule, list):
        return [_remap_field_rule(r, cp_map) for r in rule]
    if not isinstance(rule, dict):
        return rule
    out = dict(rule)
    names = out.pop("checkpoint_names", None)
    if names:
        resolved: list[int] = []
        for nm in names:
            local = cp_map.get(nm)
            if local is not None:
                resolved.append(local.id)
        if resolved:
            out["checkpoint_ids"] = resolved
    return out


def _remap_sheet_config(cfg_blob: dict | None, group_map: dict[str, CheckpointGroup]) -> dict | None:
    """Rewrite stale group_id references inside a SheetConfig.config blob.

    The exported config carries group_id values from the source competition;
    those IDs do not exist in the destination DB. Resolve each entry by name
    to the new group's id, or drop the field so the runtime falls back to
    the name-based lookup in _resolve_group_from_cfg.
    """
    if not cfg_blob or not isinstance(cfg_blob, dict):
        return cfg_blob
    out = dict(cfg_blob)
    groups = out.get("groups")
    if isinstance(groups, list):
        new_groups = []
        for grp in groups:
            if not isinstance(grp, dict):
                new_groups.append(grp)
                continue
            grp_copy = dict(grp)
            name = (grp_copy.get("name") or "").strip()
            local = group_map.get(name)
            if local is not None:
                grp_copy["group_id"] = local.id
            else:
                grp_copy.pop("group_id", None)
            new_groups.append(grp_copy)
        out["groups"] = new_groups
    return out


def _validate_export_json(data: dict) -> list[str]:
    """Return a list of validation errors (empty = valid)."""
    errors = []
    if not isinstance(data, dict):
        return ["Root must be a JSON object."]
    if "schema_version" not in data:
        errors.append("Missing schema_version.")
    required_sections = ["competition", "teams", "groups", "checkpoints"]
    for section in required_sections:
        if section not in data:
            errors.append(f"Missing required section: {section}")
    return errors


def _import_competition_from_json(data: dict) -> tuple[Competition, list[str]]:
    """Create a new competition from import JSON. Returns (competition, warnings)."""
    warnings = []

    version = data.get("schema_version", "")
    if version != SCHEMA_VERSION:
        warnings.append(f"Schema version mismatch: file has '{version}', current is '{SCHEMA_VERSION}'.")

    comp_data = data.get("competition", {})
    comp_name = comp_data.get("name", "Imported Competition")
    # Ensure unique name
    existing = Competition.query.filter_by(name=comp_name).first()
    if existing:
        comp_name = f"{comp_name} (imported {utcnow_naive().strftime('%Y%m%d-%H%M%S')})"

    settings = comp_data.get("settings", {})
    comp = Competition(
        name=comp_name,
        public_results=settings.get("public_results", False),
        hide_gps_map=settings.get("hide_gps_map", False),
        hide_dev_map=settings.get("hide_dev_map", False),
        hide_audit_messages=settings.get("hide_audit_messages", False),
        hide_score_submissions=settings.get("hide_score_submissions", False),
        created_by_user_id=current_user.id if current_user.is_authenticated else None,
    )
    db.session.add(comp)
    db.session.flush()

    # Add current user as admin member
    if current_user.is_authenticated:
        db.session.add(
            CompetitionMember(
                competition_id=comp.id,
                user_id=current_user.id,
                role="admin",
                active=True,
            )
        )

    # Groups (path wiring happens after checkpoints + paths exist)
    group_map = {}  # name → CheckpointGroup
    for g_data in data.get("groups", []):
        g = CheckpointGroup(
            competition_id=comp.id,
            name=g_data["name"],
            prefix=g_data.get("prefix"),
            description=g_data.get("description"),
            position=g_data.get("position", 0),
        )
        db.session.add(g)
        db.session.flush()
        group_map[g.name] = g

    # Checkpoints
    cp_map = {}  # name → Checkpoint
    for cp_data in data.get("checkpoints", []):
        cp = Checkpoint(
            competition_id=comp.id,
            name=cp_data["name"],
            location=cp_data.get("location"),
            description=cp_data.get("description"),
            easting=cp_data.get("easting"),
            northing=cp_data.get("northing"),
            is_virtual=bool(cp_data.get("is_virtual", False)),
            counts_for_found=bool(cp_data.get("counts_for_found", True)),
            dead_time_enabled=bool(cp_data.get("dead_time_enabled", False)),
        )
        db.session.add(cp)
        db.session.flush()
        cp_map[cp.name] = cp

    # Teams
    team_map = {}  # name → Team
    for t_data in data.get("teams", []):
        t = Team(
            competition_id=comp.id,
            name=t_data["name"],
            number=t_data.get("number"),
            organization=t_data.get("organization"),
            dnf=t_data.get("dnf", False),
        )
        db.session.add(t)
        db.session.flush()
        for idx, m_data in enumerate(t_data.get("members") or []):
            if not isinstance(m_data, dict):
                continue
            name_val = (m_data.get("name") or "").strip()
            if not name_val:
                continue
            role_val = m_data.get("role")
            if isinstance(role_val, str):
                role_val = role_val.strip() or None
            else:
                role_val = None
            db.session.add(
                TeamMember(
                    team_id=t.id,
                    name=name_val[:160],
                    role=role_val[:80] if role_val else None,
                    position=m_data.get("position", idx),
                )
            )
        team_map[t.name] = t

    # Devices
    for d_data in data.get("devices", []):
        d = LoRaDevice(
            competition_id=comp.id,
            dev_num=d_data["dev_num"],
            name=d_data.get("name"),
            note=d_data.get("note"),
            model=d_data.get("model"),
            active=d_data.get("active", True),
        )
        db.session.add(d)

    # Paths. New-format files carry a "paths" section plus per-group
    # path_name/direction; legacy files only have group_checkpoint_links,
    # which we convert into paths with the same forward/reverse merge rule
    # as the schema migration.
    path_map: dict[str, Path] = {}  # name → Path
    if data.get("paths"):
        for p_data in data.get("paths", []):
            p = Path(competition_id=comp.id, name=p_data["name"], notes=p_data.get("notes"))
            db.session.add(p)
            db.session.flush()
            stops = sorted(p_data.get("stops") or [], key=lambda s: s.get("position", 0))
            position = 0
            for s_data in stops:
                cp = cp_map.get(s_data.get("checkpoint_name"))
                if not cp:
                    warnings.append(
                        f"Path '{p.name}': unknown checkpoint '{s_data.get('checkpoint_name')}' skipped."
                    )
                    continue
                db.session.add(
                    PathStop(
                        path_id=p.id,
                        checkpoint_id=cp.id,
                        position=position,
                        expected_leg_minutes=s_data.get("expected_leg_minutes"),
                    )
                )
                position += 1
            path_map[p.name] = p
        for g_data in data.get("groups", []):
            group = group_map.get(g_data.get("name"))
            if not group:
                continue
            path_name = g_data.get("path_name")
            if path_name:
                group.path = path_map.get(path_name)
                if group.path is None:
                    warnings.append(f"Group '{group.name}': unknown path '{path_name}'.")
            direction = (g_data.get("direction") or "forward").strip().lower()
            group.direction = direction if direction in ("forward", "reverse") else "forward"
    else:
        links_by_group: dict[str, list[tuple[int, int]]] = {}
        for gl_data in data.get("group_checkpoint_links", []):
            group = group_map.get(gl_data.get("group_name"))
            cp = cp_map.get(gl_data.get("checkpoint_name"))
            if group and cp:
                links_by_group.setdefault(group.name, []).append((gl_data.get("position", 0), cp.id))
        sequence_cache: dict[tuple[int, ...], Path] = {}
        for group_name, entries in links_by_group.items():
            group = group_map[group_name]
            ordered_ids = [cp_id for (_pos, cp_id) in sorted(entries, key=lambda e: e[0])]
            path, direction = _get_or_create_path_for_sequence(
                comp.id, group_name, ordered_ids, sequence_cache
            )
            group.path = path
            group.direction = direction

    # Team ↔ group assignments
    for tg_data in data.get("team_groups", []):
        team = team_map.get(tg_data.get("team_name"))
        group = group_map.get(tg_data.get("group_name"))
        if team and group:
            db.session.add(
                TeamGroup(
                    team_id=team.id,
                    group_id=group.id,
                    active=tg_data.get("active", True),
                )
            )

    # RFID cards
    for card_data in data.get("rfid_cards", []):
        team = team_map.get(card_data.get("team_name"))
        if team:
            # Normalize on import — older exports may carry colon-separated
            # UIDs that wouldn't match the canonical /api/ingest lookup form.
            uid = normalize_uid(card_data.get("uid", ""))
            if not uid:
                continue
            existing_uid = RFIDCard.query.filter_by(competition_id=comp.id, uid=uid).first()
            if not existing_uid:
                db.session.add(
                    RFIDCard(
                        competition_id=comp.id,
                        uid=uid,
                        team_id=team.id,
                        number=card_data.get("number"),
                    )
                )

    # Check-ins
    # Resolve user and device references from the export so the
    # round-trip preserves who created each row. Users are looked up
    # globally by username (they may already exist in the destination);
    # devices are looked up within the destination competition by
    # dev_num. Missing references degrade gracefully to None.
    for ci_data in data.get("checkins", []):
        team = team_map.get(ci_data.get("team_name"))
        cp = cp_map.get(ci_data.get("checkpoint_name"))
        if team and cp:
            ts = None
            if ci_data.get("timestamp"):
                try:
                    ts = datetime.fromisoformat(ci_data["timestamp"])
                except Exception:
                    ts = utcnow_naive()

            created_by_user_id = None
            created_by_username = ci_data.get("created_by_username")
            if created_by_username:
                u = User.query.filter_by(username=created_by_username).first()
                if u:
                    created_by_user_id = u.id

            created_by_device_id = None
            created_by_dev_num = ci_data.get("created_by_dev_num")
            if created_by_dev_num is not None:
                d = LoRaDevice.query.filter_by(competition_id=comp.id, dev_num=created_by_dev_num).first()
                if d:
                    created_by_device_id = d.id

            db.session.add(
                Checkin(
                    competition_id=comp.id,
                    team_id=team.id,
                    checkpoint_id=cp.id,
                    timestamp=ts or utcnow_naive(),
                    created_by_user_id=created_by_user_id,
                    created_by_device_id=created_by_device_id,
                )
            )

    # Scores
    db.session.flush()
    for s_data in data.get("scores", []):
        team = team_map.get(s_data.get("team_name"))
        cp = cp_map.get(s_data.get("checkpoint_name"))
        if team and cp:
            checkin = Checkin.query.filter_by(
                team_id=team.id,
                checkpoint_id=cp.id,
                competition_id=comp.id,
            ).first()
            # Preserve who scored and when, mirroring the Checkin import
            # fidelity. Older exports without these fields still work.
            judge_user_id = None
            judge_username = s_data.get("judge_username")
            if judge_username:
                ju = User.query.filter_by(username=judge_username).first()
                if ju:
                    judge_user_id = ju.id
            created_at = None
            ts_raw = s_data.get("created_at")
            if ts_raw:
                try:
                    created_at = datetime.fromisoformat(ts_raw)
                except Exception:
                    created_at = None
            score_kwargs = {
                "competition_id": comp.id,
                "checkin_id": checkin.id if checkin else None,
                "team_id": team.id,
                "checkpoint_id": cp.id,
                "judge_user_id": judge_user_id,
                "raw_fields": s_data.get("raw_fields"),
                "total": s_data.get("total"),
            }
            if created_at is not None:
                score_kwargs["created_at"] = created_at
            db.session.add(ScoreEntry(**score_kwargs))

    # Scoring configuration. New-format sections are created directly;
    # legacy files (score_rules / global_score_rules / config field lists)
    # are converted after sheet_configs land, see below.
    for f_data in data.get("score_fields", []):
        cp = cp_map.get(f_data.get("checkpoint_name"))
        if not cp:
            continue
        field = ScoreField(
            competition_id=comp.id,
            checkpoint_id=cp.id,
            key=(f_data.get("key") or "")[:80],
            label=f_data.get("label"),
            hint=f_data.get("hint"),
            position=f_data.get("position", 0),
            rule_type=(f_data.get("rule_type") or "none"),
            rule_params=f_data.get("rule_params"),
            max_input=f_data.get("max_input"),
            counts_in_total=bool(f_data.get("counts_in_total", True)),
        )
        db.session.add(field)
        db.session.flush()
        for g_data in f_data.get("groups") or []:
            group = group_map.get(g_data.get("group_name"))
            if not group:
                continue
            db.session.add(
                ScoreFieldGroup(
                    score_field_id=field.id,
                    group_id=group.id,
                    enabled=bool(g_data.get("enabled", True)),
                    rule_override=g_data.get("rule_override"),
                )
            )

    for s_data in data.get("timed_segments", []):
        seg_path = path_map.get(s_data.get("path_name"))
        start_cp = cp_map.get(s_data.get("start_checkpoint_name"))
        end_cp = cp_map.get(s_data.get("end_checkpoint_name"))
        if not (seg_path and start_cp and end_cp):
            continue
        db.session.add(
            TimedSegment(
                competition_id=comp.id,
                path_id=seg_path.id,
                start_checkpoint_id=start_cp.id,
                end_checkpoint_id=end_cp.id,
                name=s_data.get("name"),
                max_points=s_data.get("max_points") or 100.0,
                min_points=s_data.get("min_points") or 0.0,
            )
        )

    for gs_data in data.get("group_scoring", []):
        group = group_map.get(gs_data.get("group_name"))
        if not group:
            continue
        db.session.add(
            GroupScoring(
                group_id=group.id,
                competition_id=comp.id,
                found_points_per=gs_data.get("found_points_per"),
                race_max_points=gs_data.get("race_max_points"),
                race_threshold_minutes=gs_data.get("race_threshold_minutes"),
                race_penalty_minutes=gs_data.get("race_penalty_minutes"),
                race_penalty_points=gs_data.get("race_penalty_points"),
                race_min_points=gs_data.get("race_min_points"),
                race_dq_multiplier=gs_data.get("race_dq_multiplier"),
            )
        )

    # Sheet configs (per-checkpoint scoring field layout). Created with a
    # local-only spreadsheet_id so the destination never tries to write to
    # the source's Google Sheet. The admin can later re-point a config at
    # a real spreadsheet via the wizard.
    local_ss_id = _local_spreadsheet_id(comp.id)
    for sc_data in data.get("sheet_configs", []):
        cp_name = sc_data.get("checkpoint_name")
        target_cp = cp_map.get(cp_name) if cp_name else None
        db.session.add(
            SheetConfig(
                competition_id=comp.id,
                spreadsheet_id=local_ss_id,
                spreadsheet_name="Local",
                tab_name=sc_data.get("tab_name") or (cp_name or "Tab"),
                tab_type=sc_data.get("tab_type") or "checkpoint",
                checkpoint_id=target_cp.id if target_cp else None,
                config=_remap_sheet_config(sc_data.get("config"), group_map),
            )
        )

    # Legacy scoring (pre-1.2.0 files): convert score_rules /
    # global_score_rules / config field lists into the phase-2 tables.
    if not data.get("score_fields") and (
        data.get("score_rules") or data.get("global_score_rules") or data.get("sheet_configs")
    ):
        legacy_rules = [
            {
                "checkpoint_name": sr.get("checkpoint_name"),
                "group_name": sr.get("group_name"),
                "rules": _remap_score_rule_blob(sr.get("rules"), cp_map) or {},
            }
            for sr in data.get("score_rules", [])
        ]
        legacy_globals = [
            {
                "group_name": gr.get("group_name"),
                "rules": _remap_score_rule_blob(gr.get("rules"), cp_map) or {},
            }
            for gr in data.get("global_score_rules", [])
        ]
        db.session.flush()
        convert_legacy_scoring(
            comp.id, cp_map, group_map, data.get("sheet_configs", []), legacy_rules, legacy_globals
        )

    db.session.flush()
    return comp, warnings


# ---- merge helpers ----


def _find_conflicts(data: dict, comp: Competition) -> list[dict]:
    """Compare imported JSON against existing competition and return conflicts."""
    conflicts = []

    existing_teams = {t.name: t for t in Team.query.filter_by(competition_id=comp.id).all()}
    for t_data in data.get("teams", []):
        name = t_data.get("name")
        if name in existing_teams:
            local = existing_teams[name]
            # Check if data differs
            diffs = {}
            if t_data.get("number") != local.number:
                diffs["number"] = {"local": local.number, "imported": t_data.get("number")}
            if (t_data.get("organization") or None) != (local.organization or None):
                diffs["organization"] = {"local": local.organization, "imported": t_data.get("organization")}
            if diffs:
                conflicts.append(
                    {
                        "entity_type": "team",
                        "identifier": name,
                        "local": {"name": local.name, "number": local.number, "organization": local.organization},
                        "imported": t_data,
                        "differences": diffs,
                    }
                )

    existing_cps = {cp.name: cp for cp in Checkpoint.query.filter_by(competition_id=comp.id).all()}
    for cp_data in data.get("checkpoints", []):
        name = cp_data.get("name")
        if name in existing_cps:
            local = existing_cps[name]
            diffs = {}
            if cp_data.get("easting") != local.easting:
                diffs["easting"] = {"local": local.easting, "imported": cp_data.get("easting")}
            if cp_data.get("northing") != local.northing:
                diffs["northing"] = {"local": local.northing, "imported": cp_data.get("northing")}
            if diffs:
                conflicts.append(
                    {
                        "entity_type": "checkpoint",
                        "identifier": name,
                        "local": {"name": local.name, "easting": local.easting, "northing": local.northing},
                        "imported": cp_data,
                        "differences": diffs,
                    }
                )

    existing_groups = {g.name: g for g in CheckpointGroup.query.filter_by(competition_id=comp.id).all()}
    for g_data in data.get("groups", []):
        name = g_data.get("name")
        if name in existing_groups:
            local = existing_groups[name]
            diffs = {}
            if (g_data.get("prefix") or None) != (local.prefix or None):
                diffs["prefix"] = {"local": local.prefix, "imported": g_data.get("prefix")}
            if diffs:
                conflicts.append(
                    {
                        "entity_type": "group",
                        "identifier": name,
                        "local": {"name": local.name, "prefix": local.prefix},
                        "imported": g_data,
                        "differences": diffs,
                    }
                )

    return conflicts


def _apply_merge(data: dict, comp: Competition, resolutions: dict) -> dict:
    """Apply merge with conflict resolutions. Returns summary.

    Score entries are merged with add-new-only semantics matched by
    (team_name, checkpoint_name): merge writes go to the local DB only;
    they intentionally bypass the Google Sheets sync helpers so a local
    merge never requires sheets write permission.
    """
    added = {
        "teams": 0,
        "checkpoints": 0,
        "groups": 0,
        "checkins": 0,
        "scores": 0,
        "sheet_configs": 0,
        "team_groups": 0,
        "paths": 0,
        "group_checkpoint_links": 0,
        "score_rules": 0,
        "global_score_rules": 0,
        "score_fields": 0,
        "timed_segments": 0,
        "group_scoring": 0,
        "devices": 0,
        "rfid_cards": 0,
    }
    updated = {"teams": 0, "checkpoints": 0, "groups": 0}
    skipped = 0

    # Build resolution lookup: "entity_type:identifier" → action
    res_lookup = {}
    for key, action in resolutions.items():
        res_lookup[key] = action

    # Groups
    group_map = {g.name: g for g in CheckpointGroup.query.filter_by(competition_id=comp.id).all()}
    for g_data in data.get("groups", []):
        name = g_data.get("name")
        if name in group_map:
            key = f"group:{name}"
            action = res_lookup.get(key, "keep_local")
            if action == "use_imported":
                g = group_map[name]
                g.prefix = g_data.get("prefix")
                g.description = g_data.get("description")
                updated["groups"] += 1
            elif action == "skip":
                skipped += 1
        else:
            g = CheckpointGroup(
                competition_id=comp.id,
                name=name,
                prefix=g_data.get("prefix"),
                description=g_data.get("description"),
                position=g_data.get("position", 0),
            )
            db.session.add(g)
            db.session.flush()
            group_map[name] = g
            added["groups"] += 1

    # Checkpoints
    cp_map = {cp.name: cp for cp in Checkpoint.query.filter_by(competition_id=comp.id).all()}
    for cp_data in data.get("checkpoints", []):
        name = cp_data.get("name")
        if name in cp_map:
            key = f"checkpoint:{name}"
            action = res_lookup.get(key, "keep_local")
            if action == "use_imported":
                cp = cp_map[name]
                cp.easting = cp_data.get("easting")
                cp.northing = cp_data.get("northing")
                cp.location = cp_data.get("location")
                cp.description = cp_data.get("description")
                cp.scoring_text = cp_data.get("scoring_text")
                cp.judges_note = cp_data.get("judges_note")
                cp.is_virtual = bool(cp_data.get("is_virtual", cp.is_virtual))
                cp.counts_for_found = bool(cp_data.get("counts_for_found", cp.counts_for_found))
                cp.dead_time_enabled = bool(cp_data.get("dead_time_enabled", cp.dead_time_enabled))
                updated["checkpoints"] += 1
            elif action == "skip":
                skipped += 1
        else:
            cp = Checkpoint(
                competition_id=comp.id,
                name=name,
                location=cp_data.get("location"),
                description=cp_data.get("description"),
                scoring_text=cp_data.get("scoring_text"),
                judges_note=cp_data.get("judges_note"),
                easting=cp_data.get("easting"),
                northing=cp_data.get("northing"),
                is_virtual=bool(cp_data.get("is_virtual", False)),
                counts_for_found=bool(cp_data.get("counts_for_found", True)),
                dead_time_enabled=bool(cp_data.get("dead_time_enabled", False)),
            )
            db.session.add(cp)
            db.session.flush()
            cp_map[name] = cp
            added["checkpoints"] += 1

    # Teams
    team_map = {t.name: t for t in Team.query.filter_by(competition_id=comp.id).all()}
    for t_data in data.get("teams", []):
        name = t_data.get("name")
        if name in team_map:
            key = f"team:{name}"
            action = res_lookup.get(key, "keep_local")
            if action == "use_imported":
                t = team_map[name]
                t.number = t_data.get("number")
                t.organization = t_data.get("organization")
                t.dnf = t_data.get("dnf", False)
                updated["teams"] += 1
            elif action == "skip":
                skipped += 1
        else:
            t = Team(
                competition_id=comp.id,
                name=name,
                number=t_data.get("number"),
                organization=t_data.get("organization"),
                dnf=t_data.get("dnf", False),
            )
            db.session.add(t)
            db.session.flush()
            for idx, m_data in enumerate(t_data.get("members") or []):
                if not isinstance(m_data, dict):
                    continue
                name_val = (m_data.get("name") or "").strip()
                if not name_val:
                    continue
                role_val = m_data.get("role")
                if isinstance(role_val, str):
                    role_val = role_val.strip() or None
                else:
                    role_val = None
                db.session.add(
                    TeamMember(
                        team_id=t.id,
                        name=name_val[:160],
                        role=role_val[:80] if role_val else None,
                        position=m_data.get("position", idx),
                    )
                )
            team_map[name] = t
            added["teams"] += 1

    # Team -> group assignments. Without this, teams that came in via the
    # merge (or pre-existed locally) are not linked to the groups that
    # carried them in the source competition, so the scoring UI shows
    # them as orphaned. Add-new-only semantics: existing (team, group)
    # pairs are left alone.
    db.session.flush()
    existing_team_groups = {
        (tg.team_id, tg.group_id)
        for tg in TeamGroup.query.join(Team, TeamGroup.team_id == Team.id).filter(Team.competition_id == comp.id).all()
    }
    for tg_data in data.get("team_groups", []):
        team = team_map.get(tg_data.get("team_name"))
        group = group_map.get(tg_data.get("group_name"))
        if not (team and group):
            continue
        if (team.id, group.id) in existing_team_groups:
            continue
        db.session.add(
            TeamGroup(
                team_id=team.id,
                group_id=group.id,
                active=tg_data.get("active", True),
            )
        )
        existing_team_groups.add((team.id, group.id))
        added["team_groups"] += 1

    # Routes. Without this, newly merged groups or checkpoints have no
    # route, so they don't appear in arrivals/score builds that gate on
    # the resolved path. New-format files merge the "paths" section
    # (add-new-only by name) and wire groups; legacy files carry only
    # group_checkpoint_links, which are converted/appended per group.
    existing_paths = {p.name: p for p in Path.query.filter_by(competition_id=comp.id).all()}
    if data.get("paths"):
        for p_data in data.get("paths", []):
            name = p_data.get("name")
            if not name or name in existing_paths:
                continue
            p = Path(competition_id=comp.id, name=name, notes=p_data.get("notes"))
            db.session.add(p)
            db.session.flush()
            stops = sorted(p_data.get("stops") or [], key=lambda s: s.get("position", 0))
            position = 0
            for s_data in stops:
                cp = cp_map.get(s_data.get("checkpoint_name"))
                if not cp:
                    continue
                db.session.add(
                    PathStop(
                        path_id=p.id,
                        checkpoint_id=cp.id,
                        position=position,
                        expected_leg_minutes=s_data.get("expected_leg_minutes"),
                    )
                )
                position += 1
            existing_paths[name] = p
            added["paths"] += 1
        for g_data in data.get("groups", []):
            group = group_map.get(g_data.get("name"))
            if not group or group.path_id:
                continue
            path_name = g_data.get("path_name")
            if path_name and path_name in existing_paths:
                group.path = existing_paths[path_name]
                direction = (g_data.get("direction") or "forward").strip().lower()
                group.direction = direction if direction in ("forward", "reverse") else "forward"
    else:
        links_by_group: dict[str, list[tuple[int, int]]] = {}
        for gl_data in data.get("group_checkpoint_links", []):
            group = group_map.get(gl_data.get("group_name"))
            cp = cp_map.get(gl_data.get("checkpoint_name"))
            if group and cp:
                links_by_group.setdefault(group.name, []).append((gl_data.get("position", 0), cp.id))
        sequence_cache = {
            tuple(stop.checkpoint_id for stop in p.stops): p for p in existing_paths.values()
        }
        for group_name, entries in links_by_group.items():
            group = group_map[group_name]
            ordered_ids = [cp_id for (_pos, cp_id) in sorted(entries, key=lambda e: e[0])]
            if group.path_id is None:
                path, direction = _get_or_create_path_for_sequence(
                    comp.id, group_name, ordered_ids, sequence_cache
                )
                if path is not None:
                    group.path = path
                    group.direction = direction
                    added["group_checkpoint_links"] += len(ordered_ids)
                continue
            # Existing route: preserve the legacy add-new-only semantics by
            # appending checkpoints the route doesn't have yet. This edits
            # the shared path, matching how a legacy merge extended the
            # group's own link list.
            path = group.path
            current_ids = {stop.checkpoint_id for stop in path.stops}
            next_position = max((stop.position for stop in path.stops), default=-1) + 1
            for cp_id in ordered_ids:
                if cp_id in current_ids:
                    continue
                db.session.add(
                    PathStop(path_id=path.id, checkpoint_id=cp_id, position=next_position)
                )
                current_ids.add(cp_id)
                next_position += 1
                added["group_checkpoint_links"] += 1
        db.session.flush()

    # Check-ins (add only new ones, matched by team+checkpoint)
    for ci_data in data.get("checkins", []):
        team = team_map.get(ci_data.get("team_name"))
        cp = cp_map.get(ci_data.get("checkpoint_name"))
        if team and cp:
            existing = Checkin.query.filter_by(
                team_id=team.id,
                checkpoint_id=cp.id,
                competition_id=comp.id,
            ).first()
            if not existing:
                ts = utcnow_naive()
                if ci_data.get("timestamp"):
                    try:
                        ts = datetime.fromisoformat(ci_data["timestamp"])
                    except Exception:
                        pass
                db.session.add(
                    Checkin(
                        competition_id=comp.id,
                        team_id=team.id,
                        checkpoint_id=cp.id,
                        timestamp=ts,
                    )
                )
                added["checkins"] += 1

    # Flush so newly-added checkins above are visible to the score lookup.
    db.session.flush()

    # Score entries (add only new ones, matched by team+checkpoint).
    # Local-only: no Google Sheets sync helpers are invoked here.
    existing_scores = {
        (s.team_id, s.checkpoint_id): s for s in ScoreEntry.query.filter_by(competition_id=comp.id).all()
    }
    for s_data in data.get("scores", []):
        team = team_map.get(s_data.get("team_name"))
        cp = cp_map.get(s_data.get("checkpoint_name"))
        if not (team and cp):
            continue
        if (team.id, cp.id) in existing_scores:
            continue
        checkin = Checkin.query.filter_by(
            team_id=team.id,
            checkpoint_id=cp.id,
            competition_id=comp.id,
        ).first()
        # Preserve judge attribution + timestamp on merge so a re-export
        # is faithful and the audit trail survives a merge cycle.
        judge_user_id = None
        judge_username = s_data.get("judge_username")
        if judge_username:
            ju = User.query.filter_by(username=judge_username).first()
            if ju:
                judge_user_id = ju.id
        created_at = None
        ts_raw = s_data.get("created_at")
        if ts_raw:
            try:
                created_at = datetime.fromisoformat(ts_raw)
            except Exception:
                created_at = None
        score_kwargs = {
            "competition_id": comp.id,
            "checkin_id": checkin.id if checkin else None,
            "team_id": team.id,
            "checkpoint_id": cp.id,
            "judge_user_id": judge_user_id,
            "raw_fields": s_data.get("raw_fields"),
            "total": s_data.get("total"),
        }
        if created_at is not None:
            score_kwargs["created_at"] = created_at
        db.session.add(ScoreEntry(**score_kwargs))
        added["scores"] += 1

    # Sheet configs (per-checkpoint scoring field layout). Add only configs
    # whose (tab_type, tab_name) pair isn't already present locally; existing
    # ones are left alone so a merge can't clobber the admin's hand-tuned
    # Google Sheets wiring. New configs get a local-only spreadsheet_id.
    existing_configs = {(sc.tab_type, sc.tab_name) for sc in SheetConfig.query.filter_by(competition_id=comp.id).all()}
    local_ss_id = _local_spreadsheet_id(comp.id)
    for sc_data in data.get("sheet_configs", []):
        tab_type = sc_data.get("tab_type") or "checkpoint"
        tab_name = sc_data.get("tab_name")
        if not tab_name:
            continue
        if (tab_type, tab_name) in existing_configs:
            continue
        cp_name = sc_data.get("checkpoint_name")
        target_cp = cp_map.get(cp_name) if cp_name else None
        db.session.add(
            SheetConfig(
                competition_id=comp.id,
                spreadsheet_id=local_ss_id,
                spreadsheet_name="Local",
                tab_name=tab_name,
                tab_type=tab_type,
                checkpoint_id=target_cp.id if target_cp else None,
                config=_remap_sheet_config(sc_data.get("config"), group_map),
            )
        )
        existing_configs.add((tab_type, tab_name))
        added["sheet_configs"] += 1

    # Scoring configuration. Add-new-only, matched by structure: fields
    # by (checkpoint, key), segments by (path, endpoints), group scoring
    # by group; admins hand-tune these, a merge must not clobber them.
    existing_fields = {
        (f.checkpoint_id, f.key)
        for f in ScoreField.query.filter_by(competition_id=comp.id).all()
    }
    for f_data in data.get("score_fields", []):
        cp = cp_map.get(f_data.get("checkpoint_name"))
        key = (f_data.get("key") or "")[:80]
        if not cp or not key or (cp.id, key) in existing_fields:
            continue
        field = ScoreField(
            competition_id=comp.id,
            checkpoint_id=cp.id,
            key=key,
            label=f_data.get("label"),
            hint=f_data.get("hint"),
            position=f_data.get("position", 0),
            rule_type=(f_data.get("rule_type") or "none"),
            rule_params=f_data.get("rule_params"),
            max_input=f_data.get("max_input"),
            counts_in_total=bool(f_data.get("counts_in_total", True)),
        )
        db.session.add(field)
        db.session.flush()
        for g_data in f_data.get("groups") or []:
            group = group_map.get(g_data.get("group_name"))
            if not group:
                continue
            db.session.add(
                ScoreFieldGroup(
                    score_field_id=field.id,
                    group_id=group.id,
                    enabled=bool(g_data.get("enabled", True)),
                    rule_override=g_data.get("rule_override"),
                )
            )
        existing_fields.add((cp.id, key))
        added["score_fields"] += 1

    existing_segments = {
        (s.path_id, s.start_checkpoint_id, s.end_checkpoint_id)
        for s in TimedSegment.query.filter_by(competition_id=comp.id).all()
    }
    local_paths = {p.name: p for p in Path.query.filter_by(competition_id=comp.id).all()}
    for s_data in data.get("timed_segments", []):
        seg_path = local_paths.get(s_data.get("path_name"))
        start_cp = cp_map.get(s_data.get("start_checkpoint_name"))
        end_cp = cp_map.get(s_data.get("end_checkpoint_name"))
        if not (seg_path and start_cp and end_cp):
            continue
        key = (seg_path.id, start_cp.id, end_cp.id)
        if key in existing_segments or (seg_path.id, end_cp.id, start_cp.id) in existing_segments:
            continue
        db.session.add(
            TimedSegment(
                competition_id=comp.id,
                path_id=seg_path.id,
                start_checkpoint_id=start_cp.id,
                end_checkpoint_id=end_cp.id,
                name=s_data.get("name"),
                max_points=s_data.get("max_points") or 100.0,
                min_points=s_data.get("min_points") or 0.0,
            )
        )
        existing_segments.add(key)
        added["timed_segments"] += 1

    existing_group_scoring = {
        row.group_id for row in GroupScoring.query.filter_by(competition_id=comp.id).all()
    }
    for gs_data in data.get("group_scoring", []):
        group = group_map.get(gs_data.get("group_name"))
        if not group or group.id in existing_group_scoring:
            continue
        db.session.add(
            GroupScoring(
                group_id=group.id,
                competition_id=comp.id,
                found_points_per=gs_data.get("found_points_per"),
                race_max_points=gs_data.get("race_max_points"),
                race_threshold_minutes=gs_data.get("race_threshold_minutes"),
                race_penalty_minutes=gs_data.get("race_penalty_minutes"),
                race_penalty_points=gs_data.get("race_penalty_points"),
                race_min_points=gs_data.get("race_min_points"),
                race_dq_multiplier=gs_data.get("race_dq_multiplier"),
            )
        )
        existing_group_scoring.add(group.id)
        added["group_scoring"] += 1

    # Legacy scoring sections (pre-1.2.0 files): converted add-new-only.
    if not data.get("score_fields") and (data.get("score_rules") or data.get("global_score_rules")):
        legacy_rules = [
            {
                "checkpoint_name": sr.get("checkpoint_name"),
                "group_name": sr.get("group_name"),
                "rules": _remap_score_rule_blob(sr.get("rules"), cp_map) or {},
            }
            for sr in data.get("score_rules", [])
        ]
        legacy_globals = [
            {
                "group_name": gr.get("group_name"),
                "rules": _remap_score_rule_blob(gr.get("rules"), cp_map) or {},
            }
            for gr in data.get("global_score_rules", [])
        ]
        before_fields = ScoreField.query.filter_by(competition_id=comp.id).count()
        before_globals = GroupScoring.query.filter_by(competition_id=comp.id).count()
        db.session.flush()
        convert_legacy_scoring(
            comp.id, cp_map, group_map, data.get("sheet_configs", []), legacy_rules, legacy_globals
        )
        db.session.flush()
        added["score_rules"] += ScoreField.query.filter_by(competition_id=comp.id).count() - before_fields
        added["global_score_rules"] += (
            GroupScoring.query.filter_by(competition_id=comp.id).count() - before_globals
        )

    # LoRa devices (matched by dev_num within the competition).
    existing_dev_nums = {d.dev_num for d in LoRaDevice.query.filter_by(competition_id=comp.id).all()}
    for d_data in data.get("devices", []):
        dev_num = d_data.get("dev_num")
        if dev_num is None or dev_num in existing_dev_nums:
            continue
        db.session.add(
            LoRaDevice(
                competition_id=comp.id,
                dev_num=dev_num,
                name=d_data.get("name"),
                note=d_data.get("note"),
                model=d_data.get("model"),
                active=d_data.get("active", True),
            )
        )
        existing_dev_nums.add(dev_num)
        added["devices"] += 1

    # RFID cards (matched by uid within the competition). UIDs are
    # normalized to the canonical /api/ingest lookup form.
    db.session.flush()
    existing_uids = {card.uid for card in RFIDCard.query.filter_by(competition_id=comp.id).all()}
    for card_data in data.get("rfid_cards", []):
        uid = normalize_uid(card_data.get("uid", ""))
        if not uid or uid in existing_uids:
            continue
        team = team_map.get(card_data.get("team_name"))
        if not team:
            continue
        db.session.add(
            RFIDCard(
                competition_id=comp.id,
                uid=uid,
                team_id=team.id,
                number=card_data.get("number"),
            )
        )
        existing_uids.add(uid)
        added["rfid_cards"] += 1

    db.session.flush()
    return {"added": added, "updated": updated, "skipped": skipped}


# ---- API endpoints ----


@transfer_api_bp.get("/api/competition/<int:comp_id>/export")
@json_roles_required("admin")
def export_competition(comp_id: int):
    comp = Competition.query.get(comp_id)
    if not comp:
        return jsonify({"error": "not_found"}), 404

    payload = _export_competition(comp)
    resp = make_response(json.dumps(payload, indent=2, ensure_ascii=False))
    resp.headers["Content-Type"] = "application/json; charset=utf-8"
    resp.headers["Content-Disposition"] = f'attachment; filename="competition_{comp_id}_export.json"'
    return resp


@transfer_api_bp.post("/api/competition/import")
@json_roles_required("admin")
def import_competition():
    # Accept JSON body or file upload
    if request.content_type and "multipart" in request.content_type:
        file = request.files.get("file")
        if not file:
            return jsonify({"error": "invalid_request", "detail": "No file uploaded."}), 400
        try:
            data = json.loads(file.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return jsonify({"error": "invalid_json", "detail": str(exc)}), 400
    else:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "invalid_json", "detail": "Request body must be valid JSON."}), 400

    errors = _validate_export_json(data)
    if errors:
        return jsonify({"error": "validation_error", "detail": "; ".join(errors)}), 400

    comp, warnings = _import_competition_from_json(data)
    db.session.commit()

    return json_ok(
        {
            "ok": True,
            "competition_id": comp.id,
            "competition_name": comp.name,
            "warnings": warnings,
        },
        status=201,
    )


@transfer_api_bp.post("/api/competition/<int:comp_id>/merge")
@json_roles_required("admin")
def merge_competition(comp_id: int):
    comp = Competition.query.get(comp_id)
    if not comp:
        return jsonify({"error": "not_found"}), 404

    # Accept JSON body or file upload
    if request.content_type and "multipart" in request.content_type:
        file = request.files.get("file")
        if not file:
            return jsonify({"error": "invalid_request", "detail": "No file uploaded."}), 400
        try:
            data = json.loads(file.read().decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            return jsonify({"error": "invalid_json", "detail": str(exc)}), 400
    else:
        data = request.get_json(silent=True)
        if not data:
            return jsonify({"error": "invalid_json", "detail": "Request body must be valid JSON."}), 400

    errors = _validate_export_json(data)
    if errors:
        return jsonify({"error": "validation_error", "detail": "; ".join(errors)}), 400

    warnings = []
    version = data.get("schema_version", "")
    if version != SCHEMA_VERSION:
        warnings.append(f"Schema version mismatch: file has '{version}', current is '{SCHEMA_VERSION}'.")

    resolutions = data.get("resolutions")

    if resolutions is None:
        # Step 1: Dry run — detect conflicts
        conflicts = _find_conflicts(data, comp)
        return json_ok(
            {
                "ok": True,
                "dry_run": True,
                "conflicts": conflicts,
                "warnings": warnings,
            }
        )

    # Step 2: Apply merge with resolutions
    if not isinstance(resolutions, dict):
        return jsonify({"error": "validation_error", "detail": "resolutions must be an object."}), 400

    summary = _apply_merge(data, comp, resolutions)
    db.session.commit()

    return json_ok(
        {
            "ok": True,
            "dry_run": False,
            "summary": summary,
            "warnings": warnings,
        }
    )
