# app/resources/ingest.py
from __future__ import annotations
from datetime import datetime

from flask_restful import Resource, reqparse
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import (
    LoRaMessage, RFIDCard, Team, Checkpoint, Checkin, LoRaDevice
)
from app.utils.sheets_sync import mark_arrival_checkbox
from app.utils.payloads import parse_gps_payload

def resolve_checkpoint_for_dev(dev_num: int) -> Checkpoint:
    device = LoRaDevice.query.filter_by(dev_num=dev_num).first()

    if device:
        if device.checkpoint:
            return device.checkpoint

        cp = Checkpoint.query.filter_by(lora_device_id=device.id).first()
        if cp:
            return cp

        cp = Checkpoint(
            name=f"LoRa Gateway {dev_num}",
            description="Auto-created from LoRa ingest",
            lora_device_id=device.id,
        )
        db.session.add(cp)
        db.session.flush()
        return cp

    device = LoRaDevice(dev_num=dev_num, name=f"GW-{dev_num}", active=True)
    db.session.add(device)
    db.session.flush()

    cp = Checkpoint(
        name=f"LoRa Gateway {dev_num}",
        description="Auto-created from LoRa ingest",
        lora_device_id=device.id,
    )
    db.session.add(cp)
    db.session.flush()
    return cp


_parser = reqparse.RequestParser(bundle_errors=True)
_parser.add_argument("dev_id", type=int, required=True, help="dev_id is required (int).")
_parser.add_argument("payload", type=str, required=False)  # optional if gps_* provided
_parser.add_argument("rssi", type=float)
_parser.add_argument("snr", type=float)
_parser.add_argument("ts", type=int)  # unix seconds

# Optional GPS fields (allow posting structured GPS instead of string payload)
_parser.add_argument("gps_lat", type=float)
_parser.add_argument("gps_lon", type=float)
_parser.add_argument("gps_alt", type=float)
_parser.add_argument("gps_age_ms", type=int)

class IngestResource(Resource):
    def post(self):
        args = _parser.parse_args()  # supports JSON and form bodies
        dev_id  = args["dev_id"]
        payload = args.get("payload")
        rssi    = args.get("rssi")
        snr     = args.get("snr")
        ts_unix = args.get("ts")
        gps_lat = args.get("gps_lat")
        gps_lon = args.get("gps_lon")
        gps_alt = args.get("gps_alt")
        gps_age = args.get("gps_age_ms")

        received_at = datetime.utcfromtimestamp(ts_unix) if ts_unix else datetime.utcnow()

        # Accept either a raw payload string or structured GPS fields.
        if (payload is None or str(payload).strip() == "") and (gps_lat is not None and gps_lon is not None):
            # Normalize: create the same payload format sent by device firmware
            # pos,<lat>,<lon>,<alt>,<age_ms>
            lat = float(gps_lat)
            lon = float(gps_lon)
            alt = float(gps_alt) if gps_alt is not None else 0.0
            age = int(gps_age) if gps_age is not None else 0
            payload = f"pos,{lat:.6f},{lon:.6f},{alt:.1f},{age}"
        
        if payload is None or str(payload).strip() == "":
            return {
                "ok": False,
                "error": "invalid_request",
                "detail": "Provide either 'payload' or ('gps_lat' and 'gps_lon').",
            }, 400

        try:
            # 1) Store raw message
            msg = LoRaMessage(
                dev_id=int(dev_id),
                payload=str(payload),
                rssi=float(rssi) if rssi is not None else None,
                snr=float(snr) if snr is not None else None,
                received_at=received_at,
            )
            db.session.add(msg)

            # 2) Update device telemetry + resolve checkpoint
            device = LoRaDevice.query.filter_by(dev_num=int(dev_id)).first()
            if device:
                device.last_seen = received_at
                if rssi is not None:
                    device.last_rssi = float(rssi)

            cp = resolve_checkpoint_for_dev(int(dev_id))

            # 3) Auto check-in if payload matches RFID UID
            uid = str(payload).strip().upper()
            card = RFIDCard.query.filter_by(uid=uid).first()

            created_checkin = False
            team_name = None
            checkpoint_name = cp.name if cp else None

            if card:
                team = db.session.get(Team, card.team_id)
                if team and cp:
                    team_name = team.name
                    exists = Checkin.query.filter_by(
                        team_id=team.id, checkpoint_id=cp.id
                    ).first()
                    if not exists:
                        db.session.add(Checkin(
                            team_id=team.id,
                            checkpoint_id=cp.id,
                            timestamp=received_at
                        ))
                        created_checkin = True
                        try:
                            mark_arrival_checkbox(team.id, cp.id)
                        except Exception as exc:
                            # do not fail ingest if Sheets update fails
                            pass

            db.session.commit()

        except SQLAlchemyError as e:
            db.session.rollback()
            return {
                "ok": False,
                "error": "database_error",
                "detail": str(e.__class__.__name__),
            }, 500

        resp = {
            "ok": True,
            "message_id": msg.id,
            "dev_id": int(dev_id),
            "uid_seen": bool(card),
            "team": team_name,
            "checkpoint": checkpoint_name,
            "checkin_created": created_checkin,
        }

        # Optional: include structured GPS if payload matches expected format
        gps = parse_gps_payload(msg.payload)
        if gps is not None:
            resp["gps"] = gps

        headers = {"Location": f"/api/messages/{msg.id}"}  # optional future resource
        return resp, 201, headers
