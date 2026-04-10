# app/resources/lora.py
from __future__ import annotations

from flask import Blueprint, jsonify, request
from flask_login import current_user
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import LoRaDevice, Checkpoint
from app.utils.audit import record_audit_event
from app.utils.rest_auth import json_login_required, json_roles_required
from app.utils.competition import require_current_competition_id
from app.utils.validators import validate_text

lora_devices_api_bp = Blueprint("api_lora_devices", __name__)


def _serialize_device(device: LoRaDevice) -> dict:
    return {
        "id": device.id,
        "dev_num": device.dev_num,
        "name": device.name,
        "note": device.note,
        "model": device.model,
        "active": bool(device.active),
        "last_seen": device.last_seen.isoformat() if device.last_seen else None,
        "last_rssi": device.last_rssi,
        "battery": device.battery,
        "checkpoint": {
            "id": device.checkpoint.id,
            "name": device.checkpoint.name,
            "description": device.checkpoint.description,
        } if device.checkpoint else None,
    }


def _device_snapshot(device: LoRaDevice) -> dict:
    return _serialize_device(device)


def _parse_device_payload(payload: dict, *, for_update: bool = False) -> tuple[dict, list[str]]:
    errors = []
    data = {}

    dev_num = payload.get("dev_num")
    if dev_num is None and not for_update:
        errors.append("dev_num is required")
    elif dev_num is not None:
        try:
            data["dev_num"] = int(dev_num)
        except Exception:
            errors.append("dev_num must be an integer")

    if "name" in payload or not for_update:
        name = payload.get("name")
        cleaned_name, name_error = validate_text(name, field_name="name", max_length=120)
        if name_error:
            errors.append(name_error)
        data["name"] = cleaned_name

    if "note" in payload or not for_update:
        note = payload.get("note")
        cleaned_note, note_error = validate_text(
            note,
            field_name="note",
            max_length=2000,
            multiline=True,
        )
        if note_error:
            errors.append(note_error)
        data["note"] = cleaned_note

    if "model" in payload or not for_update:
        model = payload.get("model")
        cleaned_model, model_error = validate_text(model, field_name="model", max_length=64)
        if model_error:
            errors.append(model_error)
        data["model"] = cleaned_model

    if "active" in payload:
        data["active"] = bool(payload.get("active"))
    elif not for_update:
        data["active"] = True

    return data, errors


@lora_devices_api_bp.get("/api/lora/devices")
@lora_devices_api_bp.get("/api/devices")
@json_login_required
def lora_device_list():
        comp_id = require_current_competition_id()
        if not comp_id:
            return jsonify({"error": "no_competition"}), 400
        devices = (
            LoRaDevice.query
            .filter(LoRaDevice.competition_id == comp_id)
            .options(joinedload(LoRaDevice.checkpoint))
            .order_by(LoRaDevice.name.asc().nulls_last(), LoRaDevice.dev_num.asc())
            .all()
        )
        return {"devices": [_serialize_device(d) for d in devices]}, 200


@lora_devices_api_bp.post("/api/lora/devices")
@lora_devices_api_bp.post("/api/devices")
@json_roles_required("judge", "admin")
def lora_device_create():
        comp_id = require_current_competition_id()
        if not comp_id:
            return jsonify({"error": "no_competition"}), 400
        payload = request.get_json(silent=True) or {}
        data, errors = _parse_device_payload(payload, for_update=False)
        if errors:
            return jsonify({"error": "validation_error", "detail": errors}), 400

        dev_num = data.get("dev_num")
        if (
            LoRaDevice.query
            .filter(LoRaDevice.competition_id == comp_id, LoRaDevice.dev_num == dev_num)
            .first()
        ):
            return jsonify({"error": "conflict", "detail": "Device number already exists."}), 409

        device = LoRaDevice(
            competition_id=comp_id,
            dev_num=data.get("dev_num"),
            name=data.get("name"),
            note=data.get("note"),
            model=data.get("model"),
            active=data.get("active", True),
        )
        db.session.add(device)
        db.session.flush()
        record_audit_event(
            competition_id=comp_id,
            event_type="device_created",
            entity_type="device",
            entity_id=device.id,
            actor_user=current_user if current_user.is_authenticated else None,
            summary=f"Device {device.name or f'DEV-{device.dev_num}'} created.",
            details=_device_snapshot(device),
        )
        db.session.commit()
        return {"ok": True, "device": _serialize_device(device)}, 201


@lora_devices_api_bp.get("/api/lora/devices/<int:device_id>")
@lora_devices_api_bp.get("/api/devices/<int:device_id>")
@json_login_required
def lora_device_get(device_id: int):
        comp_id = require_current_competition_id()
        if not comp_id:
            return jsonify({"error": "no_competition"}), 400
        device = (
            LoRaDevice.query
            .filter(LoRaDevice.competition_id == comp_id, LoRaDevice.id == device_id)
            .options(joinedload(LoRaDevice.checkpoint))
            .first()
        )
        if not device:
            return jsonify({"error": "not_found"}), 404
        return _serialize_device(device), 200


def _update_device(device_id: int, partial: bool):
        comp_id = require_current_competition_id()
        if not comp_id:
            return jsonify({"error": "no_competition"}), 400
        device = LoRaDevice.query.filter(
            LoRaDevice.competition_id == comp_id, LoRaDevice.id == device_id
        ).first()
        if not device:
            return jsonify({"error": "not_found"}), 404
        before = _device_snapshot(device)

        payload = request.get_json(silent=True) or {}
        data, errors = _parse_device_payload(payload, for_update=True)
        if errors:
            return jsonify({"error": "validation_error", "detail": errors}), 400

        if "dev_num" in data:
            dev_num = data["dev_num"]
            if dev_num is None:
                return jsonify({"error": "validation_error", "detail": "dev_num is required"}), 400
            exists = LoRaDevice.query.filter(
                LoRaDevice.competition_id == comp_id,
                LoRaDevice.dev_num == dev_num,
                LoRaDevice.id != device.id,
            ).first()
            if exists:
                return jsonify({"error": "conflict", "detail": "Device number already exists."}), 409
            device.dev_num = dev_num

        for field in ("name", "note", "model", "active"):
            if field in data:
                setattr(device, field, data[field])

        db.session.flush()
        record_audit_event(
            competition_id=comp_id,
            event_type="device_updated",
            entity_type="device",
            entity_id=device.id,
            actor_user=current_user if current_user.is_authenticated else None,
            summary=f"Device {device.name or f'DEV-{device.dev_num}'} updated.",
            details={"before": before, "after": _device_snapshot(device)},
        )
        db.session.commit()
        return {"ok": True, "device": _serialize_device(device)}, 200


@lora_devices_api_bp.patch("/api/lora/devices/<int:device_id>")
@lora_devices_api_bp.patch("/api/devices/<int:device_id>")
@json_roles_required("judge", "admin")
def lora_device_patch(device_id: int):
    return _update_device(device_id, partial=True)


@lora_devices_api_bp.put("/api/lora/devices/<int:device_id>")
@lora_devices_api_bp.put("/api/devices/<int:device_id>")
@json_roles_required("judge", "admin")
def lora_device_put(device_id: int):
    return _update_device(device_id, partial=False)


@lora_devices_api_bp.delete("/api/lora/devices/<int:device_id>")
@lora_devices_api_bp.delete("/api/devices/<int:device_id>")
@json_roles_required("admin")
def lora_device_delete(device_id: int):
        comp_id = require_current_competition_id()
        if not comp_id:
            return jsonify({"error": "no_competition"}), 400
        device = LoRaDevice.query.filter(
            LoRaDevice.competition_id == comp_id, LoRaDevice.id == device_id
        ).first()
        if not device:
            return jsonify({"error": "not_found"}), 404

        checkpoint = device.checkpoint
        if checkpoint:
            checkpoint.lora_device = None

        snapshot = _device_snapshot(device)
        record_audit_event(
            competition_id=comp_id,
            event_type="device_deleted",
            entity_type="device",
            entity_id=device.id,
            actor_user=current_user if current_user.is_authenticated else None,
            summary=f"Device {device.name or f'DEV-{device.dev_num}'} deleted.",
            details=snapshot,
        )
        db.session.delete(device)
        db.session.commit()
        return {"ok": True}, 200
