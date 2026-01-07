# app/resources/rfid.py
from __future__ import annotations

import csv
import io
from typing import Optional

from flask import request
from flask_restful import Resource
from flask import current_app
from sqlalchemy.orm import joinedload
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import RFIDCard, Team, LoRaDevice, Checkpoint, Checkin
from app.utils.rest_auth import json_login_required, json_roles_required
from app.utils.competition import require_current_competition_id
from app.utils.serial_helpers import normalize_uid, read_uid_once
from config import Config
from app.utils.card_tokens import match_digests

BAUD = Config.SERIAL_BAUDRATE
HINT = Config.SERIAL_HINT
TIMEOUT = Config.SERIAL_TIMEOUT


def _serialize_card(card: RFIDCard) -> dict:
    return {
        "id": card.id,
        "uid": card.uid,
        "number": card.number,
        "team": {
            "id": card.team.id if card.team else None,
            "name": card.team.name if card.team else None,
            "number": card.team.number if (card.team and card.team.number is not None) else None,
        },
    }


def _parse_card_payload(payload: dict, require_team: bool = True) -> tuple[Optional[str], Optional[int], Optional[int], Optional[str]]:
    uid_raw = (payload.get("uid") or "").strip()
    uid = uid_raw
    if not uid:
        return None, None, None, "uid is required"

    team_id = payload.get("team_id")
    if team_id is None and require_team:
        return uid, None, None, "team_id is required"
    if team_id is not None:
        try:
            team_id = int(team_id)
        except Exception:
            return uid, None, None, "team_id must be integer"

    number = payload.get("number")
    if number in ("", None):
        number = None
    else:
        try:
            number = int(number)
            if number <= 0:
                return uid, team_id, None, "number must be positive"
        except Exception:
            return uid, team_id, None, "number must be integer"

    return uid, team_id, number, None


class RFIDCardListResource(Resource):
    method_decorators = [json_login_required]

    def get(self):
        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400
        cards = (
            RFIDCard.query
            .join(Team, RFIDCard.team_id == Team.id)
            .filter(Team.competition_id == comp_id)
            .options(joinedload(RFIDCard.team))
            .order_by(RFIDCard.number.asc().nulls_last(), RFIDCard.uid.asc())
            .all()
        )
        return {"cards": [_serialize_card(c) for c in cards]}, 200

    @json_roles_required("judge", "admin")
    def post(self):
        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400
        payload = request.get_json(silent=True) or {}
        uid, team_id, number, error = _parse_card_payload(payload)
        if error:
            return {"error": "validation_error", "detail": error}, 400

        team = (
            Team.query
            .filter(Team.competition_id == comp_id, Team.id == team_id)
            .first()
            if team_id is not None
            else None
        )
        if not team and team_id is not None:
            return {"error": "validation_error", "detail": "Invalid team_id"}, 400

        if team and RFIDCard.query.filter_by(team_id=team.id).first():
            return {
                "error": "conflict",
                "detail": "This team already has an RFID card assigned.",
            }, 409

        card = RFIDCard(uid=uid, team_id=team_id, number=number)
        db.session.add(card)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return {"error": "conflict", "detail": "UID already exists."}, 409

        return {"ok": True, "card": _serialize_card(card)}, 201


class RFIDCardItemResource(Resource):
    method_decorators = [json_login_required]

    def get(self, card_id: int):
        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400
        card = (
            RFIDCard.query
            .join(Team, RFIDCard.team_id == Team.id)
            .filter(Team.competition_id == comp_id, RFIDCard.id == card_id)
            .options(joinedload(RFIDCard.team))
            .first()
        )
        if not card:
            return {"error": "not_found"}, 404
        return _serialize_card(card), 200

    @json_roles_required("judge", "admin")
    def patch(self, card_id: int):
        return self._update(card_id, partial=True)

    @json_roles_required("judge", "admin")
    def put(self, card_id: int):
        return self._update(card_id, partial=False)

    def _update(self, card_id: int, partial: bool):
        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400
        card = (
            RFIDCard.query
            .join(Team, RFIDCard.team_id == Team.id)
            .filter(Team.competition_id == comp_id, RFIDCard.id == card_id)
            .first()
        )
        if not card:
            return {"error": "not_found"}, 404

        payload = request.get_json(silent=True) or {}
        uid = payload.get("uid") if "uid" in payload or not partial else card.uid
        team_id = payload.get("team_id") if "team_id" in payload or not partial else card.team_id
        number = payload.get("number") if "number" in payload or not partial else card.number

        uid, team_id, number, error = _parse_card_payload(
            {"uid": uid, "team_id": team_id, "number": number},
            require_team=not partial,
        )
        if error:
            return {"error": "validation_error", "detail": error}, 400

        if team_id is not None and not Team.query.filter(
            Team.competition_id == comp_id, Team.id == team_id
        ).first():
            return {"error": "validation_error", "detail": "Invalid team_id"}, 400

        if team_id is not None:
            exists = (
                RFIDCard.query
                .filter(RFIDCard.team_id == team_id, RFIDCard.id != card.id)
                .first()
            )
            if exists:
                return {
                    "error": "conflict",
                    "detail": "That team already has an RFID card assigned.",
                }, 409

        card.uid = uid
        card.team_id = team_id
        card.number = number

        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return {"error": "conflict", "detail": "UID already exists."}, 409

        return {"ok": True, "card": _serialize_card(card)}, 200

    @json_roles_required("admin")
    def delete(self, card_id: int):
        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400
        card = (
            RFIDCard.query
            .join(Team, RFIDCard.team_id == Team.id)
            .filter(Team.competition_id == comp_id, RFIDCard.id == card_id)
            .first()
        )
        if not card:
            return {"error": "not_found"}, 404
        db.session.delete(card)
        db.session.commit()
        return {"ok": True}, 200


class RFIDScanResource(Resource):
    method_decorators = [json_roles_required("judge", "admin")]

    def post(self):
        uid = read_uid_once(BAUD, HINT, TIMEOUT)
        if not uid:
            return {
                "ok": False,
                "error": "No UID read (check device, cable, or increase timeout).",
            }, 200
        return {"ok": True, "uid": uid}, 200


class RFIDBulkImportResource(Resource):
    method_decorators = [json_roles_required("admin")]

    def post(self):
        """
        Accepts multipart/form-data with 'file' (CSV) or JSON body with 'rows'.
        CSV columns: uid, team_id (or team_name), number (optional)
        """
        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400
        rows = []
        if request.files.get("file"):
            file = request.files["file"]
            try:
                stream = io.StringIO(file.stream.read().decode("utf-8", errors="ignore"))
                reader = csv.DictReader(stream)
                rows = list(reader)
            except Exception:
                return {"error": "validation_error", "detail": "Invalid CSV upload."}, 400
        else:
            payload = request.get_json(silent=True) or {}
            rows = payload.get("rows") or []
            if not isinstance(rows, list):
                return {"error": "validation_error", "detail": "rows must be a list."}, 400

        created = updated = skipped = 0
        errors = []

        for idx, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                skipped += 1
                errors.append({"row": idx, "detail": "Row is not an object"})
                continue

            uid_raw = (row.get("uid") or "").strip()
            uid = normalize_uid(uid_raw)
            if not uid:
                skipped += 1
                errors.append({"row": idx, "detail": "Missing uid"})
                continue

            team_id = None
            team_name = (row.get("team_name") or "").strip()
            team_id_val = (row.get("team_id") or "").strip()
            if team_id_val:
                try:
                    team_id = int(team_id_val)
                except Exception:
                    errors.append({"row": idx, "detail": "Invalid team_id"})
                    skipped += 1
                    continue
            elif team_name:
                team = (
                    Team.query
                    .filter(Team.competition_id == comp_id, Team.name.ilike(team_name))
                    .first()
                )
                if team:
                    team_id = team.id
            if team_id and not Team.query.filter(
                Team.competition_id == comp_id, Team.id == team_id
            ).first():
                skipped += 1
                errors.append({"row": idx, "detail": "Unknown team"})
                continue

            number_val = (row.get("number") or "").strip()
            number = None
            if number_val:
                try:
                    number = int(number_val)
                    if number <= 0:
                        raise ValueError
                except Exception:
                    skipped += 1
                    errors.append({"row": idx, "detail": "Invalid number"})
                    continue

            card = RFIDCard.query.filter_by(uid=uid).first()
            is_new = False
            if not card:
                card = RFIDCard(uid=uid)
                db.session.add(card)
                is_new = True

            if team_id:
                conflict = (
                    RFIDCard.query
                    .filter(RFIDCard.team_id == team_id, RFIDCard.id != card.id)
                    .first()
                )
                if conflict:
                    skipped += 1
                    errors.append({"row": idx, "detail": "Team already has a card"})
                    db.session.rollback()
                    continue
                card.team_id = team_id
            card.number = number

            try:
                db.session.flush()
            except IntegrityError:
                db.session.rollback()
                skipped += 1
                errors.append({"row": idx, "detail": "UID already exists"})
                continue

            if is_new:
                created += 1
            else:
                updated += 1

        db.session.commit()

        return {
            "ok": True,
            "summary": {
                "created": created,
                "updated": updated,
                "skipped": skipped,
            },
            "errors": errors,
        }, 200


class RFIDVerifyResource(Resource):
    method_decorators = [json_roles_required("judge", "admin")]

    def post(self):
        payload = request.get_json(silent=True) or {}
        uid = (payload.get("uid") or "").strip()
        digests = payload.get("digests") or []
        device_ids = payload.get("device_ids")
        checkpoint_ids = payload.get("checkpoint_ids")

        if not uid:
            return {"error": "validation_error", "detail": "uid is required"}, 400
        if not isinstance(digests, list):
            return {"error": "validation_error", "detail": "digests must be a list"}, 400

        comp_id = require_current_competition_id()
        if not comp_id:
            return {"error": "no_competition"}, 400
        team = (
            db.session.query(Team)
            .join(RFIDCard, RFIDCard.team_id == Team.id)
            .filter(Team.competition_id == comp_id, RFIDCard.uid == uid)
            .first()
        )

        # When no explicit filter is provided, scope to checkpoints
        # assigned to the team's active group(s) if available.
        allowed_checkpoint_ids: set[int] = set()
        if team:
            for tg in (team.group_assignments or []):
                if tg.active and tg.group:
                    for link in tg.group.checkpoint_links:
                        if link.checkpoint_id:
                            allowed_checkpoint_ids.add(link.checkpoint_id)

        if device_ids is not None:
            try:
                device_ids = [int(x) for x in device_ids if str(x).strip() != ""]
            except Exception:
                return {"error": "validation_error", "detail": "device_ids must be integers"}, 400
            if not device_ids:
                device_ids = None

        if checkpoint_ids is not None:
            try:
                checkpoint_ids = [int(x) for x in checkpoint_ids if str(x).strip() != ""]
            except Exception:
                return {"error": "validation_error", "detail": "checkpoint_ids must be integers"}, 400
            if not checkpoint_ids:
                checkpoint_ids = None

        checkpoint_lookup: dict[int, Checkpoint] = {}
        device_lookup: dict[int, LoRaDevice] = {}

        if checkpoint_ids is not None:
            checkpoints = (
                Checkpoint.query
                .filter(Checkpoint.competition_id == comp_id, Checkpoint.id.in_(checkpoint_ids))
                .all()
            )
            checkpoint_lookup = {c.id: c for c in checkpoints}
            for cp in checkpoints:
                if cp.lora_device and cp.lora_device.dev_num is not None:
                    device_lookup[cp.lora_device.dev_num] = cp.lora_device
        elif device_ids is not None:
            device_lookup = {
                d.dev_num: d
                for d in LoRaDevice.query
                .filter(LoRaDevice.competition_id == comp_id, LoRaDevice.dev_num.in_(device_ids))
                .all()
                if d.dev_num is not None
            }
            device_ids_for_cp = [d.id for d in device_lookup.values()]
            checkpoint_lookup = {
                c.id: c
                for c in Checkpoint.query
                .filter(Checkpoint.competition_id == comp_id, Checkpoint.lora_device_id.in_(device_ids_for_cp))
                .all()
            }
        elif allowed_checkpoint_ids:
            checkpoints = (
                Checkpoint.query
                .filter(Checkpoint.competition_id == comp_id, Checkpoint.id.in_(allowed_checkpoint_ids))
                .all()
            )
            checkpoint_lookup = {c.id: c for c in checkpoints}
            for cp in checkpoints:
                if cp.lora_device and cp.lora_device.dev_num is not None:
                    device_lookup[cp.lora_device.dev_num] = cp.lora_device
        else:
            checkpoints = (
                Checkpoint.query
                .filter(Checkpoint.competition_id == comp_id)
                .all()
            )
            checkpoint_lookup = {c.id: c for c in checkpoints}
            device_lookup = {
                d.dev_num: d
                for d in LoRaDevice.query
                .filter(LoRaDevice.competition_id == comp_id)
                .all()
                if d.dev_num is not None
            }

        if not checkpoint_lookup and not device_lookup:
            return {"error": "not_found", "detail": "No checkpoints found for verification"}, 404

        candidate_ids = set(checkpoint_lookup.keys()) | set(device_lookup.keys())
        match_rows = match_digests(uid, digests, candidate_ids)
        team_checkpoint_ids = set()
        if team:
            team_checkpoint_ids = {
                c.checkpoint_id
                for c in Checkin.query.filter_by(team_id=team.id, competition_id=comp_id).all()
                if c.checkpoint_id is not None
            }

        results = []
        has_mismatch = False
        for row in match_rows:
            digest = row["digest"]
            matches = row["matches"]
            entries = []
            for dev_num in matches:
                d = device_lookup.get(dev_num)
                cp = None
                if d and d.checkpoint_id:
                    cp = checkpoint_lookup.get(d.checkpoint_id)
                if not cp:
                    cp = checkpoint_lookup.get(dev_num)
                cp_name = cp.name if cp else None
                cp_id = cp.id if cp else None
                checked_in = (cp_id in team_checkpoint_ids) if cp_id else False
                if matches and cp_id and not checked_in:
                    has_mismatch = True
                entries.append({
                    "device_id": d.dev_num if d else None,
                    "device_name": d.name if d else None,
                    "checkpoint": cp_name,
                    "checkpoint_id": cp_id,
                    "checked_in": checked_in,
                })
            results.append({
                "digest": digest,
                "matches": entries,
                "collision": len(entries) > 1,
            })

        unknown = [r["digest"] for r in results if not r["matches"]]

        return {
            "ok": True,
            "uid": uid,
            "checkpoint_ids": list(checkpoint_lookup.keys()),
            "device_ids": list(device_lookup.keys()),
            "results": results,
            "unknown": unknown,
            "team": {
                "id": team.id,
                "name": team.name,
            } if team else None,
            "has_mismatch": has_mismatch,
        }, 200
