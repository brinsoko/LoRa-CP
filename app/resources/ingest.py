# app/resources/ingest.py
from __future__ import annotations

import hmac
from datetime import datetime, timedelta

from flask import Blueprint, current_app, request
from flask_login import current_user
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from werkzeug.exceptions import BadRequest

from app.api.helpers import parse_int
from app.extensions import db
from app.models import (
    Checkin,
    Checkpoint,
    Competition,
    CompetitionMember,
    LoRaDevice,
    LoRaMessage,
    RFIDCard,
    Team,
)
from app.utils.audit import format_device_label, record_audit_event
from app.utils.card_tokens import compute_card_digest
from app.utils.payloads import parse_gps_payload
from app.utils.sheets_sync import mark_arrival_checkbox
from app.utils.time import utc_from_timestamp_naive, utcnow_naive

ingest_api_bp = Blueprint("api_ingest", __name__)


def resolve_checkpoint_for_dev(competition_id: int, dev_num: int) -> tuple[Checkpoint, LoRaDevice, bool, bool]:
    device = LoRaDevice.query.filter_by(competition_id=competition_id, dev_num=dev_num).first()
    created_device = False
    created_checkpoint = False

    if device:
        if device.checkpoint:
            return device.checkpoint, device, created_device, created_checkpoint

        cp = Checkpoint.query.filter_by(lora_device_id=device.id).first()
        if cp:
            return cp, device, created_device, created_checkpoint

        cp = Checkpoint(
            competition_id=competition_id,
            name=f"Device {dev_num}",
            description="Auto-created from device ingest",
            lora_device_id=device.id,
        )
        db.session.add(cp)
        db.session.flush()
        created_checkpoint = True
        return cp, device, created_device, created_checkpoint

    device = LoRaDevice(competition_id=competition_id, dev_num=dev_num, name=f"DEV-{dev_num}", active=True)
    db.session.add(device)
    db.session.flush()
    created_device = True

    cp = Checkpoint(
        competition_id=competition_id,
        name=f"Device {dev_num}",
        description="Auto-created from device ingest",
        lora_device_id=device.id,
    )
    db.session.add(cp)
    db.session.flush()
    created_checkpoint = True
    return cp, device, created_device, created_checkpoint


def _optional_int(payload: dict, key: str, *, positive: bool = False):
    raw = payload.get(key)
    if raw in (None, ""):
        return None
    try:
        parsed = parse_int(raw, key)
    except BadRequest as exc:
        raise BadRequest(description=f"invalid {key}") from exc
    if positive and parsed <= 0:
        raise BadRequest(description=f"{key} must be > 0")
    return parsed


def _optional_float(payload: dict, key: str, *, minimum: float | None = None, maximum: float | None = None):
    raw = payload.get(key)
    if raw in (None, ""):
        return None
    try:
        parsed = float(raw)
    except (TypeError, ValueError) as exc:
        raise BadRequest(description=f"invalid {key}") from exc
    import math

    if not math.isfinite(parsed):
        raise BadRequest(description=f"{key} must be a finite number")
    if minimum is not None and parsed < minimum:
        raise BadRequest(description=f"{key} must be >= {minimum}")
    if maximum is not None and parsed > maximum:
        raise BadRequest(description=f"{key} must be <= {maximum}")
    return parsed


def _parse_ingest_payload() -> dict:
    payload = request.get_json(force=True, silent=True)
    if not isinstance(payload, dict):
        payload = request.form.to_dict()
    if not isinstance(payload, dict):
        payload = {}

    raw_competition_id = payload.get("competition_id")
    if raw_competition_id in (None, ""):
        raise BadRequest()

    try:
        competition_id = parse_int(raw_competition_id, "competition_id")
    except BadRequest as exc:
        raise BadRequest() from exc

    return {
        "competition_id": competition_id,
        "dev_id": _optional_int(payload, "dev_id", positive=True),
        "checkpoint_id": _optional_int(payload, "checkpoint_id", positive=True),
        "payload": payload.get("payload"),
        "rssi": _optional_float(payload, "rssi"),
        "snr": _optional_float(payload, "snr"),
        "ts": _optional_int(payload, "ts"),
        "source": payload.get("source"),
        "ingest_password": payload.get("ingest_password"),
        "password": payload.get("password"),
        "gps_lat": _optional_float(payload, "gps_lat", minimum=-90.0, maximum=90.0),
        "gps_lon": _optional_float(payload, "gps_lon", minimum=-180.0, maximum=180.0),
        # Altitude in metres — Mt. Everest is 8848, the deepest mine ~4000 below
        # sea level. ±20000 is generous and rejects garbage like 1e308.
        "gps_alt": _optional_float(payload, "gps_alt", minimum=-20000.0, maximum=20000.0),
        "gps_age_ms": _optional_int(payload, "gps_age_ms"),
    }


def _card_writeback(
    uid: str, dev_id: int, checkpoint: Checkpoint | None, team: Team | None, received_at: datetime
) -> dict | None:
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


def _ingest_member_can_bypass_secret(user, competition_id: int) -> bool:
    """A logged-in user may skip the webhook secret only if they are an
    active admin or judge of the target competition. Viewers are read-only."""
    if not user or not user.is_authenticated:
        return False
    membership = CompetitionMember.query.filter_by(
        competition_id=competition_id,
        user_id=user.id,
        active=True,
    ).first()
    if not membership:
        return False
    return (membership.role or "").strip().lower() in {"admin", "judge"}


@ingest_api_bp.post("/api/ingest")
def ingest_post():
    args = _parse_ingest_payload()
    competition_id = args["competition_id"]

    # Auth model:
    # - In dev (LORA_WEBHOOK_SECRET unset or "CHANGE_LATER") the endpoint is
    #   open. Production startup refuses to boot with the default value, so
    #   this branch only ever runs in dev/test.
    # - With a real secret set, callers must either present X-Webhook-Secret
    #   or be a logged-in admin/judge of the target competition. Authenticated
    #   viewers and non-members are NOT trusted to skip the secret.
    expected_secret = current_app.config.get("LORA_WEBHOOK_SECRET")
    secret_configured = bool(expected_secret) and expected_secret != "CHANGE_LATER"
    if secret_configured:
        provided_secret = request.headers.get("X-Webhook-Secret", "")
        secret_ok = hmac.compare_digest(provided_secret, expected_secret)
        if not secret_ok and not _ingest_member_can_bypass_secret(current_user, competition_id):
            return {
                "ok": False,
                "error": "forbidden",
                "detail": "Invalid webhook secret.",
            }, 403
    dev_id = args.get("dev_id")
    checkpoint_id = args.get("checkpoint_id")
    payload = args.get("payload")
    rssi = args.get("rssi")
    snr = args.get("snr")
    ts_unix = args.get("ts")
    gps_lat = args.get("gps_lat")
    gps_lon = args.get("gps_lon")
    gps_alt = args.get("gps_alt")
    gps_age = args.get("gps_age_ms")
    ingest_password = args.get("ingest_password") or args.get("password")

    received_at = utc_from_timestamp_naive(ts_unix) if ts_unix else utcnow_naive()

    competition = db.session.get(Competition, competition_id)
    if not competition:
        return {
            "ok": False,
            "error": "not_found",
            "detail": "Competition not found.",
        }, 404

    # The competition-level ingest password gates non-webhook callers.
    # An authenticated user may skip it only if they are an active
    # admin/judge of *this* competition — same rule as the webhook secret
    # bypass above. Previously any authenticated user (including a viewer
    # from another competition) could bypass.
    if competition.ingest_password_hash and not (
        _ingest_member_can_bypass_secret(current_user, competition_id)
        or competition.check_ingest_password(ingest_password)
    ):
        return {
            "ok": False,
            "error": "forbidden",
            "detail": "Ingest password required.",
        }, 403

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

    # Dedup: if the same (competition, dev_id, payload) arrived within 10 s,
    # return the existing message instead of creating a duplicate.  This
    # prevents double-writes when both serial bridge and WiFi forward the
    # same LoRa packet.
    dev_id_str = str(dev_id) if dev_id is not None else f"checkpoint:{checkpoint_id}"
    cutoff = received_at - timedelta(seconds=10)
    dup = LoRaMessage.query.filter(
        LoRaMessage.competition_id == competition_id,
        LoRaMessage.dev_id == dev_id_str,
        LoRaMessage.payload == str(payload),
        LoRaMessage.received_at >= cutoff,
    ).first()
    if dup:
        return {"ok": True, "message_id": dup.id, "duplicate": True}, 200

    try:
        # 1) Store raw message
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
        device = None
        if dev_id is not None:
            device = LoRaDevice.query.filter_by(competition_id=competition_id, dev_num=int(dev_id)).first()
            if device:
                device.last_seen = received_at
                if rssi is not None:
                    device.last_rssi = float(rssi)
            cp, device, created_device, created_checkpoint = resolve_checkpoint_for_dev(competition_id, int(dev_id))
            device.last_seen = received_at
            if rssi is not None:
                device.last_rssi = float(rssi)
            if created_device:
                record_audit_event(
                    competition_id=competition_id,
                    event_type="device_created",
                    entity_type="device",
                    entity_id=device.id,
                    actor_type="device",
                    actor_device=device,
                    summary=f"Device {format_device_label(device)} auto-created from ingest.",
                    details={"id": device.id, "dev_num": device.dev_num, "name": device.name, "source": "ingest"},
                    created_at=received_at,
                )
            if created_checkpoint:
                record_audit_event(
                    competition_id=competition_id,
                    event_type="checkpoint_created",
                    entity_type="checkpoint",
                    entity_id=cp.id,
                    actor_type="device",
                    actor_device=device,
                    summary=f"Checkpoint {cp.name} auto-created from ingest.",
                    details={"id": cp.id, "name": cp.name, "lora_device_id": cp.lora_device_id, "source": "ingest"},
                    created_at=received_at,
                )
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

        # 3) Auto check-in if payload matches RFID UID. Scoped per
        # competition so the same physical card can be reused across
        # events without colliding.
        uid = str(payload).strip().upper()
        card = RFIDCard.query.filter_by(competition_id=competition_id, uid=uid).first()

        created_checkin = False
        team_name = None
        checkpoint_name = cp.name if cp else None
        team_obj = None

        if card:
            team = db.session.get(Team, card.team_id)
            if (
                team
                and cp
                and team.competition_id == competition_id
                and cp.competition_id == competition_id
                and not cp.is_virtual
            ):
                team_obj = team
                team_name = team.name
                arrived_at = received_at
                exists = Checkin.query.filter_by(
                    team_id=team.id,
                    checkpoint_id=cp.id,
                    competition_id=competition_id,
                ).first()
                if exists:
                    arrived_at = exists.timestamp or received_at
                else:
                    # Use a SAVEPOINT around just the Checkin insert so a
                    # concurrent ingest racing on the same (team, checkpoint)
                    # hits the uq_team_checkpoint constraint cleanly: we roll
                    # back just the savepoint, re-read the existing row, and
                    # continue treating this packet as a duplicate (200).
                    # The audit event lives outside the try/except so an
                    # unrelated IntegrityError (from the audit insert or
                    # anywhere else) is not misclassified as a duplicate.
                    created = Checkin(
                        team_id=team.id,
                        checkpoint_id=cp.id,
                        competition_id=competition_id,
                        timestamp=received_at,
                        created_by_device_id=device.id if device else None,
                    )
                    savepoint_ok = True
                    try:
                        with db.session.begin_nested():
                            db.session.add(created)
                    except IntegrityError:
                        savepoint_ok = False
                        existing = Checkin.query.filter_by(
                            team_id=team.id,
                            checkpoint_id=cp.id,
                            competition_id=competition_id,
                        ).first()
                        if existing:
                            arrived_at = existing.timestamp or received_at
                    if savepoint_ok:
                        record_audit_event(
                            competition_id=competition_id,
                            event_type="checkin_created",
                            entity_type="checkin",
                            entity_id=created.id,
                            actor_type="device" if device else "system",
                            actor_device=device,
                            summary=f"Check-in recorded for team {team.name} at {cp.name}.",
                            details={
                                "id": created.id,
                                "team_id": team.id,
                                "team_name": team.name,
                                "checkpoint_id": cp.id,
                                "checkpoint_name": cp.name,
                                "timestamp": received_at.isoformat(),
                                "source": "ingest",
                            },
                            created_at=received_at,
                        )
                        created_checkin = True

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
