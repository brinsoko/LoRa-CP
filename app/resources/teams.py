# app/resources/teams.py
from __future__ import annotations

from typing import Iterable, List, Optional
import random
import re

from flask import request
from flask_restful import Resource
from sqlalchemy import or_
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import Team, TeamGroup, CheckpointGroup
from app.utils.rest_auth import json_roles_required
from app.utils.competition import require_current_competition_id, get_current_competition_role
from app.utils.sheets_sync import sync_all_checkpoint_tabs


def _serialize_team(team: Team) -> dict:
    """Serialize a Team with its group assignments."""
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


def _parse_group_ids(raw_ids: Iterable) -> List[int]:
    ids: List[int] = []
    for value in raw_ids or []:
        try:
            n = int(value)
            if n > 0:
                ids.append(n)
        except Exception:
            continue
    return ids


def _apply_group_assignment(team: Team, selected_group_id: Optional[int]) -> tuple[bool, Optional[str]]:
    """Ensure the team is linked to at most one group."""
    existing_links = {tg.group_id: tg for tg in team.group_assignments}

    if selected_group_id is None:
        if existing_links:
            (db.session.query(TeamGroup)
             .filter(TeamGroup.team_id == team.id)
             .delete(synchronize_session=False))
            team.group_assignments[:] = []
        return True, None

    try:
        selected_group_id = int(selected_group_id)
        if selected_group_id <= 0:
            raise ValueError
    except Exception:
        return False, "group_id must be a positive integer"

    group = CheckpointGroup.query.get(selected_group_id)
    if not group:
        return False, "Invalid group"
    if group.competition_id != team.competition_id:
        return False, "Invalid group for this competition"

    to_remove = [gid for gid in existing_links if gid != selected_group_id]
    if to_remove:
        (db.session.query(TeamGroup)
         .filter(TeamGroup.team_id == team.id, TeamGroup.group_id.in_(to_remove))
         .delete(synchronize_session=False))
        team.group_assignments[:] = [link for link in team.group_assignments if link.group_id == selected_group_id]

    link = existing_links.get(selected_group_id)
    if link is None:
        link = TeamGroup(team_id=team.id, group_id=selected_group_id, active=True)
        db.session.add(link)
        team.group_assignments.append(link)
    else:
        link.active = True

    return True, None


class TeamListResource(Resource):

    def get(self):
        """
        Returns paginated list of teams with optional filters:
          - q: substring match on name or number
          - group_id: filter by assigned group id
          - sort: name_asc|name_desc|number_asc|number_desc (default name_asc)
        """
        q = (request.args.get("q") or "").strip()
        group_id = request.args.get("group_id", type=int)
        sort = (request.args.get("sort") or "name_asc").strip().lower()

        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400

        query = (
            Team.query
            .filter(Team.competition_id == comp_id)
            .options(joinedload(Team.group_assignments).joinedload(TeamGroup.group))
        )

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
            query = (
                query.join(TeamGroup, TeamGroup.team_id == Team.id)
                     .filter(TeamGroup.group_id == group_id)
            )

        if sort == "name_desc":
            query = query.order_by(Team.name.desc())
        elif sort == "number_asc":
            query = query.order_by(Team.number.asc().nulls_last(), Team.name.asc())
        elif sort == "number_desc":
            query = query.order_by(Team.number.desc().nulls_last(), Team.name.asc())
        else:
            query = query.order_by(Team.name.asc())

        rows = query.all()
        return {
            "teams": [_serialize_team(t) for t in rows],
            "meta": {
                "total": len(rows),
                "filters": {"q": q, "group_id": group_id, "sort": sort},
            },
        }, 200

    @json_roles_required("judge", "admin")
    @json_roles_required("judge", "admin")
    def post(self):
        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400

        payload = request.get_json(silent=True) or {}
        name = (payload.get("name") or "").strip()
        number = payload.get("number", None)
        org = (payload.get("organization") or "").strip() or None
        if not name:
            return {"error": "validation_error", "detail": "name is required"}, 400

        team = Team(name=name, organization=org, competition_id=comp_id)
        if "dnf" in payload:
            if (get_current_competition_role() or "") != "admin":
                return {"error": "forbidden", "detail": "dnf requires admin role"}, 403
            team.dnf = bool(payload.get("dnf"))
        if number is not None:
            try:
                team.number = int(number)
            except Exception:
                return {"error": "validation_error", "detail": "number must be integer"}, 400

        db.session.add(team)
        db.session.flush()

        group_id_value = payload.get("group_id", None)
        if group_id_value in (None, ""):
            active_candidate = payload.get("active_group_id", None)
            if active_candidate not in (None, ""):
                group_id_value = active_candidate
        group_ids_list = _parse_group_ids(payload.get("group_ids"))

        if group_id_value not in (None, "") and group_ids_list:
            return {"error": "validation_error", "detail": "Use only one group reference."}, 400

        selected_group_id: Optional[int]
        if group_id_value not in (None, ""):
            try:
                selected_group_id = int(group_id_value)
            except Exception:
                return {"error": "validation_error", "detail": "group_id must be integer"}, 400
        elif group_ids_list:
            if len(group_ids_list) > 1:
                return {"error": "validation_error", "detail": "Teams can belong to only one group."}, 400
            selected_group_id = group_ids_list[0]
        else:
            selected_group_id = None

        if selected_group_id is not None:
            ok, err = _apply_group_assignment(team, selected_group_id)
            if not ok:
                db.session.rollback()
                return {"error": "validation_error", "detail": err}, 400

        db.session.flush()

        db.session.commit()
        try:
            sync_all_checkpoint_tabs(competition_id=comp_id)
        except Exception:
            pass
        return {"ok": True, "team": _serialize_team(team)}, 201


class TeamItemResource(Resource):
    def get(self, team_id: int):
        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400
        team = (
            Team.query
            .filter(Team.competition_id == comp_id, Team.id == team_id)
            .options(joinedload(Team.group_assignments).joinedload(TeamGroup.group))
            .first()
        )
        if not team:
            return {"error": "not_found"}, 404
        return _serialize_team(team), 200

    @json_roles_required("judge", "admin")
    def patch(self, team_id: int):
        return self._update(team_id, partial=True)

    @json_roles_required("judge", "admin")
    def put(self, team_id: int):
        return self._update(team_id, partial=False)

    def _update(self, team_id: int, partial: bool):
        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400
        team = (
            Team.query
            .filter(Team.competition_id == comp_id, Team.id == team_id)
            .options(joinedload(Team.group_assignments))
            .first()
        )
        if not team:
            return {"error": "not_found"}, 404

        payload = request.get_json(silent=True) or {}

        if not partial or "name" in payload:
            name = (payload.get("name") or "").strip()
            if not name:
                return {"error": "validation_error", "detail": "name is required"}, 400
            team.name = name

        if "number" in payload or not partial:
            number = payload.get("number")
            if number is None or number == "":
                team.number = None
            else:
                try:
                    team.number = int(number)
                except Exception:
                    return {"error": "validation_error", "detail": "number must be integer"}, 400

        if "organization" in payload or not partial:
            org = (payload.get("organization") or "").strip()
            team.organization = org or None

        if "dnf" in payload:
            if (get_current_competition_role() or "") != "admin":
                return {"error": "forbidden", "detail": "dnf requires admin role"}, 403
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
                    return {"error": "validation_error", "detail": "group_id must be integer"}, 400
        elif "active_group_id" in payload:
            change_group = True
            raw = payload.get("active_group_id")
            if raw in (None, ""):
                selected_group_id = None
            else:
                try:
                    selected_group_id = int(raw)
                except Exception:
                    return {"error": "validation_error", "detail": "active_group_id must be integer"}, 400
        elif "group_ids" in payload:
            change_group = True
            ids = _parse_group_ids(payload.get("group_ids"))
            if len(ids) > 1:
                return {"error": "validation_error", "detail": "Teams can belong to only one group."}, 400
            selected_group_id = ids[0] if ids else None
        elif not partial:
            change_group = True
            selected_group_id = None

        if change_group:
            ok, err = _apply_group_assignment(team, selected_group_id)
            if not ok:
                db.session.rollback()
                return {"error": "validation_error", "detail": err}, 400

        db.session.commit()
        try:
            sync_all_checkpoint_tabs(competition_id=comp_id)
        except Exception:
            pass
        return {"ok": True, "team": _serialize_team(team)}, 200

    @json_roles_required("admin")
    def delete(self, team_id: int):
        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400
        team = Team.query.filter(Team.competition_id == comp_id, Team.id == team_id).first()
        if not team:
            return {"error": "not_found"}, 404
        payload = request.get_json(silent=True) or {}
        force = bool(payload.get("force"))
        confirm_text = (payload.get("confirm_text") or "").strip()
        if team.checkins:
            if not force:
                return {
                    "error": "conflict",
                    "detail": "Cannot delete team with existing check-ins.",
                }, 409
            if confirm_text != "Delete":
                return {
                    "error": "validation_error",
                    "detail": "Type Delete to confirm deletion.",
                }, 400

        TeamGroup.query.filter_by(team_id=team.id).delete()
        db.session.delete(team)
        db.session.commit()
        return {"ok": True}, 200


class TeamActiveGroupResource(Resource):
    method_decorators = [json_roles_required("judge", "admin")]

    def post(self, team_id: int):
        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400
        payload = request.get_json(silent=True) or {}
        group_id = payload.get("group_id")
        try:
            group_id = int(group_id)
        except Exception:
            return {"error": "validation_error", "detail": "group_id must be integer"}, 400

        team = Team.query.filter(Team.competition_id == comp_id, Team.id == team_id).first()
        if not team:
            return {"error": "not_found"}, 404

        ok, err = _apply_group_assignment(team, group_id)
        if not ok:
            return {"error": "validation_error", "detail": err}, 400

        db.session.commit()
        return {"ok": True}, 200


def _parse_group_prefix(prefix: str) -> tuple[int, int] | None:
    if not prefix:
        return None
    text = prefix.strip().lower()
    match = re.match(r"^(\d+)(x+)$", text)
    if not match:
        return None
    base = int(match.group(1))
    width = len(match.group(2))
    start = base * (10 ** width)
    end = start + (10 ** width) - 1
    return start, end


class TeamNumberRandomizeResource(Resource):
    method_decorators = [json_roles_required("judge", "admin")]

    def post(self):
        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400

        payload = request.get_json(silent=True) or {}
        group_id = payload.get("group_id")

        groups_query = CheckpointGroup.query.filter(CheckpointGroup.competition_id == comp_id)
        if group_id not in (None, ""):
            try:
                group_id = int(group_id)
            except Exception:
                return {"error": "validation_error", "detail": "group_id must be integer"}, 400
            groups_query = groups_query.filter(CheckpointGroup.id == group_id)
        groups = groups_query.all()
        if group_id not in (None, "") and not groups:
            return {"error": "not_found", "detail": "group not found"}, 404

        results = []
        assigned_total = 0

        for group in groups:
            prefix = (group.prefix or "").strip()
            parsed = _parse_group_prefix(prefix)
            if not parsed:
                results.append({
                    "group_id": group.id,
                    "group_name": group.name,
                    "status": "skipped",
                    "detail": "invalid_prefix",
                })
                continue

            start, end = parsed
            total_teams = (
                Team.query
                .join(TeamGroup, TeamGroup.team_id == Team.id)
                .filter(
                    Team.competition_id == comp_id,
                    TeamGroup.group_id == group.id,
                    TeamGroup.active.is_(True),
                )
                .count()
            )
            if total_teams <= 0:
                results.append({
                    "group_id": group.id,
                    "group_name": group.name,
                    "status": "no_op",
                    "detail": "no_teams_in_group",
                })
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
                Team.query
                .join(TeamGroup, TeamGroup.team_id == Team.id)
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
                results.append({
                    "group_id": group.id,
                    "group_name": group.name,
                    "status": "no_op",
                    "detail": "no_unnumbered_teams",
                })
                continue

            available = [n for n in range(range_start, range_end + 1) if n not in used_set]
            if len(available) < needed:
                results.append({
                    "group_id": group.id,
                    "group_name": group.name,
                    "status": "insufficient_numbers",
                    "needed": needed,
                    "available": len(available),
                    "range": [range_start, range_end],
                })
                continue

            random.shuffle(available)
            for team, number in zip(teams, available):
                team.number = number
            assigned_total += needed
            results.append({
                "group_id": group.id,
                "group_name": group.name,
                "status": "assigned",
                "assigned": needed,
                "range": [range_start, range_end],
            })

        if assigned_total:
            db.session.commit()
        return {"ok": True, "assigned_total": assigned_total, "results": results}, 200
