# app/api/transfer.py
"""Export / Import / Merge competition data as JSON."""
from __future__ import annotations

import json
from datetime import datetime

from flask import Blueprint, jsonify, request, make_response
from flask_babel import gettext as _
from flask_login import current_user

from app.api.helpers import json_ok
from app.extensions import db
from app.models import (
    Checkin,
    Checkpoint,
    CheckpointGroup,
    CheckpointGroupLink,
    Competition,
    CompetitionMember,
    LoRaDevice,
    RFIDCard,
    ScoreEntry,
    Team,
    TeamGroup,
)
from app.utils.competition import require_current_competition_id
from app.utils.rest_auth import json_roles_required

transfer_api_bp = Blueprint("api_transfer", __name__)

SCHEMA_VERSION = "1.0.0"


# ---- serialisation helpers ----

def _export_competition(comp: Competition) -> dict:
    """Build the full export payload for a competition."""
    teams = Team.query.filter_by(competition_id=comp.id).all()
    groups = CheckpointGroup.query.filter_by(competition_id=comp.id).all()
    checkpoints = Checkpoint.query.filter_by(competition_id=comp.id).all()
    checkins = Checkin.query.filter_by(competition_id=comp.id).all()
    devices = LoRaDevice.query.filter_by(competition_id=comp.id).all()
    scores = ScoreEntry.query.filter_by(competition_id=comp.id).all()
    rfid_cards = (
        RFIDCard.query
        .join(Team, RFIDCard.team_id == Team.id)
        .filter(Team.competition_id == comp.id)
        .all()
    )
    team_groups = (
        TeamGroup.query
        .join(Team, TeamGroup.team_id == Team.id)
        .filter(Team.competition_id == comp.id)
        .all()
    )
    group_links = (
        CheckpointGroupLink.query
        .join(CheckpointGroup, CheckpointGroupLink.group_id == CheckpointGroup.id)
        .filter(CheckpointGroup.competition_id == comp.id)
        .all()
    )

    return {
        "schema_version": SCHEMA_VERSION,
        "exported_at": datetime.utcnow().isoformat() + "Z",
        "competition": {
            "name": comp.name,
            "settings": {
                "public_results": comp.public_results,
                "hide_gps_map": comp.hide_gps_map,
            },
        },
        "teams": [
            {
                "name": t.name,
                "number": t.number,
                "organization": t.organization,
                "dnf": bool(t.dnf),
            }
            for t in teams
        ],
        "groups": [
            {
                "name": g.name,
                "prefix": g.prefix,
                "description": g.description,
                "position": g.position,
            }
            for g in groups
        ],
        "checkpoints": [
            {
                "name": cp.name,
                "location": cp.location,
                "description": cp.description,
                "easting": cp.easting,
                "northing": cp.northing,
            }
            for cp in checkpoints
        ],
        "checkins": [
            {
                "team_name": c.team.name if c.team else None,
                "checkpoint_name": c.checkpoint.name if c.checkpoint else None,
                "timestamp": c.timestamp.isoformat() if c.timestamp else None,
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
            }
            for s in scores
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
        "group_checkpoint_links": [
            {
                "group_name": gl.group.name if gl.group else None,
                "checkpoint_name": gl.checkpoint.name if gl.checkpoint else None,
                "position": gl.position,
            }
            for gl in group_links
        ],
    }


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
        warnings.append(
            f"Schema version mismatch: file has '{version}', current is '{SCHEMA_VERSION}'."
        )

    comp_data = data.get("competition", {})
    comp_name = comp_data.get("name", "Imported Competition")
    # Ensure unique name
    existing = Competition.query.filter_by(name=comp_name).first()
    if existing:
        comp_name = f"{comp_name} (imported {datetime.utcnow().strftime('%Y%m%d-%H%M%S')})"

    settings = comp_data.get("settings", {})
    comp = Competition(
        name=comp_name,
        public_results=settings.get("public_results", False),
        hide_gps_map=settings.get("hide_gps_map", False),
        created_by_user_id=current_user.id if current_user.is_authenticated else None,
    )
    db.session.add(comp)
    db.session.flush()

    # Add current user as admin member
    if current_user.is_authenticated:
        db.session.add(CompetitionMember(
            competition_id=comp.id,
            user_id=current_user.id,
            role="admin",
            active=True,
        ))

    # Groups
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

    # Group ↔ checkpoint links
    for gl_data in data.get("group_checkpoint_links", []):
        group = group_map.get(gl_data.get("group_name"))
        cp = cp_map.get(gl_data.get("checkpoint_name"))
        if group and cp:
            db.session.add(CheckpointGroupLink(
                group_id=group.id,
                checkpoint_id=cp.id,
                position=gl_data.get("position", 0),
            ))

    # Team ↔ group assignments
    for tg_data in data.get("team_groups", []):
        team = team_map.get(tg_data.get("team_name"))
        group = group_map.get(tg_data.get("group_name"))
        if team and group:
            db.session.add(TeamGroup(
                team_id=team.id,
                group_id=group.id,
                active=tg_data.get("active", True),
            ))

    # RFID cards
    for card_data in data.get("rfid_cards", []):
        team = team_map.get(card_data.get("team_name"))
        if team:
            uid = card_data.get("uid", "")
            existing_uid = RFIDCard.query.filter_by(uid=uid).first()
            if not existing_uid:
                db.session.add(RFIDCard(
                    uid=uid,
                    team_id=team.id,
                    number=card_data.get("number"),
                ))

    # Check-ins
    for ci_data in data.get("checkins", []):
        team = team_map.get(ci_data.get("team_name"))
        cp = cp_map.get(ci_data.get("checkpoint_name"))
        if team and cp:
            ts = None
            if ci_data.get("timestamp"):
                try:
                    ts = datetime.fromisoformat(ci_data["timestamp"])
                except Exception:
                    ts = datetime.utcnow()
            db.session.add(Checkin(
                competition_id=comp.id,
                team_id=team.id,
                checkpoint_id=cp.id,
                timestamp=ts or datetime.utcnow(),
            ))

    # Scores
    db.session.flush()
    for s_data in data.get("scores", []):
        team = team_map.get(s_data.get("team_name"))
        cp = cp_map.get(s_data.get("checkpoint_name"))
        if team and cp:
            checkin = Checkin.query.filter_by(
                team_id=team.id, checkpoint_id=cp.id, competition_id=comp.id,
            ).first()
            db.session.add(ScoreEntry(
                competition_id=comp.id,
                checkin_id=checkin.id if checkin else None,
                team_id=team.id,
                checkpoint_id=cp.id,
                raw_fields=s_data.get("raw_fields"),
                total=s_data.get("total"),
            ))

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
                conflicts.append({
                    "entity_type": "team",
                    "identifier": name,
                    "local": {"name": local.name, "number": local.number, "organization": local.organization},
                    "imported": t_data,
                    "differences": diffs,
                })

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
                conflicts.append({
                    "entity_type": "checkpoint",
                    "identifier": name,
                    "local": {"name": local.name, "easting": local.easting, "northing": local.northing},
                    "imported": cp_data,
                    "differences": diffs,
                })

    existing_groups = {g.name: g for g in CheckpointGroup.query.filter_by(competition_id=comp.id).all()}
    for g_data in data.get("groups", []):
        name = g_data.get("name")
        if name in existing_groups:
            local = existing_groups[name]
            diffs = {}
            if (g_data.get("prefix") or None) != (local.prefix or None):
                diffs["prefix"] = {"local": local.prefix, "imported": g_data.get("prefix")}
            if diffs:
                conflicts.append({
                    "entity_type": "group",
                    "identifier": name,
                    "local": {"name": local.name, "prefix": local.prefix},
                    "imported": g_data,
                    "differences": diffs,
                })

    return conflicts


def _apply_merge(data: dict, comp: Competition, resolutions: dict) -> dict:
    """Apply merge with conflict resolutions. Returns summary."""
    added = {"teams": 0, "checkpoints": 0, "groups": 0, "checkins": 0}
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
                updated["checkpoints"] += 1
            elif action == "skip":
                skipped += 1
        else:
            cp = Checkpoint(
                competition_id=comp.id,
                name=name,
                location=cp_data.get("location"),
                description=cp_data.get("description"),
                easting=cp_data.get("easting"),
                northing=cp_data.get("northing"),
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
            team_map[name] = t
            added["teams"] += 1

    # Check-ins (add only new ones, matched by team+checkpoint)
    for ci_data in data.get("checkins", []):
        team = team_map.get(ci_data.get("team_name"))
        cp = cp_map.get(ci_data.get("checkpoint_name"))
        if team and cp:
            existing = Checkin.query.filter_by(
                team_id=team.id, checkpoint_id=cp.id, competition_id=comp.id,
            ).first()
            if not existing:
                ts = datetime.utcnow()
                if ci_data.get("timestamp"):
                    try:
                        ts = datetime.fromisoformat(ci_data["timestamp"])
                    except Exception:
                        pass
                db.session.add(Checkin(
                    competition_id=comp.id,
                    team_id=team.id,
                    checkpoint_id=cp.id,
                    timestamp=ts,
                ))
                added["checkins"] += 1

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

    return json_ok({
        "ok": True,
        "competition_id": comp.id,
        "competition_name": comp.name,
        "warnings": warnings,
    }, status=201)


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
        warnings.append(
            f"Schema version mismatch: file has '{version}', current is '{SCHEMA_VERSION}'."
        )

    resolutions = data.get("resolutions")

    if resolutions is None:
        # Step 1: Dry run — detect conflicts
        conflicts = _find_conflicts(data, comp)
        return json_ok({
            "ok": True,
            "dry_run": True,
            "conflicts": conflicts,
            "warnings": warnings,
        })

    # Step 2: Apply merge with resolutions
    if not isinstance(resolutions, dict):
        return jsonify({"error": "validation_error", "detail": "resolutions must be an object."}), 400

    summary = _apply_merge(data, comp, resolutions)
    db.session.commit()

    return json_ok({
        "ok": True,
        "dry_run": False,
        "summary": summary,
        "warnings": warnings,
    })
