# app/resources/rfid.py
from __future__ import annotations

import csv
import io
from typing import Optional

from flask import request
from flask_restful import Resource
from sqlalchemy.orm import joinedload
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import RFIDCard, Team
from app.utils.rest_auth import json_login_required, json_roles_required
from app.utils.serial_helpers import normalize_uid, read_uid_once
from config import Config

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
    uid = normalize_uid(uid_raw)
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
        cards = (
            RFIDCard.query
            .options(joinedload(RFIDCard.team))
            .order_by(RFIDCard.number.asc().nulls_last(), RFIDCard.uid.asc())
            .all()
        )
        return {"cards": [_serialize_card(c) for c in cards]}, 200

    @json_roles_required("judge", "admin")
    def post(self):
        payload = request.get_json(silent=True) or {}
        uid, team_id, number, error = _parse_card_payload(payload)
        if error:
            return {"error": "validation_error", "detail": error}, 400

        team = Team.query.get(team_id) if team_id is not None else None
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
        card = (
            RFIDCard.query
            .options(joinedload(RFIDCard.team))
            .get(card_id)
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
        card = RFIDCard.query.get(card_id)
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

        if team_id is not None and not Team.query.get(team_id):
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
        card = RFIDCard.query.get(card_id)
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
                team = Team.query.filter(Team.name.ilike(team_name)).first()
                if team:
                    team_id = team.id
            if team_id and not Team.query.get(team_id):
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
