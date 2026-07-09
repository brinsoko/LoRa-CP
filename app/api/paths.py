from __future__ import annotations

from collections.abc import Iterable

from flask import Blueprint, jsonify, request
from flask_babel import gettext as _
from flask_login import current_user
from sqlalchemy.orm import joinedload

from app.api.helpers import json_ok
from app.extensions import db
from app.models import Checkpoint, CheckpointGroup, Path, PathStop
from app.utils.audit import record_audit_event
from app.utils.competition import require_current_competition_id
from app.utils.paths import replace_path_stops
from app.utils.rest_auth import json_login_required, json_roles_required
from app.utils.validators import validate_text

paths_api_bp = Blueprint("api_paths", __name__)


def _serialize_path(path: Path, include_stops: bool = True) -> dict:
    data = {
        "id": path.id,
        "name": path.name,
        "notes": path.notes,
        "group_count": len(path.groups),
        "groups": [
            {"id": group.id, "name": group.name, "direction": group.direction}
            for group in sorted(path.groups, key=lambda g: (g.position, g.name))
        ],
    }
    if include_stops:
        data["stops"] = [
            {
                "checkpoint_id": stop.checkpoint_id,
                "name": stop.checkpoint.name if stop.checkpoint else None,
                "position": stop.position,
                "expected_leg_minutes": stop.expected_leg_minutes,
            }
            for stop in path.stops
        ]
    return data


def _parse_minutes_list(values) -> list[float | None] | None:
    """Parse an expected_leg_minutes list aligned with checkpoint_ids."""
    if not isinstance(values, list):
        return None
    minutes: list[float | None] = []
    for value in values:
        if value in (None, ""):
            minutes.append(None)
            continue
        try:
            number = float(value)
            minutes.append(number if number >= 0 else None)
        except (TypeError, ValueError):
            minutes.append(None)
    return minutes


def _parse_checkpoint_ids(values: Iterable) -> list[int] | None:
    """Parse an ordered checkpoint-id list; None when input is not a list."""
    if not isinstance(values, list):
        return None
    ids: list[int] = []
    for value in values:
        try:
            number = int(value)
        except Exception:
            continue
        if number > 0:
            ids.append(number)
    return ids


def _validate_checkpoints(comp_id: int, checkpoint_ids: list[int]) -> str | None:
    if not checkpoint_ids:
        return None
    valid_ids = {
        cp_id
        for (cp_id,) in db.session.query(Checkpoint.id)
        .filter(Checkpoint.competition_id == comp_id, Checkpoint.id.in_(set(checkpoint_ids)))
        .all()
    }
    missing = [cid for cid in checkpoint_ids if cid not in valid_ids]
    if missing:
        return _("Unknown checkpoint ids: %(ids)s", ids=", ".join(str(c) for c in missing))
    return None


def _path_query(comp_id: int):
    return (
        db.session.query(Path)
        .filter(Path.competition_id == comp_id)
        .options(joinedload(Path.stops).joinedload(PathStop.checkpoint), joinedload(Path.groups))
    )


def _unique_name(comp_id: int, base: str) -> str:
    """First free name of the form base / base (2) / base (3)..."""
    existing = {
        name for (name,) in db.session.query(Path.name).filter(Path.competition_id == comp_id).all()
    }
    if base not in existing:
        return base
    counter = 2
    while f"{base} ({counter})" in existing:
        counter += 1
    return f"{base} ({counter})"


@paths_api_bp.get("/api/paths")
@json_login_required
def path_list():
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    paths = _path_query(comp_id).order_by(Path.name.asc()).all()
    return json_ok({"paths": [_serialize_path(p) for p in paths]})


@paths_api_bp.get("/api/paths/<int:path_id>")
@json_login_required
def path_get(path_id: int):
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    path = _path_query(comp_id).filter(Path.id == path_id).first()
    if not path:
        return jsonify({"error": "not_found"}), 404
    return json_ok(_serialize_path(path))


@paths_api_bp.post("/api/paths")
@json_roles_required("judge", "admin")
def path_create():
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    payload = request.get_json(silent=True) or {}
    name, name_error = validate_text(payload.get("name"), field_name="name", max_length=120, required=True)
    notes, notes_error = validate_text(
        payload.get("notes"), field_name="notes", max_length=2000, multiline=True
    )
    if name_error:
        return jsonify({"error": "validation_error", "detail": name_error}), 400
    if notes_error:
        return jsonify({"error": "validation_error", "detail": notes_error}), 400
    if db.session.query(Path).filter(Path.competition_id == comp_id, Path.name == name).first():
        return jsonify({"error": "duplicate", "detail": _("Path name already exists.")}), 409

    checkpoint_ids = _parse_checkpoint_ids(payload.get("checkpoint_ids")) or []
    invalid = _validate_checkpoints(comp_id, checkpoint_ids)
    if invalid:
        return jsonify({"error": "validation_error", "detail": invalid}), 400

    path = Path(competition_id=comp_id, name=name, notes=notes or None)
    db.session.add(path)
    db.session.flush()
    replace_path_stops(path, checkpoint_ids, _parse_minutes_list(payload.get("expected_leg_minutes")))
    db.session.flush()
    record_audit_event(
        competition_id=comp_id,
        event_type="path_created",
        entity_type="path",
        entity_id=path.id,
        actor_user=current_user if current_user.is_authenticated else None,
        summary=f"Path {path.name} created.",
        details=_serialize_path(path),
    )
    db.session.commit()
    return json_ok({"ok": True, "path": _serialize_path(path)}, status=201)


def _update_path(path_id: int, partial: bool):
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    path = _path_query(comp_id).filter(Path.id == path_id).first()
    if not path:
        return jsonify({"error": "not_found"}), 404
    before = _serialize_path(path)

    payload = request.get_json(silent=True) or {}
    if not partial or "name" in payload:
        name, name_error = validate_text(payload.get("name"), field_name="name", max_length=120, required=True)
        if name_error:
            return jsonify({"error": "validation_error", "detail": name_error}), 400
        duplicate = (
            db.session.query(Path)
            .filter(Path.competition_id == comp_id, Path.name == name, Path.id != path.id)
            .first()
        )
        if duplicate:
            return jsonify({"error": "duplicate", "detail": _("Path name already exists.")}), 409
        path.name = name

    if "notes" in payload or not partial:
        notes, notes_error = validate_text(
            payload.get("notes"), field_name="notes", max_length=2000, multiline=True
        )
        if notes_error:
            return jsonify({"error": "validation_error", "detail": notes_error}), 400
        path.notes = notes or None

    if "checkpoint_ids" in payload:
        checkpoint_ids = _parse_checkpoint_ids(payload.get("checkpoint_ids"))
        if checkpoint_ids is None:
            return jsonify({"error": "validation_error", "detail": "checkpoint_ids must be a list"}), 400
        invalid = _validate_checkpoints(comp_id, checkpoint_ids)
        if invalid:
            return jsonify({"error": "validation_error", "detail": invalid}), 400
        replace_path_stops(path, checkpoint_ids, _parse_minutes_list(payload.get("expected_leg_minutes")))

    db.session.flush()
    record_audit_event(
        competition_id=comp_id,
        event_type="path_updated",
        entity_type="path",
        entity_id=path.id,
        actor_user=current_user if current_user.is_authenticated else None,
        summary=f"Path {path.name} updated.",
        details={"before": before, "after": _serialize_path(path)},
    )
    db.session.commit()
    return json_ok({"ok": True, "path": _serialize_path(path)})


@paths_api_bp.patch("/api/paths/<int:path_id>")
@json_roles_required("judge", "admin")
def path_patch(path_id: int):
    return _update_path(path_id, partial=True)


@paths_api_bp.put("/api/paths/<int:path_id>")
@json_roles_required("judge", "admin")
def path_put(path_id: int):
    return _update_path(path_id, partial=False)


@paths_api_bp.post("/api/paths/<int:path_id>/duplicate")
@json_roles_required("judge", "admin")
def path_duplicate(path_id: int):
    """Copy a path, optionally with the stop order reversed.

    Used by the 'Duplicate' / 'Duplicate reversed' actions when a copy
    should evolve independently. For 'same course, other direction' prefer
    assigning the same path with direction=reverse on the group.
    """
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    source = _path_query(comp_id).filter(Path.id == path_id).first()
    if not source:
        return jsonify({"error": "not_found"}), 404

    payload = request.get_json(silent=True) or {}
    reversed_copy = bool(payload.get("reversed"))
    requested_name = payload.get("name")
    if requested_name:
        name, name_error = validate_text(requested_name, field_name="name", max_length=120, required=True)
        if name_error:
            return jsonify({"error": "validation_error", "detail": name_error}), 400
        if db.session.query(Path).filter(Path.competition_id == comp_id, Path.name == name).first():
            return jsonify({"error": "duplicate", "detail": _("Path name already exists.")}), 409
    else:
        suffix = _("(reversed)") if reversed_copy else _("(copy)")
        name = _unique_name(comp_id, f"{source.name} {suffix}"[:120])

    copy = Path(competition_id=comp_id, name=name, notes=source.notes)
    db.session.add(copy)
    db.session.flush()
    stops = list(source.stops)
    if reversed_copy:
        stops = list(reversed(stops))
    for position, stop in enumerate(stops):
        copy.stops.append(
            PathStop(
                checkpoint_id=stop.checkpoint_id,
                position=position,
                expected_leg_minutes=stop.expected_leg_minutes,
            )
        )
    db.session.flush()
    record_audit_event(
        competition_id=comp_id,
        event_type="path_created",
        entity_type="path",
        entity_id=copy.id,
        actor_user=current_user if current_user.is_authenticated else None,
        summary=f"Path {copy.name} duplicated from {source.name}.",
        details=_serialize_path(copy),
    )
    db.session.commit()
    return json_ok({"ok": True, "path": _serialize_path(copy)}, status=201)


@paths_api_bp.delete("/api/paths/<int:path_id>")
@json_roles_required("admin")
def path_delete(path_id: int):
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    path = db.session.query(Path).filter(Path.competition_id == comp_id, Path.id == path_id).first()
    if not path:
        return jsonify({"error": "not_found"}), 404

    referencing = (
        db.session.query(CheckpointGroup).filter(CheckpointGroup.path_id == path_id).count()
    )
    if referencing:
        return jsonify(
            {
                "error": "conflict",
                "detail": _("Cannot delete a path that is assigned to one or more groups."),
            }
        ), 409

    snapshot = _serialize_path(path)
    record_audit_event(
        competition_id=comp_id,
        event_type="path_deleted",
        entity_type="path",
        entity_id=path.id,
        actor_user=current_user if current_user.is_authenticated else None,
        summary=f"Path {path.name} deleted.",
        details=snapshot,
    )
    db.session.delete(path)
    db.session.commit()
    return json_ok({"ok": True})
