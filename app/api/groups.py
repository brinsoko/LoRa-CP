from __future__ import annotations

from typing import Iterable, List

from flask import Blueprint, jsonify, request
from flask_login import current_user
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from app.api.helpers import json_ok
from app.extensions import db
from app.models import Checkpoint, CheckpointGroup, CheckpointGroupLink, TeamGroup
from app.utils.audit import record_audit_event
from app.utils.competition import require_current_competition_id
from app.utils.rest_auth import json_roles_required
from app.utils.validators import validate_text

groups_api_bp = Blueprint("api_groups", __name__)


def _serialize_group(group: CheckpointGroup, include_checkpoints: bool = True) -> dict:
    data = {
        "id": group.id,
        "name": group.name,
        "prefix": group.prefix,
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


def _group_snapshot(group: CheckpointGroup) -> dict:
    return _serialize_group(group)


def _parse_checkpoint_ids(values: Iterable) -> List[int]:
    ids: List[int] = []
    for value in values or []:
        try:
            number = int(value)
            if number > 0:
                ids.append(number)
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


def _group_query(comp_id: int):
    return db.session.query(CheckpointGroup).filter(CheckpointGroup.competition_id == comp_id)


@groups_api_bp.get("/api/groups")
def group_list():
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    groups = (
        _group_query(comp_id)
        .options(joinedload(CheckpointGroup.checkpoint_links).joinedload(CheckpointGroupLink.checkpoint))
        .order_by(CheckpointGroup.position.asc(), CheckpointGroup.name.asc())
        .all()
    )
    return json_ok({"groups": [_serialize_group(g) for g in groups]})


@groups_api_bp.post("/api/groups")
@json_roles_required("judge", "admin")
def group_create():
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    payload = request.get_json(silent=True) or {}
    name, name_error = validate_text(payload.get("name"), field_name="name", max_length=120, required=True)
    prefix, prefix_error = validate_text(payload.get("prefix"), field_name="prefix", max_length=20)
    description, description_error = validate_text(
        payload.get("description"),
        field_name="description",
        max_length=2000,
        multiline=True,
    )
    checkpoint_ids = _parse_checkpoint_ids(payload.get("checkpoint_ids"))

    if name_error:
        return jsonify({"error": "validation_error", "detail": name_error}), 400
    if prefix_error:
        return jsonify({"error": "validation_error", "detail": prefix_error}), 400
    if description_error:
        return jsonify({"error": "validation_error", "detail": description_error}), 400
    if _group_query(comp_id).filter(CheckpointGroup.name == name).first():
        return jsonify({"error": "duplicate", "detail": "Group name already exists."}), 409

    max_position = _group_query(comp_id).with_entities(func.max(CheckpointGroup.position)).scalar()
    next_position = (max_position if max_position is not None else -1) + 1
    group = CheckpointGroup(
        name=name,
        prefix=prefix,
        description=description,
        competition_id=comp_id,
        position=next_position,
    )
    db.session.add(group)
    db.session.flush()

    if checkpoint_ids:
        _sync_group_checkpoints(group, checkpoint_ids)

    db.session.flush()
    record_audit_event(
        competition_id=comp_id,
        event_type="group_created",
        entity_type="group",
        entity_id=group.id,
        actor_user=current_user if current_user.is_authenticated else None,
        summary=f"Group {group.name} created.",
        details=_group_snapshot(group),
    )
    db.session.commit()
    return json_ok({"ok": True, "group": _serialize_group(group)}, status=201)


@groups_api_bp.get("/api/groups/<int:group_id>")
def group_get(group_id: int):
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    group = (
        _group_query(comp_id)
        .filter(CheckpointGroup.id == group_id)
        .options(joinedload(CheckpointGroup.checkpoint_links).joinedload(CheckpointGroupLink.checkpoint))
        .first()
    )
    if not group:
        return jsonify({"error": "not_found"}), 404
    return json_ok(_serialize_group(group))


def _update_group(group_id: int, partial: bool):
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    group = (
        _group_query(comp_id)
        .options(joinedload(CheckpointGroup.checkpoint_links))
        .filter(CheckpointGroup.id == group_id)
        .first()
    )
    if not group:
        return jsonify({"error": "not_found"}), 404
    before = _group_snapshot(group)

    payload = request.get_json(silent=True) or {}
    if not partial or "name" in payload:
        name, name_error = validate_text(payload.get("name"), field_name="name", max_length=120, required=True)
        if name_error:
            return jsonify({"error": "validation_error", "detail": name_error}), 400
        group.name = name

    if "prefix" in payload or not partial:
        prefix, prefix_error = validate_text(payload.get("prefix"), field_name="prefix", max_length=20)
        if prefix_error:
            return jsonify({"error": "validation_error", "detail": prefix_error}), 400
        group.prefix = prefix or None

    if "description" in payload or not partial:
        description, description_error = validate_text(
            payload.get("description"),
            field_name="description",
            max_length=2000,
            multiline=True,
        )
        if description_error:
            return jsonify({"error": "validation_error", "detail": description_error}), 400
        group.description = description or None

    if "checkpoint_ids" in payload:
        checkpoint_ids = _parse_checkpoint_ids(payload.get("checkpoint_ids"))
        _sync_group_checkpoints(group, checkpoint_ids)

    db.session.flush()
    record_audit_event(
        competition_id=comp_id,
        event_type="group_updated",
        entity_type="group",
        entity_id=group.id,
        actor_user=current_user if current_user.is_authenticated else None,
        summary=f"Group {group.name} updated.",
        details={"before": before, "after": _group_snapshot(group)},
    )
    db.session.commit()
    return json_ok({"ok": True, "group": _serialize_group(group)})


@groups_api_bp.patch("/api/groups/<int:group_id>")
@json_roles_required("judge", "admin")
def group_patch(group_id: int):
    return _update_group(group_id, partial=True)


@groups_api_bp.put("/api/groups/<int:group_id>")
@json_roles_required("judge", "admin")
def group_put(group_id: int):
    return _update_group(group_id, partial=False)


@groups_api_bp.delete("/api/groups/<int:group_id>")
@json_roles_required("admin")
def group_delete(group_id: int):
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    group = _group_query(comp_id).filter(CheckpointGroup.id == group_id).first()
    if not group:
        return jsonify({"error": "not_found"}), 404

    active_refs = db.session.query(TeamGroup).filter(TeamGroup.group_id == group_id, TeamGroup.active.is_(True)).count()
    if active_refs:
        return jsonify(
            {
                "error": "conflict",
                "detail": "Cannot delete a group that is active for one or more teams.",
            }
        ), 409

    snapshot = _group_snapshot(group)
    record_audit_event(
        competition_id=comp_id,
        event_type="group_deleted",
        entity_type="group",
        entity_id=group.id,
        actor_user=current_user if current_user.is_authenticated else None,
        summary=f"Group {group.name} deleted.",
        details=snapshot,
    )
    db.session.delete(group)
    db.session.commit()
    return json_ok({"ok": True})


@groups_api_bp.post("/api/groups/order")
@json_roles_required("admin")
def group_order():
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    payload = request.get_json(silent=True) or {}
    ordered_ids = _parse_checkpoint_ids(payload.get("group_ids"))
    if not ordered_ids:
        return jsonify({"error": "validation_error", "detail": "group_ids are required"}), 400
    if len(set(ordered_ids)) != len(ordered_ids):
        return jsonify({"error": "validation_error", "detail": "group_ids must be unique"}), 400

    existing_groups = _group_query(comp_id).all()
    existing_ids = {g.id for g in existing_groups}
    if set(ordered_ids) != existing_ids:
        return jsonify({"error": "validation_error", "detail": "group_ids must include all groups"}), 400

    group_by_id = {g.id: g for g in existing_groups}
    for position, group_id in enumerate(ordered_ids):
        group_by_id[group_id].position = position

    record_audit_event(
        competition_id=comp_id,
        event_type="group_order_updated",
        entity_type="group_batch",
        entity_id=None,
        actor_user=current_user if current_user.is_authenticated else None,
        summary="Group order updated.",
        details={"group_ids": ordered_ids},
    )
    db.session.commit()
    return json_ok({"ok": True})
