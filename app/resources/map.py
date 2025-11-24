# app/resources/map.py
from __future__ import annotations
from flask_restful import Resource, reqparse
from app.extensions import db
from app.models import LoRaMessage
from app.utils.payloads import parse_gps_payload

from app.utils.status import all_checkpoints_for_map, compute_team_statuses

_parser = reqparse.RequestParser(trim=True, bundle_errors=True)
_parser.add_argument("team_id", type=int, required=False, location="args")

class MapCheckpoints(Resource):
    """
    GET /api/map/checkpoints
      - No team_id: returns ALL checkpoints (id, name, easting, northing)
      - team_id=N: returns ONLY checkpoints that team N has checked in at
    """
    def get(self):
        args = _parser.parse_args()
        team_id = args.get("team_id")

        if team_id:
            status_summary = compute_team_statuses(team_id)
            return status_summary.get("checkpoints", []), 200

        cps = all_checkpoints_for_map()
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
_lora_parser = reqparse.RequestParser(trim=True, bundle_errors=True)
_lora_parser.add_argument("dev_id", type=int, required=False, location="args")
_lora_parser.add_argument("limit", type=int, required=False, location="args")


class LoRaMapPoints(Resource):
    """
    GET /api/map/lora-points
      - dev_id omitted: latest GPS point per device that has GPS payloads
      - dev_id=N: up to `limit` recent points for the given device (default 50)
    """
    def get(self):
        args = _lora_parser.parse_args()
        dev_id = args.get("dev_id")
        limit = args.get("limit") or 50
        limit = max(1, min(500, int(limit)))

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

        q = LoRaMessage.query.filter(LoRaMessage.payload.like("pos,%"))
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
