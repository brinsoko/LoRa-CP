from __future__ import annotations

from collections.abc import Iterable

from flask import Blueprint, jsonify, request
from flask_login import current_user
from sqlalchemy.orm import joinedload

from app.api.helpers import json_ok
from app.extensions import db
from app.models import Checkpoint, CheckpointGroup, CheckpointGroupLink, LoRaDevice
from app.utils.audit import record_audit_event
from app.utils.competition import require_current_competition_id
from app.utils.rest_auth import json_login_required, json_roles_required
from app.utils.validators import validate_finite_float, validate_text

checkpoints_api_bp = Blueprint("api_checkpoints", __name__)


def _serialize_checkpoint(cp: Checkpoint) -> dict:
    # Read assigned judges via JudgeCheckpoint -> User for the read-only
    # roster the admin sees on the edit page. Free-text judges_note is
    # separate so admins can record sub-judges / volunteers without
    # creating user accounts for them.
    from app.models import JudgeCheckpoint
    from app.models import User as _User

    assigned_users = (
        db.session.query(_User.id, _User.username)
        .join(JudgeCheckpoint, JudgeCheckpoint.user_id == _User.id)
        .filter(JudgeCheckpoint.checkpoint_id == cp.id)
        .order_by(_User.username.asc())
        .all()
    )

    return {
        "id": cp.id,
        "name": cp.name,
        "location": cp.location,
        "description": cp.description,
        "scoring_text": cp.scoring_text,
        "judges_note": cp.judges_note,
        "assigned_judges": [{"id": uid, "username": uname} for (uid, uname) in assigned_users],
        "easting": cp.easting,
        "northing": cp.northing,
        "is_virtual": cp.is_virtual,
        "groups": [
            {
                "id": link.group_id,
                "name": link.group.name if link.group else None,
                "position": link.position,
            }
            for link in sorted(
                cp.group_links,
                key=lambda link: (link.group.name if link.group else "", link.position),
            )
        ],
        "lora_device": (
            {
                "id": cp.lora_device.id,
                "dev_num": cp.lora_device.dev_num,
                "name": cp.lora_device.name,
            }
            if cp.lora_device
            else None
        ),
    }


def _checkpoint_snapshot(cp: Checkpoint) -> dict:
    return _serialize_checkpoint(cp)


def _parse_group_ids(values: Iterable) -> list[int]:
    ids: list[int] = []
    for value in values or []:
        try:
            number = int(value)
            if number > 0:
                ids.append(number)
        except Exception:
            continue
    return ids


def _apply_groups(cp: Checkpoint, group_ids: list[int]) -> None:
    if group_ids is None:
        return

    existing = {link.group_id: link for link in cp.group_links}
    new_links: list[CheckpointGroupLink] = []

    if not group_ids:
        for link in existing.values():
            db.session.delete(link)
        cp.group_links = []
        return

    groups = (
        db.session.query(CheckpointGroup)
        .options(joinedload(CheckpointGroup.checkpoint_links))
        .filter(
            CheckpointGroup.id.in_(group_ids),
            CheckpointGroup.competition_id == cp.competition_id,
        )
        .all()
    )
    group_lookup = {g.id: g for g in groups}

    for gid in group_ids:
        group = group_lookup.get(gid)
        if not group:
            continue
        link = existing.pop(gid, None)
        if link is None:
            next_position = max((cl.position for cl in group.checkpoint_links), default=-1) + 1
            link = CheckpointGroupLink(group=group, checkpoint=cp, position=next_position)
        new_links.append(link)

    for leftover in existing.values():
        db.session.delete(leftover)

    cp.group_links = new_links


def _assign_lora_device(cp: Checkpoint, device_id: int | None) -> dict | None:
    if not device_id:
        cp.lora_device = None
        return None

    device = db.session.get(LoRaDevice, device_id)
    if not device:
        return {"error": "invalid_device", "detail": "Invalid device."}
    if device.competition_id != cp.competition_id:
        return {
            "error": "invalid_device_competition",
            "detail": "Device not available for this competition.",
        }

    existing = Checkpoint.query.filter(
        Checkpoint.lora_device_id == device.id,
        Checkpoint.id != cp.id,
    ).first()
    if existing:
        return {
            "error": "device_in_use",
            "detail": f"Device already attached to checkpoint '{existing.name}'.",
            "checkpoint_id": existing.id,
            "checkpoint_name": existing.name,
        }

    cp.lora_device = device
    return None


def _checkpoint_query(comp_id: int):
    return Checkpoint.query.filter(Checkpoint.competition_id == comp_id)


@checkpoints_api_bp.get("/api/checkpoints")
@json_login_required
def checkpoint_list():
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    cps = (
        _checkpoint_query(comp_id)
        .options(
            joinedload(Checkpoint.group_links).joinedload(CheckpointGroupLink.group),
            joinedload(Checkpoint.lora_device),
        )
        .order_by(Checkpoint.position.asc().nulls_last(), Checkpoint.name.asc())
        .all()
    )
    return json_ok({"checkpoints": [_serialize_checkpoint(cp) for cp in cps]})


@checkpoints_api_bp.post("/api/checkpoints")
@json_roles_required("judge", "admin")
def checkpoint_create():
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    payload = request.get_json(silent=True) or {}
    name, name_error = validate_text(payload.get("name"), field_name="name", max_length=120, required=True)
    location, location_error = validate_text(payload.get("location"), field_name="location", max_length=255)
    description, description_error = validate_text(
        payload.get("description"),
        field_name="description",
        max_length=2000,
        multiline=True,
    )
    if name_error:
        return jsonify({"error": "validation_error", "detail": name_error}), 400
    if location_error:
        return jsonify({"error": "validation_error", "detail": location_error}), 400
    if description_error:
        return jsonify({"error": "validation_error", "detail": description_error}), 400
    if _checkpoint_query(comp_id).filter(Checkpoint.name == name).first():
        return jsonify({"error": "duplicate", "detail": "Checkpoint name already exists."}), 409

    easting, easting_err = validate_finite_float(payload.get("easting"), field_name="easting")
    if easting_err:
        return jsonify({"error": "validation_error", "detail": easting_err}), 400
    northing, northing_err = validate_finite_float(payload.get("northing"), field_name="northing")
    if northing_err:
        return jsonify({"error": "validation_error", "detail": northing_err}), 400

    cp = Checkpoint(
        competition_id=comp_id,
        name=name,
        location=location,
        description=description,
        easting=easting,
        northing=northing,
        is_virtual=bool(payload.get("is_virtual")),
    )
    db.session.add(cp)
    db.session.flush()

    group_ids = _parse_group_ids(payload.get("group_ids"))
    if group_ids:
        _apply_groups(cp, group_ids)

    lora_device_id = payload.get("lora_device_id")
    if lora_device_id is not None:
        try:
            lora_device_id = int(lora_device_id)
        except Exception:
            return jsonify({"error": "validation_error", "detail": "lora_device_id must be integer"}), 400
        error = _assign_lora_device(cp, lora_device_id)
        if error:
            db.session.rollback()
            return jsonify(error), 400

    db.session.flush()
    record_audit_event(
        competition_id=comp_id,
        event_type="checkpoint_created",
        entity_type="checkpoint",
        entity_id=cp.id,
        actor_user=current_user if current_user.is_authenticated else None,
        summary=f"Checkpoint {cp.name} created.",
        details=_checkpoint_snapshot(cp),
    )
    db.session.commit()
    return json_ok({"ok": True, "checkpoint": _serialize_checkpoint(cp)}, status=201)


@checkpoints_api_bp.get("/api/checkpoints/<int:checkpoint_id>")
@json_login_required
def checkpoint_get(checkpoint_id: int):
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    cp = (
        _checkpoint_query(comp_id)
        .filter(Checkpoint.id == checkpoint_id)
        .options(
            joinedload(Checkpoint.group_links).joinedload(CheckpointGroupLink.group),
            joinedload(Checkpoint.lora_device),
        )
        .first()
    )
    if not cp:
        return jsonify({"error": "not_found"}), 404
    return json_ok(_serialize_checkpoint(cp))


def _update_checkpoint(checkpoint_id: int, partial: bool):
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    cp = (
        Checkpoint.query.options(joinedload(Checkpoint.group_links))
        .filter(Checkpoint.competition_id == comp_id, Checkpoint.id == checkpoint_id)
        .first()
    )
    if not cp:
        return jsonify({"error": "not_found"}), 404
    before = _checkpoint_snapshot(cp)

    payload = request.get_json(silent=True) or {}

    if not partial or "name" in payload:
        name, name_error = validate_text(payload.get("name"), field_name="name", max_length=120, required=True)
        if name_error:
            return jsonify({"error": "validation_error", "detail": name_error}), 400
        existing = _checkpoint_query(comp_id).filter(Checkpoint.name == name, Checkpoint.id != cp.id).first()
        if existing:
            return jsonify({"error": "duplicate", "detail": "Checkpoint name already exists."}), 409
        cp.name = name

    if "location" in payload or not partial:
        location, location_error = validate_text(payload.get("location"), field_name="location", max_length=255)
        if location_error:
            return jsonify({"error": "validation_error", "detail": location_error}), 400
        cp.location = location or None

    if "description" in payload or not partial:
        description, description_error = validate_text(
            payload.get("description"),
            field_name="description",
            max_length=2000,
            multiline=True,
        )
        if description_error:
            return jsonify({"error": "validation_error", "detail": description_error}), 400
        cp.description = description or None

    if "scoring_text" in payload or not partial:
        scoring_text, scoring_text_error = validate_text(
            payload.get("scoring_text"),
            field_name="scoring_text",
            max_length=4000,
            multiline=True,
        )
        if scoring_text_error:
            return jsonify({"error": "validation_error", "detail": scoring_text_error}), 400
        cp.scoring_text = scoring_text or None

    if "judges_note" in payload or not partial:
        judges_note, judges_note_error = validate_text(
            payload.get("judges_note"),
            field_name="judges_note",
            max_length=2000,
            multiline=True,
        )
        if judges_note_error:
            return jsonify({"error": "validation_error", "detail": judges_note_error}), 400
        cp.judges_note = judges_note or None

    if "easting" in payload or not partial:
        easting, easting_err = validate_finite_float(payload.get("easting"), field_name="easting")
        if easting_err:
            return jsonify({"error": "validation_error", "detail": easting_err}), 400
        cp.easting = easting

    if "northing" in payload or not partial:
        northing, northing_err = validate_finite_float(payload.get("northing"), field_name="northing")
        if northing_err:
            return jsonify({"error": "validation_error", "detail": northing_err}), 400
        cp.northing = northing

    if "is_virtual" in payload or not partial:
        cp.is_virtual = bool(payload.get("is_virtual"))

    if "group_ids" in payload:
        group_ids = _parse_group_ids(payload.get("group_ids"))
        _apply_groups(cp, group_ids)

    if "lora_device_id" in payload or (not partial and "lora_device_id" not in payload):
        raw_device_id = payload.get("lora_device_id", None)
        if raw_device_id in (None, "", "null"):
            error = _assign_lora_device(cp, None)
        else:
            try:
                raw_device_id = int(raw_device_id)
            except Exception:
                return jsonify({"error": "validation_error", "detail": "lora_device_id must be integer"}), 400
            error = _assign_lora_device(cp, raw_device_id)
        if error:
            db.session.rollback()
            return jsonify(error), 400

    db.session.flush()
    record_audit_event(
        competition_id=comp_id,
        event_type="checkpoint_updated",
        entity_type="checkpoint",
        entity_id=cp.id,
        actor_user=current_user if current_user.is_authenticated else None,
        summary=f"Checkpoint {cp.name} updated.",
        details={"before": before, "after": _checkpoint_snapshot(cp)},
    )
    db.session.commit()
    return json_ok({"ok": True, "checkpoint": _serialize_checkpoint(cp)})


@checkpoints_api_bp.patch("/api/checkpoints/<int:checkpoint_id>")
@json_roles_required("judge", "admin")
def checkpoint_patch(checkpoint_id: int):
    return _update_checkpoint(checkpoint_id, partial=True)


@checkpoints_api_bp.put("/api/checkpoints/<int:checkpoint_id>")
@json_roles_required("judge", "admin")
def checkpoint_put(checkpoint_id: int):
    return _update_checkpoint(checkpoint_id, partial=False)


@checkpoints_api_bp.delete("/api/checkpoints/<int:checkpoint_id>")
@json_roles_required("admin")
def checkpoint_delete(checkpoint_id: int):
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    cp = _checkpoint_query(comp_id).filter(Checkpoint.id == checkpoint_id).first()
    if not cp:
        return jsonify({"error": "not_found"}), 404
    if cp.checkins:
        return jsonify({"error": "conflict", "detail": "Cannot delete checkpoint with existing check-ins."}), 409
    snapshot = _checkpoint_snapshot(cp)
    record_audit_event(
        competition_id=comp_id,
        event_type="checkpoint_deleted",
        entity_type="checkpoint",
        entity_id=cp.id,
        actor_user=current_user if current_user.is_authenticated else None,
        summary=f"Checkpoint {cp.name} deleted.",
        details=snapshot,
    )
    db.session.delete(cp)
    db.session.commit()
    return json_ok({"ok": True})


@checkpoints_api_bp.post("/api/checkpoints/import")
@json_roles_required("judge", "admin")
def checkpoint_import():
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    payload = request.get_json(silent=True) or {}
    items = payload.get("items") or []
    if not isinstance(items, list):
        return jsonify({"error": "validation_error", "detail": "items must be an array"}), 400

    created = updated = skipped = 0
    errors: list[dict] = []

    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            skipped += 1
            errors.append({"index": idx, "detail": "Item is not an object"})
            continue

        name, name_error = validate_text(item.get("name"), field_name="name", max_length=120, required=True)
        if name_error:
            skipped += 1
            errors.append({"index": idx, "detail": name_error})
            continue
        location, location_error = validate_text(item.get("location"), field_name="location", max_length=255)
        if location_error:
            skipped += 1
            errors.append({"index": idx, "detail": location_error})
            continue
        description, description_error = validate_text(
            item.get("description"),
            field_name="description",
            max_length=2000,
            multiline=True,
        )
        if description_error:
            skipped += 1
            errors.append({"index": idx, "detail": description_error})
            continue
        scoring_text, scoring_text_error = validate_text(
            item.get("scoring_text"),
            field_name="scoring_text",
            max_length=4000,
            multiline=True,
        )
        if scoring_text_error:
            skipped += 1
            errors.append({"index": idx, "detail": scoring_text_error})
            continue

        action = (item.get("action") or "upsert").lower()
        cp = _checkpoint_query(comp_id).filter(Checkpoint.name == name).first()

        if action == "create" and cp:
            skipped += 1
            errors.append({"index": idx, "detail": "checkpoint already exists"})
            continue
        if action == "update" and not cp:
            skipped += 1
            errors.append({"index": idx, "detail": "checkpoint not found"})
            continue

        is_new = False
        if not cp:
            cp = Checkpoint(name=name, competition_id=comp_id)
            db.session.add(cp)
            db.session.flush()
            is_new = True

        if "is_virtual" in item:
            cp.is_virtual = bool(item.get("is_virtual"))

        if location is not None:
            cp.location = location
        if description is not None:
            cp.description = description
        if scoring_text is not None:
            cp.scoring_text = scoring_text

        if "easting" in item and item["easting"] not in (None, ""):
            easting, _err = validate_finite_float(item["easting"], field_name="easting")
            if easting is not None:
                cp.easting = easting
        if "northing" in item and item["northing"] not in (None, ""):
            northing, _err = validate_finite_float(item["northing"], field_name="northing")
            if northing is not None:
                cp.northing = northing

        if "group_ids" in item:
            group_ids = _parse_group_ids(item.get("group_ids"))
            _apply_groups(cp, group_ids)

        if "lora_device_id" in item:
            raw_device_id = item.get("lora_device_id")
            if raw_device_id in (None, "", "null"):
                _assign_lora_device(cp, None)
            else:
                try:
                    raw_device_id = int(raw_device_id)
                except Exception:
                    errors.append({"index": idx, "detail": "invalid lora_device_id"})
                    continue
                err = _assign_lora_device(cp, raw_device_id)
                if err:
                    errors.append({"index": idx, "detail": err.get("detail")})
                    continue

        created += 1 if is_new else 0
        updated += 0 if is_new else 1

    db.session.commit()

    return json_ok(
        {
            "ok": True,
            "summary": {
                "created": created,
                "updated": updated,
                "skipped": skipped,
            },
            "errors": errors,
        }
    )
