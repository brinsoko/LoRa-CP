# app/resources/messages.py
from __future__ import annotations

from flask import request
from flask_restful import Resource

from app.extensions import db
from app.models import LoRaMessage
from app.utils.rest_auth import json_roles_required


def _serialize_message(msg: LoRaMessage) -> dict:
    return {
        "id": msg.id,
        "dev_id": msg.dev_id,
        "payload": msg.payload,
        "rssi": msg.rssi,
        "snr": msg.snr,
        "received_at": msg.received_at.isoformat() if msg.received_at else None,
    }


class LoRaMessageListResource(Resource):
    method_decorators = [json_roles_required("admin")]

    def get(self):
        dev_id = request.args.get("dev_id")
        page = request.args.get("page", 1, type=int)
        per_page = min(request.args.get("per_page", 50, type=int), 200)

        query = LoRaMessage.query.order_by(LoRaMessage.received_at.desc())
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
