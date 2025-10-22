# ingest.py
from __future__ import annotations
from flask import Blueprint, request, jsonify, abort
from datetime import datetime
from sqlalchemy import func

from app.extensions import db
from app.models import LoRaMessage, RFIDCard, Team, Checkpoint, Checkin, LoRaDevice

ingest_bp = Blueprint("ingest", __name__)

def resolve_checkpoint_for_dev(dev_num: int) -> Checkpoint:
    """
    Resolve a Checkpoint for a given dev_num using the LoRaDevice mapping.
    - If LoRaDevice exists and has a checkpoint, return it.
    - If LoRaDevice exists but has no checkpoint, create/link a placeholder checkpoint once.
    - If LoRaDevice does not exist, create it and a placeholder checkpoint and link them.
    """
    device = LoRaDevice.query.filter_by(dev_num=dev_num).first()

    if device:
        # if checkpoint already linked, use it
        if device.checkpoint:
            return device.checkpoint

        # maybe there is an existing checkpoint already pointing at this device id
        cp = Checkpoint.query.filter_by(lora_device_id=device.id).first()
        if cp:
            return cp

        # otherwise create a placeholder and link it
        cp = Checkpoint(
            name=f"LoRa Gateway {dev_num}",
            description="Auto-created from LoRa ingest",
            lora_device_id=device.id,
        )
        db.session.add(cp)
        db.session.flush()
        return cp

    # No device yet → create one, then a placeholder checkpoint
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

@ingest_bp.post("/api/ingest")
def ingest():
    # Accept JSON or form
    if request.is_json:
        data = request.get_json(silent=True) or {}
        dev_id  = data.get("dev_id")
        payload = data.get("payload", "")
        rssi    = data.get("rssi")
        snr     = data.get("snr")
        ts_unix = data.get("ts")
    else:
        dev_id  = request.form.get("dev_id", type=int)
        payload = request.form.get("payload", "", type=str)
        rssi    = request.form.get("rssi", type=float)
        snr     = request.form.get("snr", type=float)
        ts_unix = request.form.get("ts", type=int)

    if dev_id is None or payload is None:
        abort(400, "dev_id and payload required")

    received_at = datetime.utcfromtimestamp(ts_unix) if ts_unix else datetime.utcnow()

    # Save raw message
    msg = LoRaMessage(
        dev_id=int(dev_id),
        payload=str(payload),
        rssi=float(rssi) if rssi is not None else None,
        snr=float(snr) if snr is not None else None,
        received_at=received_at,
    )
    db.session.add(msg)

    # Update device telemetry + resolve checkpoint strictly via device mapping
    device = LoRaDevice.query.filter_by(dev_num=int(dev_id)).first()
    if device:
        device.last_seen = received_at
        if rssi is not None:
            device.last_rssi = float(rssi)
    cp = resolve_checkpoint_for_dev(int(dev_id))

    # Auto check-in
    uid = str(payload).strip().upper()
    card = RFIDCard.query.filter_by(uid=uid).first()
    created_checkin = False
    team_name = None
    checkpoint_name = cp.name if cp else None

    if card:
        team = db.session.get(Team, card.team_id)
        if team and cp:
            team_name = team.name
            exists = Checkin.query.filter_by(team_id=team.id, checkpoint_id=cp.id).first()
            if not exists:
                db.session.add(Checkin(team_id=team.id, checkpoint_id=cp.id, timestamp=received_at))
                created_checkin = True

    db.session.commit()

    return jsonify({
        "ok": True,
        "message_id": msg.id,
        "dev_id": int(dev_id),
        "uid_seen": bool(card),
        "team": team_name,
        "checkpoint": checkpoint_name,
        "checkin_created": created_checkin,
    })