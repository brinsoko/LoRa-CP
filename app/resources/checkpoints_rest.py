# app/resources/checkpoints_rest.py
from __future__ import annotations

from typing import Iterable, List, Optional

from flask import request
from flask_restful import Resource
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import Checkpoint, CheckpointGroup, CheckpointGroupLink, LoRaDevice
from app.utils.rest_auth import json_login_required, json_roles_required


def _serialize_checkpoint(cp: Checkpoint) -> dict:
    return {
        "id": cp.id,
        "name": cp.name,
        "location": cp.location,
        "description": cp.description,
        "easting": cp.easting,
        "northing": cp.northing,
        "groups": [
            {
                "id": link.group_id,
                "name": link.group.name if link.group else None,
                "position": link.position,
            }
            for link in sorted(
                cp.group_links,
                key=lambda link: (link.group.name if link.group else "", link.position),
            )
        ],
        "lora_device": {
            "id": cp.lora_device.id,
            "dev_num": cp.lora_device.dev_num,
            "name": cp.lora_device.name,
        } if cp.lora_device else None,
    }


def _parse_group_ids(values: Iterable) -> List[int]:
    ids: List[int] = []
    for v in values or []:
        try:
            n = int(v)
            if n > 0:
                ids.append(n)
        except Exception:
            continue
    return ids


def _apply_groups(cp: Checkpoint, group_ids: List[int]) -> None:
    if group_ids is None:
        return

    existing = {link.group_id: link for link in cp.group_links}
    new_links: List[CheckpointGroupLink] = []

    if not group_ids:
        for link in existing.values():
            db.session.delete(link)
        cp.group_links = []
        return

    groups = (
        db.session.query(CheckpointGroup)
        .options(joinedload(CheckpointGroup.checkpoint_links))
        .filter(CheckpointGroup.id.in_(group_ids))
        .all()
    )
    group_lookup = {g.id: g for g in groups}

    for gid in group_ids:
        group = group_lookup.get(gid)
        if not group:
            continue
        link = existing.pop(gid, None)
        if link is None:
            next_position = (
                max((l.position for l in group.checkpoint_links), default=-1) + 1
            )
            link = CheckpointGroupLink(group=group, checkpoint=cp, position=next_position)
        new_links.append(link)

    for leftover in existing.values():
        db.session.delete(leftover)

    cp.group_links = new_links


def _assign_lora_device(cp: Checkpoint, device_id: Optional[int]) -> Optional[str]:
    if not device_id:
        cp.lora_device = None
        return None

    device = LoRaDevice.query.get(device_id)
    if not device:
        return "Invalid device."

    existing = Checkpoint.query.filter(
        Checkpoint.lora_device_id == device.id,
        Checkpoint.id != cp.id,
    ).first()
    if existing:
        return f"Device already attached to checkpoint '{existing.name}'."

    cp.lora_device = device
    return None


class CheckpointListResource(Resource):
    method_decorators = [json_login_required]

    def get(self):
        cps = (
            Checkpoint.query
            .options(
                joinedload(Checkpoint.group_links).joinedload(CheckpointGroupLink.group),
                joinedload(Checkpoint.lora_device),
            )
            .order_by(Checkpoint.name.asc())
            .all()
        )
        return {"checkpoints": [_serialize_checkpoint(cp) for cp in cps]}, 200

    @json_roles_required("judge", "admin")
    def post(self):
        payload = request.get_json(silent=True) or {}
        name = (payload.get("name") or "").strip()
        if not name:
            return {"error": "validation_error", "detail": "name is required"}, 400
        if Checkpoint.query.filter_by(name=name).first():
            return {"error": "duplicate", "detail": "Checkpoint name already exists."}, 409

        cp = Checkpoint(
            name=name,
            location=(payload.get("location") or "").strip() or None,
            description=(payload.get("description") or "").strip() or None,
            easting=payload.get("easting"),
            northing=payload.get("northing"),
        )
        db.session.add(cp)
        db.session.flush()

        group_ids = _parse_group_ids(payload.get("group_ids"))
        if group_ids:
            _apply_groups(cp, group_ids)

        lora_device_id = payload.get("lora_device_id")
        if lora_device_id is not None:
            try:
                lora_device_id = int(lora_device_id)
            except Exception:
                return {"error": "validation_error", "detail": "lora_device_id must be integer"}, 400
            error = _assign_lora_device(cp, lora_device_id)
            if error:
                db.session.rollback()
                return {"error": "validation_error", "detail": error}, 400

        db.session.commit()
        return {"ok": True, "checkpoint": _serialize_checkpoint(cp)}, 201


class CheckpointItemResource(Resource):
    method_decorators = [json_login_required]

    def get(self, checkpoint_id: int):
        cp = (
            Checkpoint.query
            .options(
                joinedload(Checkpoint.group_links).joinedload(CheckpointGroupLink.group),
                joinedload(Checkpoint.lora_device),
            )
            .get(checkpoint_id)
        )
        if not cp:
            return {"error": "not_found"}, 404
        return _serialize_checkpoint(cp), 200

    @json_roles_required("judge", "admin")
    def patch(self, checkpoint_id: int):
        return self._update(checkpoint_id, partial=True)

    @json_roles_required("judge", "admin")
    def put(self, checkpoint_id: int):
        return self._update(checkpoint_id, partial=False)

    def _update(self, checkpoint_id: int, partial: bool):
        cp = (
            Checkpoint.query
            .options(joinedload(Checkpoint.group_links))
            .get(checkpoint_id)
        )
        if not cp:
            return {"error": "not_found"}, 404

        payload = request.get_json(silent=True) or {}

        if not partial or "name" in payload:
            name = (payload.get("name") or "").strip()
            if not name:
                return {"error": "validation_error", "detail": "name is required"}, 400
            existing = (
                Checkpoint.query
                .filter(Checkpoint.name == name, Checkpoint.id != cp.id)
                .first()
            )
            if existing:
                return {"error": "duplicate", "detail": "Checkpoint name already exists."}, 409
            cp.name = name

        if "location" in payload or not partial:
            cp.location = (payload.get("location") or "").strip() or None

        if "description" in payload or not partial:
            cp.description = (payload.get("description") or "").strip() or None

        if "easting" in payload or not partial:
            cp.easting = payload.get("easting")

        if "northing" in payload or not partial:
            cp.northing = payload.get("northing")

        if "group_ids" in payload:
            group_ids = _parse_group_ids(payload.get("group_ids"))
            _apply_groups(cp, group_ids)

        if "lora_device_id" in payload or (not partial and "lora_device_id" not in payload):
            raw_device_id = payload.get("lora_device_id", None)
            if raw_device_id in (None, "", "null"):
                error = _assign_lora_device(cp, None)
            else:
                try:
                    raw_device_id = int(raw_device_id)
                except Exception:
                    return {"error": "validation_error", "detail": "lora_device_id must be integer"}, 400
                error = _assign_lora_device(cp, raw_device_id)
            if error:
                db.session.rollback()
                return {"error": "validation_error", "detail": error}, 400

        db.session.commit()
        return {"ok": True, "checkpoint": _serialize_checkpoint(cp)}, 200

    @json_roles_required("admin")
    def delete(self, checkpoint_id: int):
        cp = Checkpoint.query.get(checkpoint_id)
        if not cp:
            return {"error": "not_found"}, 404
        if cp.checkins:
            return {
                "error": "conflict",
                "detail": "Cannot delete checkpoint with existing check-ins.",
            }, 409
        db.session.delete(cp)
        db.session.commit()
        return {"ok": True}, 200


class CheckpointImportResource(Resource):
    method_decorators = [json_roles_required("judge", "admin")]

    def post(self):
        """
        Bulk import/update checkpoints.
        Payload: { "items": [ {name, easting?, northing?, location?, description?, group_ids?, lora_device_id?, action?} ] }
        action: "create"|"update"|"upsert" (default upsert)
        """
        payload = request.get_json(silent=True) or {}
        items = payload.get("items") or []
        if not isinstance(items, list):
            return {"error": "validation_error", "detail": "items must be an array"}, 400

        created = updated = skipped = 0
        errors: List[dict] = []

        for idx, item in enumerate(items):
            if not isinstance(item, dict):
                skipped += 1
                errors.append({"index": idx, "detail": "Item is not an object"})
                continue

            name = (item.get("name") or "").strip()
            if not name:
                skipped += 1
                errors.append({"index": idx, "detail": "name is required"})
                continue

            action = (item.get("action") or "upsert").lower()
            cp = Checkpoint.query.filter(Checkpoint.name == name).first()

            if action == "create" and cp:
                skipped += 1
                errors.append({"index": idx, "detail": "checkpoint already exists"})
                continue
            if action == "update" and not cp:
                skipped += 1
                errors.append({"index": idx, "detail": "checkpoint not found"})
                continue

            is_new = False
            if not cp:
                cp = Checkpoint(name=name)
                db.session.add(cp)
                db.session.flush()
                is_new = True

            cp.location = (item.get("location") or "").strip() or cp.location
            cp.description = (item.get("description") or "").strip() or cp.description

            if "easting" in item and item["easting"] not in (None, ""):
                try:
                    cp.easting = float(item["easting"])
                except Exception:
                    pass
            if "northing" in item and item["northing"] not in (None, ""):
                try:
                    cp.northing = float(item["northing"])
                except Exception:
                    pass

            if "group_ids" in item:
                group_ids = _parse_group_ids(item.get("group_ids"))
                _apply_groups(cp, group_ids)

            if "lora_device_id" in item:
                raw_device_id = item.get("lora_device_id")
                if raw_device_id in (None, "", "null"):
                    _assign_lora_device(cp, None)
                else:
                    try:
                        raw_device_id = int(raw_device_id)
                    except Exception:
                        errors.append({"index": idx, "detail": "invalid lora_device_id"})
                        continue
                    err = _assign_lora_device(cp, raw_device_id)
                    if err:
                        errors.append({"index": idx, "detail": err})
                        continue

            created += 1 if is_new else 0
            updated += 0 if is_new else 1

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
