from __future__ import annotations

import re
from collections.abc import Iterable

from flask import Blueprint, jsonify, request
from flask_babel import gettext as _
from flask_login import current_user
from sqlalchemy import func
from sqlalchemy.orm import joinedload

from app.api.helpers import json_ok
from app.extensions import db
from app.models import CheckpointGroup, Path, PathStop, TeamGroup
from app.utils.audit import record_audit_event
from app.utils.competition import require_current_competition_id
from app.utils.paths import resolve_route_ids
from app.utils.rest_auth import json_login_required, json_roles_required
from app.utils.validators import validate_text

_PREFIX_RE = re.compile(r"^(\d+)(x+)$", re.IGNORECASE)


def _validate_prefix_format(prefix: str | None) -> tuple[bool, str | None]:
    """Validate that prefix matches the digits+x pattern. Returns (ok, error_message)."""
    if not prefix:
        return True, None
    text = prefix.strip().lower()
    if not _PREFIX_RE.match(text):
        return False, _("Prefix must be digits followed by one or more 'x' characters (e.g. 3xx, 01xx).")
    return True, None


def _validate_prefix_overlap(
    prefix: str | None, comp_id: int, exclude_group_id: int | None = None
) -> tuple[bool, str | None]:
    """Check that the new prefix doesn't overlap with existing group prefixes in the same competition."""
    if not prefix:
        return True, None
    text = prefix.strip().lower()
    match = _PREFIX_RE.match(text)
    if not match:
        return True, None  # format validation handled separately
    new_digits = match.group(1)

    query = db.session.query(CheckpointGroup).filter(CheckpointGroup.competition_id == comp_id)
    if exclude_group_id:
        query = query.filter(CheckpointGroup.id != exclude_group_id)

    for group in query.all():
        existing_prefix = (group.prefix or "").strip().lower()
        if not existing_prefix:
            continue
        existing_match = _PREFIX_RE.match(existing_prefix)
        if not existing_match:
            continue
        existing_digits = existing_match.group(1)

        # Neither digit portion should be a prefix of the other
        if new_digits.startswith(existing_digits) or existing_digits.startswith(new_digits):
            return False, _(
                "Prefix conflicts with existing group '%(group)s' (prefix: %(prefix)s).",
                group=group.name,
                prefix=group.prefix,
            )

    return True, None


groups_api_bp = Blueprint("api_groups", __name__)


def _serialize_group(group: CheckpointGroup, include_checkpoints: bool = True) -> dict:
    data = {
        "id": group.id,
        "name": group.name,
        "prefix": group.prefix,
        "description": group.description,
        "position": group.position,
        "path_id": group.path_id,
        "path_name": group.path.name if group.path else None,
        "direction": group.direction,
    }
    if include_checkpoints:
        # Read-only resolved route (direction applied) for display.
        name_by_id: dict[int, str | None] = {}
        if group.path:
            name_by_id = {
                stop.checkpoint_id: (stop.checkpoint.name if stop.checkpoint else None)
                for stop in group.path.stops
            }
        data["checkpoints"] = [
            {"id": cp_id, "name": name_by_id.get(cp_id), "position": position}
            for position, cp_id in enumerate(resolve_route_ids(group))
        ]
    return data


def _group_snapshot(group: CheckpointGroup) -> dict:
    return _serialize_group(group)


def _parse_id_list(values: Iterable) -> list[int]:
    ids: list[int] = []
    for value in values or []:
        try:
            number = int(value)
            if number > 0:
                ids.append(number)
        except Exception:
            continue
    return ids


def _resolve_path(comp_id: int, payload: dict) -> tuple[Path | None, tuple | None]:
    """Resolve payload['path_id'] to a Path or an (error response) tuple."""
    raw = payload.get("path_id")
    if raw in (None, "", "null", 0, "0"):
        return None, None
    try:
        path_id = int(raw)
    except (TypeError, ValueError):
        return None, (jsonify({"error": "validation_error", "detail": "path_id must be an integer"}), 400)
    path = db.session.query(Path).filter(Path.id == path_id, Path.competition_id == comp_id).first()
    if not path:
        return None, (jsonify({"error": "validation_error", "detail": _("Unknown path.")}), 400)
    return path, None


def _parse_direction(payload: dict) -> tuple[str | None, tuple | None]:
    raw = payload.get("direction")
    if raw is None:
        return None, None
    value = str(raw).strip().lower()
    if value not in ("forward", "reverse"):
        return None, (
            jsonify({"error": "validation_error", "detail": "direction must be 'forward' or 'reverse'"}),
            400,
        )
    return value, None


def _group_query(comp_id: int):
    return db.session.query(CheckpointGroup).filter(CheckpointGroup.competition_id == comp_id)


@groups_api_bp.get("/api/groups")
@json_login_required
def group_list():
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    groups = (
        _group_query(comp_id)
        .options(joinedload(CheckpointGroup.path).joinedload(Path.stops).joinedload(PathStop.checkpoint))
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
    if name_error:
        return jsonify({"error": "validation_error", "detail": name_error}), 400
    if prefix_error:
        return jsonify({"error": "validation_error", "detail": prefix_error}), 400
    if description_error:
        return jsonify({"error": "validation_error", "detail": description_error}), 400

    path, path_error = _resolve_path(comp_id, payload)
    if path_error:
        return path_error
    direction, direction_error = _parse_direction(payload)
    if direction_error:
        return direction_error

    # Validate prefix format and overlap
    if prefix:
        fmt_ok, fmt_err = _validate_prefix_format(prefix)
        if not fmt_ok:
            return jsonify({"error": "validation_error", "detail": fmt_err}), 400
        overlap_ok, overlap_err = _validate_prefix_overlap(prefix, comp_id)
        if not overlap_ok:
            return jsonify({"error": "validation_error", "detail": overlap_err}), 409

    if _group_query(comp_id).filter(CheckpointGroup.name == name).first():
        return jsonify({"error": "duplicate", "detail": _("Group name already exists.")}), 409

    max_position = _group_query(comp_id).with_entities(func.max(CheckpointGroup.position)).scalar()
    next_position = (max_position if max_position is not None else -1) + 1
    group = CheckpointGroup(
        name=name,
        prefix=prefix,
        description=description,
        competition_id=comp_id,
        position=next_position,
        path=path,
        direction=direction or "forward",
    )
    db.session.add(group)
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
@json_login_required
def group_get(group_id: int):
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    group = (
        _group_query(comp_id)
        .filter(CheckpointGroup.id == group_id)
        .options(joinedload(CheckpointGroup.path).joinedload(Path.stops).joinedload(PathStop.checkpoint))
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
        .options(joinedload(CheckpointGroup.path).joinedload(Path.stops))
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
        if prefix:
            fmt_ok, fmt_err = _validate_prefix_format(prefix)
            if not fmt_ok:
                return jsonify({"error": "validation_error", "detail": fmt_err}), 400
            overlap_ok, overlap_err = _validate_prefix_overlap(prefix, comp_id, exclude_group_id=group_id)
            if not overlap_ok:
                return jsonify({"error": "validation_error", "detail": overlap_err}), 409
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

    if "path_id" in payload or not partial:
        path, path_error = _resolve_path(comp_id, payload)
        if path_error:
            return path_error
        group.path = path

    if "direction" in payload or not partial:
        direction, direction_error = _parse_direction(payload)
        if direction_error:
            return direction_error
        group.direction = direction or "forward"

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
    ordered_ids = _parse_id_list(payload.get("group_ids"))
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
