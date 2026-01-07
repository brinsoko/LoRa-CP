# app/resources/ingest.py
from __future__ import annotations
from datetime import datetime
import hashlib, hmac

from flask import current_app

from flask_restful import Resource, reqparse
from sqlalchemy.exc import SQLAlchemyError

from app.extensions import db
from app.models import (
    LoRaMessage, RFIDCard, Team, Checkpoint, Checkin, LoRaDevice
)
from app.utils.sheets_sync import mark_arrival_checkbox
from app.utils.payloads import parse_gps_payload
from app.utils.card_tokens import compute_card_digest

def resolve_checkpoint_for_dev(competition_id: int, dev_num: int) -> Checkpoint:
    device = LoRaDevice.query.filter_by(competition_id=competition_id, dev_num=dev_num).first()

    if device:
        if device.checkpoint:
            return device.checkpoint

        cp = Checkpoint.query.filter_by(lora_device_id=device.id).first()
        if cp:
            return cp

        cp = Checkpoint(
            competition_id=competition_id,
            name=f"Device {dev_num}",
            description="Auto-created from device ingest",
            lora_device_id=device.id,
        )
        db.session.add(cp)
        db.session.flush()
        return cp

    device = LoRaDevice(competition_id=competition_id, dev_num=dev_num, name=f"DEV-{dev_num}", active=True)
    db.session.add(device)
    db.session.flush()

    cp = Checkpoint(
        competition_id=competition_id,
        name=f"Device {dev_num}",
        description="Auto-created from device ingest",
        lora_device_id=device.id,
    )
    db.session.add(cp)
    db.session.flush()
    return cp


_parser = reqparse.RequestParser(bundle_errors=True)
_parser.add_argument("competition_id", type=int, required=True, help="competition_id is required (int).")
_parser.add_argument("dev_id", type=int, required=False)
_parser.add_argument("checkpoint_id", type=int, required=False)
_parser.add_argument("payload", type=str, required=False)  # optional if gps_* provided
_parser.add_argument("rssi", type=float)
_parser.add_argument("snr", type=float)
_parser.add_argument("ts", type=int)  # unix seconds
_parser.add_argument("source", type=str)  # optional client hint (e.g., mobile)

# Optional GPS fields (allow posting structured GPS instead of string payload)
_parser.add_argument("gps_lat", type=float)
_parser.add_argument("gps_lon", type=float)
_parser.add_argument("gps_alt", type=float)
_parser.add_argument("gps_age_ms", type=int)


def _card_writeback(uid: str,
                    dev_id: int,
                    checkpoint: Checkpoint | None,
                    team: Team | None,
                    received_at: datetime) -> dict | None:
    """
    Build a short, signed payload that an Android phone can write to the RFID card.
    Format: "<dev_id>|<uid>|<ts>|<hmac>" so clients can verify offline.
    """
    if not uid:
        return None

    digest_short = compute_card_digest(uid, dev_id)
    if not digest_short:
        return None
    payload = digest_short  # we only write the truncated HMAC to the tag
    return {
        "payload": payload,
        "hmac": digest_short,
        "device_id": dev_id,
        "card_uid": uid,
        "checkpoint_id": checkpoint.id if checkpoint else None,
        "checkpoint": checkpoint.name if checkpoint else None,
        "team_id": team.id if team else None,
        "team": team.name if team else None,
    }

class IngestResource(Resource):
    def post(self):
        args = _parser.parse_args()  # supports JSON and form bodies
        competition_id = args["competition_id"]
        dev_id  = args.get("dev_id")
        checkpoint_id = args.get("checkpoint_id")
        payload = args.get("payload")
        rssi    = args.get("rssi")
        snr     = args.get("snr")
        ts_unix = args.get("ts")
        gps_lat = args.get("gps_lat")
        gps_lon = args.get("gps_lon")
        gps_alt = args.get("gps_alt")
        gps_age = args.get("gps_age_ms")

        received_at = datetime.utcfromtimestamp(ts_unix) if ts_unix else datetime.utcnow()

        if dev_id is None and checkpoint_id is None:
            return {
                "ok": False,
                "error": "invalid_request",
                "detail": "Provide either 'dev_id' or 'checkpoint_id'.",
            }, 400

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

        card_writeback = None

        try:
            # 1) Store raw message
            dev_id_str = str(dev_id) if dev_id is not None else f"checkpoint:{checkpoint_id}"
            msg = LoRaMessage(
                competition_id=competition_id,
                dev_id=dev_id_str,
                payload=str(payload),
                rssi=float(rssi) if rssi is not None else None,
                snr=float(snr) if snr is not None else None,
                received_at=received_at,
            )
            db.session.add(msg)

            # 2) Update device telemetry + resolve checkpoint
            cp = None
            if dev_id is not None:
                device = LoRaDevice.query.filter_by(
                    competition_id=competition_id, dev_num=int(dev_id)
                ).first()
                if device:
                    device.last_seen = received_at
                    if rssi is not None:
                        device.last_rssi = float(rssi)
                cp = resolve_checkpoint_for_dev(competition_id, int(dev_id))
            elif checkpoint_id is not None:
                cp = Checkpoint.query.filter(
                    Checkpoint.competition_id == competition_id,
                    Checkpoint.id == checkpoint_id,
                ).first()
                if not cp:
                    db.session.rollback()
                    return {
                        "ok": False,
                        "error": "invalid_request",
                        "detail": "Invalid checkpoint_id.",
                    }, 400

            # 3) Auto check-in if payload matches RFID UID
            uid = str(payload).strip().upper()
            card = RFIDCard.query.filter_by(uid=uid).first()

            created_checkin = False
            team_name = None
            checkpoint_name = cp.name if cp else None
            team_obj = None

            if card:
                team = db.session.get(Team, card.team_id)
                if team and cp and team.competition_id == competition_id and cp.competition_id == competition_id:
                    team_obj = team
                    team_name = team.name
                    exists = Checkin.query.filter_by(
                        team_id=team.id,
                        checkpoint_id=cp.id,
                        competition_id=competition_id,
                    ).first()
                    arrived_at = received_at
                    if not exists:
                        db.session.add(
                            Checkin(
                                team_id=team.id,
                                checkpoint_id=cp.id,
                                competition_id=competition_id,
                                timestamp=received_at,
                            )
                        )
                        created_checkin = True
                    else:
                        arrived_at = exists.timestamp or received_at

                    try:
                        mark_arrival_checkbox(team.id, cp.id, arrived_at)
                    except Exception:
                        # do not fail ingest if Sheets update fails
                        pass

            looks_like_uid = gps_lat is None and gps_lon is None and (payload is not None) and ("," not in str(payload))
            if looks_like_uid:
                digest_id = int(dev_id) if dev_id is not None else int(checkpoint_id or 0)
                card_writeback = _card_writeback(uid, digest_id, cp, team_obj, received_at)

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
            "dev_id": int(dev_id) if dev_id is not None else None,
            "uid_seen": bool(card),
            "team": team_name,
            "checkpoint": checkpoint_name,
            "checkin_created": created_checkin,
        }

        # Optional: include structured GPS if payload matches expected format
        gps = parse_gps_payload(msg.payload)
        if gps is not None:
            resp["gps"] = gps
        if card_writeback:
            resp["card_writeback"] = card_writeback

        headers = {"Location": f"/api/messages/{msg.id}"}  # optional future resource
        return resp, 201, headers
