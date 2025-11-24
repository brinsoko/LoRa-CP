# app/resources/groups.py
from __future__ import annotations

from typing import Iterable, List, Tuple

from flask import request
from flask_restful import Resource
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import (
    CheckpointGroup,
    Checkpoint,
    CheckpointGroupLink,
    TeamGroup,
)
from app.utils.rest_auth import json_roles_required


def _serialize_group(group: CheckpointGroup, include_checkpoints: bool = True) -> dict:
    data = {
        "id": group.id,
        "name": group.name,
        "description": group.description,
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
            link = CheckpointGroupLink(group=group, checkpoint=checkpoint)
        link.position = position
        new_links.append(link)

    for obsolete in existing.values():
        db.session.delete(obsolete)

    group.checkpoint_links = new_links


class GroupListResource(Resource):
    def get(self):
        groups = (
            db.session.query(CheckpointGroup)
            .options(
                joinedload(CheckpointGroup.checkpoint_links)
                .joinedload(CheckpointGroupLink.checkpoint)
            )
            .order_by(CheckpointGroup.name.asc())
            .all()
        )
        return {"groups": [_serialize_group(g) for g in groups]}, 200

    @json_roles_required("judge", "admin")
    def post(self):
        payload = request.get_json(silent=True) or {}
        name = (payload.get("name") or "").strip()
        description = (payload.get("description") or "").strip() or None
        checkpoint_ids = _parse_checkpoint_ids(payload.get("checkpoint_ids"))

        if not name:
            return {"error": "validation_error", "detail": "name is required"}, 400
        if db.session.query(CheckpointGroup).filter(CheckpointGroup.name == name).first():
            return {"error": "duplicate", "detail": "Group name already exists."}, 409

        group = CheckpointGroup(name=name, description=description)
        db.session.add(group)
        db.session.flush()

        if checkpoint_ids:
            _sync_group_checkpoints(group, checkpoint_ids)

        db.session.commit()
        return {"ok": True, "group": _serialize_group(group)}, 201


class GroupItemResource(Resource):
    def get(self, group_id: int):
        group = (
            db.session.query(CheckpointGroup)
            .options(
                joinedload(CheckpointGroup.checkpoint_links)
                .joinedload(CheckpointGroupLink.checkpoint)
            )
            .get(group_id)
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
        group = (
            db.session.query(CheckpointGroup)
            .options(joinedload(CheckpointGroup.checkpoint_links))
            .get(group_id)
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
        group = db.session.query(CheckpointGroup).get(group_id)
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
