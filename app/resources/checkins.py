# app/resources/checkins.py
from __future__ import annotations

from datetime import datetime, timedelta
import io, csv
from typing import Optional, Tuple

from flask import request, make_response
from flask_restful import Resource
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import Checkin, Team, Checkpoint
from app.utils.time import from_datetime_local

from app.utils.rest_auth import json_roles_required
from app.utils.sheets_sync import mark_arrival_checkbox


# -------- helpers --------
def _parse_date_range(date_from_str: Optional[str], date_to_str: Optional[str]) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Build an inclusive day range for YYYY-MM-DD inputs."""
    start = end = None
    try:
        if date_from_str:
            start = datetime.fromisoformat(date_from_str)
        if date_to_str:
            end = datetime.fromisoformat(date_to_str) + timedelta(days=1)  # exclusive
    except ValueError:
        pass
    return start, end


def _filtered_query(team_id: Optional[int], checkpoint_id: Optional[int],
                    date_from: Optional[str], date_to: Optional[str]):
    """Return a SQLAlchemy query over Checkin with eager-loaded relations and filters applied."""
    q = (Checkin.query
         .options(joinedload(Checkin.team), joinedload(Checkin.checkpoint)))

    if team_id:
        q = q.filter(Checkin.team_id == team_id)
    if checkpoint_id:
        q = q.filter(Checkin.checkpoint_id == checkpoint_id)

    start, end = _parse_date_range(date_from, date_to)
    if start:
        q = q.filter(Checkin.timestamp >= start)
    if end:
        q = q.filter(Checkin.timestamp < end)

    return q


def _parse_timestamp(payload: dict, fallback_dt: Optional[datetime] = None) -> datetime:
    """
    Accepts either:
      - timestamp: ISO-8601 UTC string      (e.g. "2025-10-17T02:36:00")
      - timestamp_local + timezone (IANA)   (e.g. "2025-10-17T04:36", "Europe/Ljubljana")
    Fallbacks to `fallback_dt` or utcnow().
    """
    default_dt = fallback_dt or datetime.utcnow()

    ts_str = (payload.get("timestamp") or payload.get("timestamp_local") or "").strip()
    tz_name = (payload.get("timezone") or payload.get("tz") or "").strip()
    if not ts_str:
        return default_dt

    if tz_name:
        try:
            dt = from_datetime_local(ts_str, tz_name)
            if dt:
                return dt
        except Exception:
            pass

    try:
        return datetime.fromisoformat(ts_str)
    except Exception:
        return default_dt


def _serialize_checkin(c: Checkin) -> dict:
    return {
        "id": c.id,
        "timestamp_utc": c.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
        "team": {
            "id": c.team.id if c.team else None,
            "name": c.team.name if c.team else None,
            "number": c.team.number if (c.team and c.team.number is not None) else None,
        },
        "checkpoint": {
            "id": c.checkpoint.id if c.checkpoint else None,
            "name": c.checkpoint.name if c.checkpoint else None,
        },
    }


# -------- resources --------
class CheckinListResource(Resource):
    """
    GET  /api/checkins
    POST /api/checkins
    """

    method_decorators = [json_roles_required("judge", "admin")]

    def get(self):
        # filters & sort
        team_id = request.args.get("team_id", type=int)
        checkpoint_id = request.args.get("checkpoint_id", type=int)
        date_from = request.args.get("date_from")
        date_to = request.args.get("date_to")
        sort = (request.args.get("sort") or "new").lower()

        q = _filtered_query(team_id, checkpoint_id, date_from, date_to)

        if sort == "old":
            q = q.order_by(Checkin.timestamp.asc())
        elif sort == "team":
            q = (q.join(Team, Checkin.team_id == Team.id)
                   .order_by(Team.name.asc(),
                             Team.number.asc().nulls_last(),
                             Checkin.timestamp.asc()))
        else:
            q = q.order_by(Checkin.timestamp.desc())

        rows = q.all()
        return {"checkins": [_serialize_checkin(r) for r in rows]}, 200

    def post(self):
        """
        Body (JSON or x-www-form-urlencoded):
          - team_id (int, required)
          - checkpoint_id (int, required)
          - timestamp | timestamp_local + timezone (optional)
          - override = "replace" (optional)
        """
        payload = request.get_json(silent=True) or request.form.to_dict()
        try:
            team_id = int(payload.get("team_id"))
            checkpoint_id = int(payload.get("checkpoint_id"))
        except Exception:
            return {"error": "invalid_request", "detail": "team_id and checkpoint_id are required integers."}, 400

        if not Team.query.get(team_id) or not Checkpoint.query.get(checkpoint_id):
            return {"error": "invalid_fk", "detail": "Invalid team or checkpoint."}, 400

        ts = _parse_timestamp(payload, datetime.utcnow())
        override = (payload.get("override") or "").strip().lower()

        existing = Checkin.query.filter_by(team_id=team_id, checkpoint_id=checkpoint_id).first()
        if existing:
            if override == "replace":
                existing.timestamp = ts
                db.session.commit()
                return {"ok": True, "replaced": True, "checkin": _serialize_checkin(existing)}, 200
            return {
                "error": "duplicate",
                "detail": "Check-in for this team and checkpoint already exists. Use override=replace to update its timestamp.",
                "checkin": _serialize_checkin(existing),
            }, 409

        c = Checkin(team_id=team_id, checkpoint_id=checkpoint_id, timestamp=ts)
        db.session.add(c)
        db.session.commit()
        try:
            mark_arrival_checkbox(team_id, checkpoint_id, ts)
        except Exception:
            pass
        return {"ok": True, "created": True, "checkin": _serialize_checkin(c)}, 201


class CheckinItemResource(Resource):
    """
    GET    /api/checkins/<id>
    PATCH  /api/checkins/<id>
    PUT    /api/checkins/<id>
    DELETE /api/checkins/<id>
    """

    def get(self, checkin_id: int):
        c = Checkin.query.options(joinedload(Checkin.team), joinedload(Checkin.checkpoint)).get(checkin_id)
        if not c:
            return {"error": "not_found"}, 404
        return _serialize_checkin(c), 200

    def _update(self, checkin_id: int, partial: bool):
        c = Checkin.query.get(checkin_id)
        if not c:
            return {"error": "not_found"}, 404

        payload = request.get_json(silent=True) or request.form.to_dict()

        new_team_id = payload.get("team_id", None if partial else c.team_id)
        new_cp_id = payload.get("checkpoint_id", None if partial else c.checkpoint_id)

        # normalize to ints if provided
        try:
            if new_team_id is not None:
                new_team_id = int(new_team_id)
            if new_cp_id is not None:
                new_cp_id = int(new_cp_id)
        except Exception:
            return {"error": "invalid_request", "detail": "team_id/checkpoint_id must be integers."}, 400

        # validate FKs if provided
        if new_team_id is not None and not Team.query.get(new_team_id):
            return {"error": "invalid_fk", "detail": "Invalid team."}, 400
        if new_cp_id is not None and not Checkpoint.query.get(new_cp_id):
            return {"error": "invalid_fk", "detail": "Invalid checkpoint."}, 400

        # timestamp
        new_ts = _parse_timestamp(payload, c.timestamp)

        # duplicate protection
        if new_team_id is None:
            new_team_id = c.team_id
        if new_cp_id is None:
            new_cp_id = c.checkpoint_id

        dup = (Checkin.query
               .filter(Checkin.team_id == new_team_id,
                       Checkin.checkpoint_id == new_cp_id,
                       Checkin.id != checkin_id)
               .first())

        override = (payload.get("override") or "").strip().lower()
        if dup and override != "replace":
            return {
                "error": "duplicate",
                "detail": "Another check-in exists for that team & checkpoint. Use override=replace to replace it.",
                "other_checkin": _serialize_checkin(dup),
            }, 409

        if dup and override == "replace":
            db.session.delete(dup)
            db.session.flush()

        c.team_id = new_team_id
        c.checkpoint_id = new_cp_id
        c.timestamp = new_ts
        db.session.commit()
        try:
            mark_arrival_checkbox(new_team_id, new_cp_id)
        except Exception:
            pass
        return {"ok": True, "updated": True, "checkin": _serialize_checkin(c)}, 200

    def put(self, checkin_id: int):
        return self._update(checkin_id, partial=False)

    def patch(self, checkin_id: int):
        return self._update(checkin_id, partial=True)

    def delete(self, checkin_id: int):
        c = Checkin.query.get(checkin_id)
        if not c:
            return {"error": "not_found"}, 404
        db.session.delete(c)
        db.session.commit()
        return {"ok": True, "deleted": True}, 200


class CheckinExportResource(Resource):
    """
    GET /api/checkins/export.csv
    Returns CSV with same filters/sort as list.
    """

    def get(self):
        team_id = request.args.get("team_id", type=int)
        checkpoint_id = request.args.get("checkpoint_id", type=int)
        date_from = request.args.get("date_from")
        date_to = request.args.get("date_to")
        sort = (request.args.get("sort") or "new").lower()

        q = _filtered_query(team_id, checkpoint_id, date_from, date_to)

        if sort == "old":
            q = q.order_by(Checkin.timestamp.asc())
        elif sort == "team":
            q = (q.join(Team, Checkin.team_id == Team.id)
                   .order_by(Team.name.asc(),
                             Team.number.asc().nulls_last(),
                             Checkin.timestamp.asc()))
        else:
            q = q.order_by(Checkin.timestamp.desc())

        rows = q.all()

        buf = io.StringIO()
        w = csv.writer(buf)
        w.writerow([
            "timestamp_utc", "team_id", "team_name", "team_number",
            "checkpoint_id", "checkpoint_name",
        ])
        for r in rows:
            w.writerow([
                r.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                r.team.id if r.team else "",
                r.team.name if r.team else "",
                r.team.number if (r.team and r.team.number is not None) else "",
                r.checkpoint.id if r.checkpoint else "",
                r.checkpoint.name if r.checkpoint else "",
            ])

        resp = make_response(buf.getvalue(), 200)
        resp.mimetype = "text/csv"
        resp.headers["Content-Disposition"] = "attachment; filename=checkins.csv"
        return resp
