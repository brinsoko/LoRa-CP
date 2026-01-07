# app/resources/groups.py
from __future__ import annotations

from typing import Iterable, List, Tuple

from flask import request
from flask_restful import Resource
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import (
    CheckpointGroup,
    Checkpoint,
    CheckpointGroupLink,
    TeamGroup,
)
from app.utils.rest_auth import json_roles_required
from app.utils.competition import require_current_competition_id


def _serialize_group(group: CheckpointGroup, include_checkpoints: bool = True) -> dict:
    data = {
        "id": group.id,
        "name": group.name,
        "description": group.description,
        "position": group.position,
    }
    if include_checkpoints:
        data["checkpoints"] = [
            {
                "id": link.checkpoint_id,
                "name": link.checkpoint.name if link.checkpoint else None,
                "position": link.position,
            }
            for link in group.checkpoint_links
        ]
    return data


def _parse_checkpoint_ids(values: Iterable) -> List[int]:
    ids: List[int] = []
    for v in values or []:
        try:
            n = int(v)
            if n > 0:
                ids.append(n)
        except Exception:
            continue
    return ids


def _sync_group_checkpoints(group: CheckpointGroup, ordered_ids: List[int]) -> None:
    existing = {link.checkpoint_id: link for link in group.checkpoint_links}
    new_links: List[CheckpointGroupLink] = []

    for position, cp_id in enumerate(ordered_ids):
        link = existing.pop(cp_id, None)
        if link is None:
            checkpoint = db.session.get(Checkpoint, cp_id)
            if not checkpoint:
                continue
            if checkpoint.competition_id != group.competition_id:
                continue
            link = CheckpointGroupLink(group=group, checkpoint=checkpoint)
        link.position = position
        new_links.append(link)

    for obsolete in existing.values():
        db.session.delete(obsolete)

    group.checkpoint_links = new_links


class GroupListResource(Resource):
    def get(self):
        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400
        groups = (
            db.session.query(CheckpointGroup)
            .filter(CheckpointGroup.competition_id == comp_id)
            .options(
                joinedload(CheckpointGroup.checkpoint_links)
                .joinedload(CheckpointGroupLink.checkpoint)
            )
            .order_by(CheckpointGroup.position.asc(), CheckpointGroup.name.asc())
            .all()
        )
        return {"groups": [_serialize_group(g) for g in groups]}, 200

    @json_roles_required("judge", "admin")
    def post(self):
        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400
        payload = request.get_json(silent=True) or {}
        name = (payload.get("name") or "").strip()
        description = (payload.get("description") or "").strip() or None
        checkpoint_ids = _parse_checkpoint_ids(payload.get("checkpoint_ids"))

        if not name:
            return {"error": "validation_error", "detail": "name is required"}, 400
        if (
            db.session.query(CheckpointGroup)
            .filter(CheckpointGroup.competition_id == comp_id, CheckpointGroup.name == name)
            .first()
        ):
            return {"error": "duplicate", "detail": "Group name already exists."}, 409

        max_position = (
            db.session.query(func.max(CheckpointGroup.position))
            .filter(CheckpointGroup.competition_id == comp_id)
            .scalar()
        )
        next_position = (max_position if max_position is not None else -1) + 1
        group = CheckpointGroup(
            name=name,
            description=description,
            competition_id=comp_id,
            position=next_position,
        )
        db.session.add(group)
        db.session.flush()

        if checkpoint_ids:
            _sync_group_checkpoints(group, checkpoint_ids)

        db.session.commit()
        return {"ok": True, "group": _serialize_group(group)}, 201


class GroupItemResource(Resource):
    def get(self, group_id: int):
        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400
        group = (
            db.session.query(CheckpointGroup)
            .filter(CheckpointGroup.competition_id == comp_id, CheckpointGroup.id == group_id)
            .options(
                joinedload(CheckpointGroup.checkpoint_links)
                .joinedload(CheckpointGroupLink.checkpoint)
            )
            .first()
        )
        if not group:
            return {"error": "not_found"}, 404
        return _serialize_group(group), 200

    @json_roles_required("judge", "admin")
    def patch(self, group_id: int):
        return self._update(group_id, partial=True)

    @json_roles_required("judge", "admin")
    def put(self, group_id: int):
        return self._update(group_id, partial=False)

    def _update(self, group_id: int, partial: bool):
        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400
        group = (
            db.session.query(CheckpointGroup)
            .options(joinedload(CheckpointGroup.checkpoint_links))
            .filter(CheckpointGroup.competition_id == comp_id, CheckpointGroup.id == group_id)
            .first()
        )
        if not group:
            return {"error": "not_found"}, 404

        payload = request.get_json(silent=True) or {}
        if not partial or "name" in payload:
            name = (payload.get("name") or "").strip()
            if not name:
                return {"error": "validation_error", "detail": "name is required"}, 400
            group.name = name

        if "description" in payload or not partial:
            group.description = (payload.get("description") or "").strip() or None

        if "checkpoint_ids" in payload:
            checkpoint_ids = _parse_checkpoint_ids(payload.get("checkpoint_ids"))
            _sync_group_checkpoints(group, checkpoint_ids)

        db.session.commit()
        return {"ok": True, "group": _serialize_group(group)}, 200

    @json_roles_required("admin")
    def delete(self, group_id: int):
        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400
        group = (
            db.session.query(CheckpointGroup)
            .filter(CheckpointGroup.competition_id == comp_id, CheckpointGroup.id == group_id)
            .first()
        )
        if not group:
            return {"error": "not_found"}, 404

        active_refs = (
            db.session.query(TeamGroup)
            .filter(TeamGroup.group_id == group_id, TeamGroup.active.is_(True))
            .count()
        )
        if active_refs:
            return {
                "error": "conflict",
                "detail": "Cannot delete a group that is active for one or more teams.",
            }, 409

        db.session.delete(group)
        db.session.commit()
        return {"ok": True}, 200


class GroupOrderResource(Resource):
    @json_roles_required("admin")
    def post(self):
        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400
        payload = request.get_json(silent=True) or {}
        ordered_ids = _parse_checkpoint_ids(payload.get("group_ids"))
        if not ordered_ids:
            return {"error": "validation_error", "detail": "group_ids are required"}, 400
        if len(set(ordered_ids)) != len(ordered_ids):
            return {"error": "validation_error", "detail": "group_ids must be unique"}, 400

        existing_groups = (
            db.session.query(CheckpointGroup)
            .filter(CheckpointGroup.competition_id == comp_id)
            .all()
        )
        existing_ids = {g.id for g in existing_groups}
        if set(ordered_ids) != existing_ids:
            return {"error": "validation_error", "detail": "group_ids must include all groups"}, 400

        group_by_id = {g.id: g for g in existing_groups}
        for position, group_id in enumerate(ordered_ids):
            group_by_id[group_id].position = position

        db.session.commit()
        return {"ok": True}, 200
