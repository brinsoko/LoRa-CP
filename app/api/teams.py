from __future__ import annotations

import random
import re
from collections.abc import Iterable

from flask import Blueprint, current_app, jsonify, request
from flask_babel import gettext as _
from flask_login import current_user
from sqlalchemy import or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload

from app.api.helpers import json_ok
from app.extensions import db
from app.models import CheckpointGroup, Team, TeamGroup, TeamMember
from app.utils.audit import record_audit_event
from app.utils.competition import get_current_competition_role, require_current_competition_id
from app.utils.rest_auth import json_login_required, json_roles_required
from app.utils.sheets_sync import sync_all_checkpoint_tabs
from app.utils.sheets_sync_worker import enqueue_sync_all_checkpoint_tabs
from app.utils.validators import validate_finite_float, validate_text


def _dispatch_sync_all(comp_id: int) -> None:
    """Hand off the per-CP team-number refresh. Inline in tests so existing
    assertions hold; async in prod so a slow Sheets API (throttle, 429,
    network) can't tie up the gunicorn worker handling the team write."""
    if current_app.config.get("SHEETS_SYNC_INLINE"):
        try:
            sync_all_checkpoint_tabs(competition_id=comp_id)
        except Exception:
            pass
        return
    try:
        enqueue_sync_all_checkpoint_tabs(current_app._get_current_object(), competition_id=comp_id)
    except Exception:
        pass


teams_api_bp = Blueprint("api_teams", __name__)


def _apply_members(team: Team, members_payload) -> None:
    """Replace team.members with the given list.

    Each item may be a plain string (treated as the member's name) or a
    dict {"name": str, "role": str?}. Items with an empty name are
    dropped. Position is assigned by submission order so the UI list
    stays stable.

    The uq_team_member_position constraint blocks two members from
    claiming the same slot. To avoid a transient duplicate when the
    payload reorders members, we delete the existing rows and flush
    before attaching the new collection.
    """
    if members_payload is None:
        return
    if not isinstance(members_payload, list):
        return
    parsed: list[tuple[str, str | None]] = []
    for item in members_payload:
        if isinstance(item, str):
            name, role = item.strip(), None
        elif isinstance(item, dict):
            name = (item.get("name") or "").strip()
            raw_role = item.get("role")
            role = (raw_role.strip() if isinstance(raw_role, str) else None) or None
        else:
            continue
        if not name:
            continue
        parsed.append((name[:160], role[:80] if role else None))
    for existing in list(team.members):
        db.session.delete(existing)
    db.session.flush()
    team.members = [TeamMember(name=n, role=r, position=idx) for idx, (n, r) in enumerate(parsed)]


def _serialize_team(team: Team) -> dict:
    return {
        "id": team.id,
        "name": team.name,
        "number": team.number,
        "organization": team.organization,
        "dnf": bool(team.dnf),
        "notes": team.notes or "",
        "bonus_dead_time": float(team.bonus_dead_time or 0),
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
        "members": [
            {"id": m.id, "name": m.name, "role": m.role, "position": m.position}
            for m in sorted(team.members or [], key=lambda m: m.position)
        ],
    }


def _team_snapshot(team: Team) -> dict:
    return _serialize_team(team)


def _parse_group_ids(raw_ids: Iterable) -> list[int]:
    ids: list[int] = []
    for value in raw_ids or []:
        try:
            number = int(value)
            if number > 0:
                ids.append(number)
        except Exception:
            continue
    return ids


def _apply_group_assignment(team: Team, selected_group_id: int | None) -> tuple[bool, str | None]:
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
        # Drop links to other groups via orphan-delete cascade. Flush
        # immediately so the DELETEs land before the INSERT below;
        # otherwise the partial unique index uq_team_group_one_active
        # would see two active rows for this team mid-flush.
        team.group_assignments[:] = [link for link in team.group_assignments if link.group_id == selected_group_id]
        db.session.flush()

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
@json_login_required
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
    if "notes" in payload:
        raw_notes = payload.get("notes")
        if raw_notes is None:
            team.notes = None
        else:
            notes_value, notes_error = validate_text(raw_notes, field_name="notes", max_length=2000, multiline=True)
            if notes_error:
                return jsonify({"error": "validation_error", "detail": notes_error}), 400
            team.notes = notes_value or None
    if "bonus_dead_time" in payload and (get_current_competition_role() or "") == "admin":
        bonus_value, bonus_error = validate_finite_float(
            payload.get("bonus_dead_time"),
            field_name="bonus_dead_time",
            minimum=0.0,
        )
        if bonus_error:
            return jsonify({"error": "validation_error", "detail": bonus_error}), 400
        team.bonus_dead_time = bonus_value if bonus_value is not None else 0.0
    if number is not None:
        try:
            num_val = int(number)
        except Exception:
            return jsonify({"error": "validation_error", "detail": _("Team number must be a positive integer.")}), 400
        if num_val <= 0:
            return jsonify({"error": "validation_error", "detail": _("Team number must be a positive integer.")}), 400
        team.number = num_val

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

    _apply_members(team, payload.get("members"))

    try:
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
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "validation_error", "detail": _("Team number must be a positive integer.")}), 400
    _dispatch_sync_all(comp_id)
    return json_ok({"ok": True, "team": _serialize_team(team)}, status=201)


def _team_for_competition(comp_id: int, team_id: int, with_groups: bool = True) -> Team | None:
    query = _team_query(comp_id).filter(Team.id == team_id)
    if with_groups:
        query = query.options(joinedload(Team.group_assignments).joinedload(TeamGroup.group))
    return query.first()


@teams_api_bp.get("/api/teams/<int:team_id>")
@json_login_required
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
    team = _team_query(comp_id).filter(Team.id == team_id).options(joinedload(Team.group_assignments)).first()
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
                num_val = int(number)
            except Exception:
                return jsonify(
                    {"error": "validation_error", "detail": _("Team number must be a positive integer.")}
                ), 400
            if num_val <= 0:
                return jsonify(
                    {"error": "validation_error", "detail": _("Team number must be a positive integer.")}
                ), 400
            team.number = num_val

    if "organization" in payload or not partial:
        org, org_error = validate_text(payload.get("organization"), field_name="organization", max_length=120)
        if org_error:
            return jsonify({"error": "validation_error", "detail": org_error}), 400
        team.organization = org or None

    if "dnf" in payload:
        if (get_current_competition_role() or "") != "admin":
            return jsonify({"error": "forbidden", "detail": "dnf requires admin role"}), 403
        team.dnf = bool(payload.get("dnf"))

    if "notes" in payload:
        raw_notes = payload.get("notes")
        if raw_notes is None:
            team.notes = None
        else:
            notes_value, notes_error = validate_text(raw_notes, field_name="notes", max_length=2000, multiline=True)
            if notes_error:
                return jsonify({"error": "validation_error", "detail": notes_error}), 400
            team.notes = notes_value or None

    if "bonus_dead_time" in payload:
        # Admin-only field. Silently drop for non-admins so a judge edit
        # of an unrelated field doesn't 403 the whole request.
        if (get_current_competition_role() or "") == "admin":
            bonus_value, bonus_error = validate_finite_float(
                payload.get("bonus_dead_time"),
                field_name="bonus_dead_time",
                minimum=0.0,
            )
            if bonus_error:
                return jsonify({"error": "validation_error", "detail": bonus_error}), 400
            team.bonus_dead_time = bonus_value if bonus_value is not None else 0.0

    change_group = False
    selected_group_id: int | None = None

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

    if "members" in payload:
        _apply_members(team, payload.get("members"))

    try:
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
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "validation_error", "detail": _("Team number must be a positive integer.")}), 400
    _dispatch_sync_all(comp_id)
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

        # Get all active teams in this group
        all_group_teams = (
            Team.query.join(TeamGroup, TeamGroup.team_id == Team.id)
            .filter(
                Team.competition_id == comp_id,
                TeamGroup.group_id == group.id,
                TeamGroup.active.is_(True),
            )
            .all()
        )
        total_teams = len(all_group_teams)
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

        # The usable range is start+1 .. start+total_teams (capped at end).
        # E.g. 3xx with 3 teams → 301-303; with 100 teams → 301-399.
        range_start = start + 1
        range_end = min(end, start + total_teams)

        # Teams that already have a number within the range don't need a new one.
        # Teams with no number or a number outside the range need assignment.
        teams_needing_number = []
        for team in all_group_teams:
            if team.number is not None and range_start <= team.number <= range_end:
                continue  # already has a valid number in range
            teams_needing_number.append(team)

        needed = len(teams_needing_number)
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

        # Find all numbers already used in the range across the competition
        used_numbers = (
            db.session.query(Team.number)
            .filter(Team.competition_id == comp_id)
            .filter(Team.number.isnot(None))
            .filter(Team.number >= range_start, Team.number <= range_end)
            .all()
        )
        used_set = {n[0] for n in used_numbers}

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
        for team, number in zip(teams_needing_number, available, strict=False):
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
