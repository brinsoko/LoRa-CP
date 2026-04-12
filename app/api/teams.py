from __future__ import annotations

import random
import re
from typing import Iterable, List, Optional

from flask import Blueprint, jsonify, request
from flask_login import current_user
from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from app.api.helpers import json_ok
from app.extensions import db
from app.models import CheckpointGroup, Team, TeamGroup
from app.utils.audit import record_audit_event
from app.utils.competition import get_current_competition_role, require_current_competition_id
from app.utils.rest_auth import json_roles_required
from app.utils.sheets_sync import sync_all_checkpoint_tabs
from app.utils.validators import validate_text

teams_api_bp = Blueprint("api_teams", __name__)


def _serialize_team(team: Team) -> dict:
    return {
        "id": team.id,
        "name": team.name,
        "number": team.number,
        "organization": team.organization,
        "dnf": bool(team.dnf),
        "checkins_count": len(team.checkins or []),
        "groups": [
            {
                "id": tg.group_id,
                "name": tg.group.name if tg.group else None,
                "active": bool(tg.active),
            }
            for tg in sorted(
                team.group_assignments,
                key=lambda tg: (0 if tg.active else 1, tg.group.name if tg.group else ""),
            )
        ],
    }


def _team_snapshot(team: Team) -> dict:
    return _serialize_team(team)


def _parse_group_ids(raw_ids: Iterable) -> List[int]:
    ids: List[int] = []
    for value in raw_ids or []:
        try:
            number = int(value)
            if number > 0:
                ids.append(number)
        except Exception:
            continue
    return ids


def _apply_group_assignment(team: Team, selected_group_id: Optional[int]) -> tuple[bool, Optional[str]]:
    existing_links = {tg.group_id: tg for tg in list(team.group_assignments)}

    if selected_group_id is None:
        if existing_links:
            team.group_assignments[:] = []
        return True, None

    try:
        selected_group_id = int(selected_group_id)
        if selected_group_id <= 0:
            raise ValueError
    except Exception:
        return False, "group_id must be a positive integer"

    group = db.session.get(CheckpointGroup, selected_group_id)
    if not group:
        return False, "Invalid group"
    if group.competition_id != team.competition_id:
        return False, "Invalid group for this competition"

    to_remove = [gid for gid in existing_links if gid != selected_group_id]
    if to_remove:
        team.group_assignments[:] = [link for link in team.group_assignments if link.group_id == selected_group_id]

    link = next((assignment for assignment in team.group_assignments if assignment.group_id == selected_group_id), None)
    if link is None:
        link = TeamGroup(group_id=selected_group_id, active=True)
        team.group_assignments.append(link)
    else:
        link.active = True

    return True, None


def _team_query(comp_id: int):
    return Team.query.filter(Team.competition_id == comp_id)


@teams_api_bp.get("/api/teams")
def team_list():
    q = (request.args.get("q") or "").strip()
    group_id = request.args.get("group_id", type=int)
    sort = (request.args.get("sort") or "name_asc").strip().lower()

    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400

    query = _team_query(comp_id).options(joinedload(Team.group_assignments).joinedload(TeamGroup.group))

    if q:
        like = f"%{q.replace('*', '%')}%"
        query = query.filter(
            or_(
                Team.name.ilike(like),
                Team.organization.ilike(like),
                Team.number.cast(db.String).ilike(like),
            )
        )

    if group_id:
        query = query.join(TeamGroup, TeamGroup.team_id == Team.id).filter(TeamGroup.group_id == group_id)

    if sort == "name_desc":
        query = query.order_by(Team.name.desc())
    elif sort == "number_asc":
        query = query.order_by(Team.number.asc().nulls_last(), Team.name.asc())
    elif sort == "number_desc":
        query = query.order_by(Team.number.desc().nulls_last(), Team.name.asc())
    else:
        query = query.order_by(Team.name.asc())

    rows = query.all()
    return json_ok(
        {
            "teams": [_serialize_team(t) for t in rows],
            "meta": {
                "total": len(rows),
                "filters": {"q": q, "group_id": group_id, "sort": sort},
            },
        }
    )


@teams_api_bp.post("/api/teams")
@json_roles_required("judge", "admin")
def team_create():
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400

    payload = request.get_json(silent=True) or {}
    name, name_error = validate_text(payload.get("name"), field_name="name", max_length=100, required=True)
    number = payload.get("number", None)
    org, org_error = validate_text(payload.get("organization"), field_name="organization", max_length=120)
    if name_error:
        return jsonify({"error": "validation_error", "detail": name_error}), 400
    if org_error:
        return jsonify({"error": "validation_error", "detail": org_error}), 400

    team = Team(name=name, organization=org, competition_id=comp_id)
    if "dnf" in payload:
        if (get_current_competition_role() or "") != "admin":
            return jsonify({"error": "forbidden", "detail": "dnf requires admin role"}), 403
        team.dnf = bool(payload.get("dnf"))
    if number is not None:
        try:
            team.number = int(number)
        except Exception:
            return jsonify({"error": "validation_error", "detail": "number must be integer"}), 400

    db.session.add(team)
    db.session.flush()

    group_id_value = payload.get("group_id", None)
    if group_id_value in (None, ""):
        active_candidate = payload.get("active_group_id", None)
        if active_candidate not in (None, ""):
            group_id_value = active_candidate
    group_ids_list = _parse_group_ids(payload.get("group_ids"))

    if group_id_value not in (None, "") and group_ids_list:
        return jsonify({"error": "validation_error", "detail": "Use only one group reference."}), 400

    if group_id_value not in (None, ""):
        try:
            selected_group_id = int(group_id_value)
        except Exception:
            return jsonify({"error": "validation_error", "detail": "group_id must be integer"}), 400
    elif group_ids_list:
        if len(group_ids_list) > 1:
            return jsonify({"error": "validation_error", "detail": "Teams can belong to only one group."}), 400
        selected_group_id = group_ids_list[0]
    else:
        selected_group_id = None

    if selected_group_id is not None:
        ok, err = _apply_group_assignment(team, selected_group_id)
        if not ok:
            db.session.rollback()
            return jsonify({"error": "validation_error", "detail": err}), 400

    db.session.flush()
    record_audit_event(
        competition_id=comp_id,
        event_type="team_created",
        entity_type="team",
        entity_id=team.id,
        actor_user=current_user if current_user.is_authenticated else None,
        summary=f"Team {team.name} created.",
        details=_team_snapshot(team),
    )
    db.session.commit()
    try:
        sync_all_checkpoint_tabs(competition_id=comp_id)
    except Exception:
        pass
    return json_ok({"ok": True, "team": _serialize_team(team)}, status=201)


def _team_for_competition(comp_id: int, team_id: int, with_groups: bool = True) -> Team | None:
    query = _team_query(comp_id).filter(Team.id == team_id)
    if with_groups:
        query = query.options(joinedload(Team.group_assignments).joinedload(TeamGroup.group))
    return query.first()


@teams_api_bp.get("/api/teams/<int:team_id>")
def team_get(team_id: int):
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    team = _team_for_competition(comp_id, team_id)
    if not team:
        return jsonify({"error": "not_found"}), 404
    return json_ok(_serialize_team(team))


def _update_team(team_id: int, partial: bool):
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    team = (
        _team_query(comp_id)
        .filter(Team.id == team_id)
        .options(joinedload(Team.group_assignments))
        .first()
    )
    if not team:
        return jsonify({"error": "not_found"}), 404
    before = _team_snapshot(team)

    payload = request.get_json(silent=True) or {}

    if not partial or "name" in payload:
        name, name_error = validate_text(payload.get("name"), field_name="name", max_length=100, required=True)
        if name_error:
            return jsonify({"error": "validation_error", "detail": name_error}), 400
        team.name = name

    if "number" in payload or not partial:
        number = payload.get("number")
        if number is None or number == "":
            team.number = None
        else:
            try:
                team.number = int(number)
            except Exception:
                return jsonify({"error": "validation_error", "detail": "number must be integer"}), 400

    if "organization" in payload or not partial:
        org, org_error = validate_text(payload.get("organization"), field_name="organization", max_length=120)
        if org_error:
            return jsonify({"error": "validation_error", "detail": org_error}), 400
        team.organization = org or None

    if "dnf" in payload:
        if (get_current_competition_role() or "") != "admin":
            return jsonify({"error": "forbidden", "detail": "dnf requires admin role"}), 403
        team.dnf = bool(payload.get("dnf"))

    change_group = False
    selected_group_id: Optional[int] = None

    if "group_id" in payload:
        change_group = True
        raw = payload.get("group_id")
        if raw in (None, ""):
            selected_group_id = None
        else:
            try:
                selected_group_id = int(raw)
            except Exception:
                return jsonify({"error": "validation_error", "detail": "group_id must be integer"}), 400
    elif "active_group_id" in payload:
        change_group = True
        raw = payload.get("active_group_id")
        if raw in (None, ""):
            selected_group_id = None
        else:
            try:
                selected_group_id = int(raw)
            except Exception:
                return jsonify({"error": "validation_error", "detail": "active_group_id must be integer"}), 400
    elif "group_ids" in payload:
        change_group = True
        ids = _parse_group_ids(payload.get("group_ids"))
        if len(ids) > 1:
            return jsonify({"error": "validation_error", "detail": "Teams can belong to only one group."}), 400
        selected_group_id = ids[0] if ids else None
    elif not partial:
        change_group = True
        selected_group_id = None

    if change_group:
        ok, err = _apply_group_assignment(team, selected_group_id)
        if not ok:
            db.session.rollback()
            return jsonify({"error": "validation_error", "detail": err}), 400

    db.session.flush()
    record_audit_event(
        competition_id=comp_id,
        event_type="team_updated",
        entity_type="team",
        entity_id=team.id,
        actor_user=current_user if current_user.is_authenticated else None,
        summary=f"Team {team.name} updated.",
        details={"before": before, "after": _team_snapshot(team)},
    )
    db.session.commit()
    try:
        sync_all_checkpoint_tabs(competition_id=comp_id)
    except Exception:
        pass
    return json_ok({"ok": True, "team": _serialize_team(team)})


@teams_api_bp.patch("/api/teams/<int:team_id>")
@json_roles_required("judge", "admin")
def team_patch(team_id: int):
    return _update_team(team_id, partial=True)


@teams_api_bp.put("/api/teams/<int:team_id>")
@json_roles_required("judge", "admin")
def team_put(team_id: int):
    return _update_team(team_id, partial=False)


@teams_api_bp.delete("/api/teams/<int:team_id>")
@json_roles_required("admin")
def team_delete(team_id: int):
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    team = _team_query(comp_id).filter(Team.id == team_id).first()
    if not team:
        return jsonify({"error": "not_found"}), 404
    snapshot = _team_snapshot(team)
    payload = request.get_json(silent=True) or {}
    force = bool(payload.get("force"))
    confirm_text = (payload.get("confirm_text") or "").strip()
    if team.checkins:
        if not force:
            return jsonify({"error": "conflict", "detail": "Cannot delete team with existing check-ins."}), 409
        if confirm_text != "Delete":
            return jsonify({"error": "validation_error", "detail": "Type Delete to confirm deletion."}), 400

    record_audit_event(
        competition_id=comp_id,
        event_type="team_deleted",
        entity_type="team",
        entity_id=team.id,
        actor_user=current_user if current_user.is_authenticated else None,
        summary=f"Team {team.name} deleted.",
        details=snapshot,
    )
    db.session.delete(team)
    db.session.commit()
    return json_ok({"ok": True})


@teams_api_bp.post("/api/teams/<int:team_id>/active-group")
@json_roles_required("judge", "admin")
def team_active_group(team_id: int):
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    payload = request.get_json(silent=True) or {}
    group_id = payload.get("group_id")
    try:
        group_id = int(group_id)
    except Exception:
        return jsonify({"error": "validation_error", "detail": "group_id must be integer"}), 400

    team = _team_query(comp_id).filter(Team.id == team_id).first()
    if not team:
        return jsonify({"error": "not_found"}), 404

    ok, err = _apply_group_assignment(team, group_id)
    if not ok:
        return jsonify({"error": "validation_error", "detail": err}), 400

    db.session.flush()
    record_audit_event(
        competition_id=comp_id,
        event_type="team_group_updated",
        entity_type="team",
        entity_id=team.id,
        actor_user=current_user if current_user.is_authenticated else None,
        summary=f"Active group changed for team {team.name}.",
        details=_team_snapshot(team),
    )
    db.session.commit()
    return json_ok({"ok": True})


def _parse_group_prefix(prefix: str) -> tuple[int, int] | None:
    if not prefix:
        return None
    text = prefix.strip().lower()
    match = re.match(r"^(\d+)(x+)$", text)
    if not match:
        return None
    base = int(match.group(1))
    width = len(match.group(2))
    start = base * (10**width)
    end = start + (10**width) - 1
    return start, end


@teams_api_bp.post("/api/teams/randomize")
@json_roles_required("judge", "admin")
def team_randomize_numbers():
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400

    payload = request.get_json(silent=True) or {}
    group_id = payload.get("group_id")

    groups_query = CheckpointGroup.query.filter(CheckpointGroup.competition_id == comp_id)
    if group_id not in (None, ""):
        try:
            group_id = int(group_id)
        except Exception:
            return jsonify({"error": "validation_error", "detail": "group_id must be integer"}), 400
        groups_query = groups_query.filter(CheckpointGroup.id == group_id)
    groups = groups_query.all()
    if group_id not in (None, "") and not groups:
        return jsonify({"error": "not_found", "detail": "group not found"}), 404

    results = []
    assigned_total = 0

    for group in groups:
        prefix = (group.prefix or "").strip()
        parsed = _parse_group_prefix(prefix)
        if not parsed:
            results.append(
                {
                    "group_id": group.id,
                    "group_name": group.name,
                    "status": "skipped",
                    "detail": "invalid_prefix",
                }
            )
            continue

        start, end = parsed
        total_teams = (
            Team.query.join(TeamGroup, TeamGroup.team_id == Team.id)
            .filter(
                Team.competition_id == comp_id,
                TeamGroup.group_id == group.id,
                TeamGroup.active.is_(True),
            )
            .count()
        )
        if total_teams <= 0:
            results.append(
                {
                    "group_id": group.id,
                    "group_name": group.name,
                    "status": "no_op",
                    "detail": "no_teams_in_group",
                }
            )
            continue
        range_start = start + 1
        range_end = min(end, start + total_teams)
        used_numbers = (
            db.session.query(Team.number)
            .filter(Team.competition_id == comp_id)
            .filter(Team.number.isnot(None))
            .filter(Team.number >= range_start, Team.number <= range_end)
            .all()
        )
        used_set = {n[0] for n in used_numbers}

        teams = (
            Team.query.join(TeamGroup, TeamGroup.team_id == Team.id)
            .filter(
                Team.competition_id == comp_id,
                TeamGroup.group_id == group.id,
                TeamGroup.active.is_(True),
                Team.number.is_(None),
            )
            .all()
        )
        needed = len(teams)
        if needed == 0:
            results.append(
                {
                    "group_id": group.id,
                    "group_name": group.name,
                    "status": "no_op",
                    "detail": "no_unnumbered_teams",
                }
            )
            continue

        available = [n for n in range(range_start, range_end + 1) if n not in used_set]
        if len(available) < needed:
            results.append(
                {
                    "group_id": group.id,
                    "group_name": group.name,
                    "status": "insufficient_numbers",
                    "needed": needed,
                    "available": len(available),
                    "range": [range_start, range_end],
                }
            )
            continue

        random.shuffle(available)
        for team, number in zip(teams, available):
            team.number = number
        assigned_total += needed
        results.append(
            {
                "group_id": group.id,
                "group_name": group.name,
                "status": "assigned",
                "assigned": needed,
                "range": [range_start, range_end],
            }
        )

    if assigned_total:
        record_audit_event(
            competition_id=comp_id,
            event_type="team_numbers_randomized",
            entity_type="team_batch",
            entity_id=None,
            actor_user=current_user if current_user.is_authenticated else None,
            summary="Team numbers randomized.",
            details={"assigned_total": assigned_total, "results": results},
        )
        db.session.commit()
    return json_ok({"ok": True, "assigned_total": assigned_total, "results": results})
