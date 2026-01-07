# app/resources/lora.py
from __future__ import annotations

from flask import request
from flask_restful import Resource
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import LoRaDevice, Checkpoint
from app.utils.rest_auth import json_login_required, json_roles_required
from app.utils.competition import require_current_competition_id


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
        data["name"] = name.strip() if isinstance(name, str) else None

    if "note" in payload or not for_update:
        note = payload.get("note")
        data["note"] = note.strip() if isinstance(note, str) else None

    if "model" in payload or not for_update:
        model = payload.get("model")
        data["model"] = model.strip() if isinstance(model, str) else None

    if "active" in payload:
        data["active"] = bool(payload.get("active"))
    elif not for_update:
        data["active"] = True

    return data, errors


class LoRaDeviceListResource(Resource):
    method_decorators = [json_login_required]

    def get(self):
        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400
        devices = (
            LoRaDevice.query
            .filter(LoRaDevice.competition_id == comp_id)
            .options(joinedload(LoRaDevice.checkpoint))
            .order_by(LoRaDevice.name.asc().nulls_last(), LoRaDevice.dev_num.asc())
            .all()
        )
        return {"devices": [_serialize_device(d) for d in devices]}, 200

    @json_roles_required("judge", "admin")
    def post(self):
        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400
        payload = request.get_json(silent=True) or {}
        data, errors = _parse_device_payload(payload, for_update=False)
        if errors:
            return {"error": "validation_error", "detail": errors}, 400

        dev_num = data.get("dev_num")
        if (
            LoRaDevice.query
            .filter(LoRaDevice.competition_id == comp_id, LoRaDevice.dev_num == dev_num)
            .first()
        ):
            return {"error": "conflict", "detail": "Device number already exists."}, 409

        device = LoRaDevice(
            competition_id=comp_id,
            dev_num=data.get("dev_num"),
            name=data.get("name"),
            note=data.get("note"),
            model=data.get("model"),
            active=data.get("active", True),
        )
        db.session.add(device)
        db.session.commit()
        return {"ok": True, "device": _serialize_device(device)}, 201


class LoRaDeviceItemResource(Resource):
    method_decorators = [json_login_required]

    def get(self, device_id: int):
        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400
        device = (
            LoRaDevice.query
            .filter(LoRaDevice.competition_id == comp_id, LoRaDevice.id == device_id)
            .options(joinedload(LoRaDevice.checkpoint))
            .first()
        )
        if not device:
            return {"error": "not_found"}, 404
        return _serialize_device(device), 200

    @json_roles_required("judge", "admin")
    def patch(self, device_id: int):
        return self._update(device_id, partial=True)

    @json_roles_required("judge", "admin")
    def put(self, device_id: int):
        return self._update(device_id, partial=False)

    def _update(self, device_id: int, partial: bool):
        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400
        device = LoRaDevice.query.filter(
            LoRaDevice.competition_id == comp_id, LoRaDevice.id == device_id
        ).first()
        if not device:
            return {"error": "not_found"}, 404

        payload = request.get_json(silent=True) or {}
        data, errors = _parse_device_payload(payload, for_update=True)
        if errors:
            return {"error": "validation_error", "detail": errors}, 400

        if "dev_num" in data:
            dev_num = data["dev_num"]
            if dev_num is None:
                return {"error": "validation_error", "detail": "dev_num is required"}, 400
            exists = LoRaDevice.query.filter(
                LoRaDevice.competition_id == comp_id,
                LoRaDevice.dev_num == dev_num,
                LoRaDevice.id != device.id,
            ).first()
            if exists:
                return {"error": "conflict", "detail": "Device number already exists."}, 409
            device.dev_num = dev_num

        for field in ("name", "note", "model", "active"):
            if field in data:
                setattr(device, field, data[field])

        db.session.commit()
        return {"ok": True, "device": _serialize_device(device)}, 200

    @json_roles_required("admin")
    def delete(self, device_id: int):
        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400
        device = LoRaDevice.query.filter(
            LoRaDevice.competition_id == comp_id, LoRaDevice.id == device_id
        ).first()
        if not device:
            return {"error": "not_found"}, 404

        checkpoint = device.checkpoint
        if checkpoint:
            checkpoint.lora_device = None

        db.session.delete(device)
        db.session.commit()
        return {"ok": True}, 200
