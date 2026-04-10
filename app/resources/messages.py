# app/resources/messages.py
from __future__ import annotations

from flask import Blueprint, jsonify, request

from app.extensions import db
from app.models import LoRaMessage
from app.utils.payloads import parse_gps_payload
from app.utils.rest_auth import json_roles_required
from app.utils.competition import require_current_competition_id

messages_api_bp = Blueprint("api_device_messages", __name__)


def _serialize_message(msg: LoRaMessage) -> dict:
    data = {
        "id": msg.id,
        "dev_id": msg.dev_id,
        "payload": msg.payload,
        "rssi": msg.rssi,
        "snr": msg.snr,
        "received_at": msg.received_at.isoformat() if msg.received_at else None,
    }
    gps = parse_gps_payload(msg.payload)
    if gps is not None:
        data["gps"] = gps
    return data


@messages_api_bp.get("/api/lora/messages")
@messages_api_bp.get("/api/devices/messages")
@json_roles_required("admin")
def lora_message_list():
        comp_id = require_current_competition_id()
        if not comp_id:
            return jsonify({"error": "no_competition"}), 400
        dev_id = request.args.get("dev_id")
        page = request.args.get("page", 1, type=int)
        per_page = min(request.args.get("per_page", 50, type=int), 200)

        query = (
            LoRaMessage.query
            .filter(LoRaMessage.competition_id == comp_id)
            .order_by(LoRaMessage.received_at.desc())
        )
        if dev_id:
            query = query.filter(LoRaMessage.dev_id == dev_id)

        pagination = query.paginate(page=page, per_page=per_page, error_out=False)

        return {
            "messages": [_serialize_message(m) for m in pagination.items],
            "meta": {
                "page": pagination.page,
                "per_page": pagination.per_page,
                "total": pagination.total,
                "pages": pagination.pages,
            },
        }, 200
