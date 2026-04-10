# app/resources/map.py
from __future__ import annotations
from flask import Blueprint, jsonify, request
from werkzeug.exceptions import BadRequest
from app.extensions import db
from app.models import LoRaMessage, Team
from app.api.helpers import parse_int
from app.utils.payloads import parse_gps_payload
from app.utils.rest_auth import json_roles_required
from app.utils.competition import require_current_competition_id

from app.utils.status import all_checkpoints_for_map, compute_team_statuses

map_api_bp = Blueprint("api_map", __name__)


def _optional_query_int(name: str):
    raw = request.args.get(name)
    if raw in (None, ""):
        return None
    try:
        return parse_int(raw, name)
    except BadRequest as exc:
        raise BadRequest() from exc

@map_api_bp.get("/api/map/checkpoints")
@json_roles_required("judge", "admin")
def map_checkpoints():
        comp_id = require_current_competition_id()
        if not comp_id:
            return jsonify({"error": "no_competition"}), 400
        team_id = _optional_query_int("team_id")

        if team_id:
            team = Team.query.filter(Team.competition_id == comp_id, Team.id == team_id).first()
            if not team:
                return jsonify({"error": "not_found", "detail": "Team not found."}), 404
            status_summary = compute_team_statuses(team_id, comp_id)
            return status_summary.get("checkpoints", []), 200

        cps = all_checkpoints_for_map(comp_id)
        return [
            {
                "id": cp["id"],
                "name": cp["name"],
                "easting": cp["easting"],
                "northing": cp["northing"],
                "status": "not_found",
                "order": index,
                "location": cp.get("location"),
            }
            for index, cp in enumerate(cps)
        ], 200


# ---- LoRa GPS points for map ----
@map_api_bp.get("/api/map/lora-points")
@map_api_bp.get("/api/map/device-points")
@json_roles_required("judge", "admin")
def lora_map_points():
        comp_id = require_current_competition_id()
        if not comp_id:
            return jsonify({"error": "no_competition"}), 400
        dev_id = _optional_query_int("dev_id")
        limit = _optional_query_int("limit") or 50
        limit = max(1, min(500, limit))

        def serialize(msg: LoRaMessage):
            gps = parse_gps_payload(msg.payload)
            if not gps:
                return None
            return {
                "dev_id": msg.dev_id,
                "lat": gps["lat"],
                "lon": gps["lon"],
                "alt": gps["alt"],
                "age_ms": gps["age_ms"],
                "rssi": msg.rssi,
                "snr": msg.snr,
                "received_at": msg.received_at.isoformat() if msg.received_at else None,
            }

        q = LoRaMessage.query.filter(
            LoRaMessage.payload.like("pos,%"),
            LoRaMessage.competition_id == comp_id,
        )
        if dev_id is not None:
            # Store dev_id as string in DB
            q = q.filter(LoRaMessage.dev_id == str(dev_id))
            q = q.order_by(LoRaMessage.received_at.desc()).limit(limit)
            items = [serialize(m) for m in q.all()]
            return [i for i in items if i is not None], 200

        # Latest point per device
        q = q.order_by(LoRaMessage.dev_id.asc(), LoRaMessage.received_at.desc()).limit(2000)
        latest_by_dev = {}
        for m in q.all():
            if m.dev_id not in latest_by_dev:
                item = serialize(m)
                if item:
                    latest_by_dev[m.dev_id] = item
        return list(latest_by_dev.values()), 200
