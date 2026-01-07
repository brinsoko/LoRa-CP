# app/blueprints/checkins/routes.py
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from flask import Blueprint, render_template, request, redirect, url_for, flash, Response
from flask_login import current_user
from app.models import JudgeCheckpoint, Checkpoint
from app.utils.competition import get_current_competition_id, get_current_competition_role

from app.utils.frontend_api import api_json, api_request
from app.utils.perms import roles_required

checkins_bp = Blueprint("checkins", __name__, template_folder="../../templates")


DEFAULT_TIMEZONE = ZoneInfo("Europe/Ljubljana")


def _fetch_teams():
    resp, payload = api_json("GET", "/api/teams", params={"sort": "name_asc"})
    if resp.status_code != 200:
        flash("Could not load teams.", "warning")
        return []
    return payload.get("teams", [])


def _fetch_checkpoints():
    resp, payload = api_json("GET", "/api/checkpoints")
    if resp.status_code != 200:
        flash("Could not load checkpoints.", "warning")
        return []
    return payload.get("checkpoints", [])


def _fetch_assigned_checkpoints():
    comp_id = get_current_competition_id()
    if not comp_id:
        return []
    assigned = (
        JudgeCheckpoint.query
        .join(Checkpoint, JudgeCheckpoint.checkpoint_id == Checkpoint.id)
        .filter(
            JudgeCheckpoint.user_id == current_user.id,
            Checkpoint.competition_id == comp_id,
        )
        .order_by(Checkpoint.name.asc())
        .all()
    )
    return [jc.checkpoint for jc in assigned if jc.checkpoint]


def _fetch_checkpoints_for_user(include_checkpoint_id: int | None = None):
    role = get_current_competition_role()
    if role == "judge":
        checkpoints = _fetch_assigned_checkpoints()
        if include_checkpoint_id and all(cp.id != include_checkpoint_id for cp in checkpoints):
            extra = Checkpoint.query.filter(Checkpoint.id == include_checkpoint_id).first()
            if extra:
                checkpoints.append(extra)
        return checkpoints
    return _fetch_checkpoints()


def _parse_timestamp_from_form(fallback: datetime | None = None) -> datetime:
    ts_str = (request.form.get("timestamp") or request.form.get("timestamp_local") or "").strip()
    tz_name = (request.form.get("timezone") or request.form.get("tz") or "").strip()
    default_dt = fallback or datetime.utcnow()

    if not ts_str:
        return default_dt

    try:
        local_dt = datetime.fromisoformat(ts_str)
    except ValueError:
        return default_dt

    try:
        tz = ZoneInfo(tz_name) if tz_name else DEFAULT_TIMEZONE
    except Exception:
        tz = DEFAULT_TIMEZONE

    aware_local = local_dt.replace(tzinfo=tz)
    utc_dt = aware_local.astimezone(ZoneInfo("UTC"))
    return utc_dt.replace(tzinfo=None)


def _decorate_checkins(items):
    decorated = []
    for item in items:
        ts = item.get("timestamp_utc")
        dt = None
        if ts:
            try:
                dt = datetime.fromisoformat(ts)
            except Exception:
                dt = None
        decorated.append(
            {
                "id": item.get("id"),
                "timestamp": dt,
                "team": item.get("team") or {},
                "checkpoint": item.get("checkpoint") or {},
            }
        )
    return decorated


@checkins_bp.route("/", methods=["GET"])
def list_checkins():
    team_id = request.args.get("team_id", type=int)
    checkpoint_id = request.args.get("checkpoint_id", type=int)
    date_from = request.args.get("date_from") or ""
    date_to = request.args.get("date_to") or ""
    sort = (request.args.get("sort") or "new").lower()

    params = {"sort": sort}
    if team_id:
        params["team_id"] = team_id
    if checkpoint_id:
        params["checkpoint_id"] = checkpoint_id
    if date_from:
        params["date_from"] = date_from
    if date_to:
        params["date_to"] = date_to

    resp, payload = api_json("GET", "/api/checkins", params=params)
    if resp.status_code != 200:
        flash(payload.get("detail") or payload.get("error") or "Could not load check-ins.", "warning")
        checkins = []
    else:
        checkins = _decorate_checkins(payload.get("checkins", []))

    teams = _fetch_teams()
    checkpoints = _fetch_checkpoints_for_user(include_checkpoint_id=checkpoint_id)

    return render_template(
        "view_checkins.html",
        checkins=checkins,
        teams=teams,
        checkpoints=checkpoints,
        selected_team_id=team_id,
        selected_checkpoint_id=checkpoint_id,
        selected_date_from=date_from,
        selected_date_to=date_to,
        selected_sort=sort,
    )


@checkins_bp.route("/export.csv", methods=["GET"])
def export_checkins_csv():
    params = {
        key: value
        for key, value in {
            "team_id": request.args.get("team_id"),
            "checkpoint_id": request.args.get("checkpoint_id"),
            "date_from": request.args.get("date_from"),
            "date_to": request.args.get("date_to"),
            "sort": request.args.get("sort"),
        }.items()
        if value not in (None, "")
    }

    resp = api_request("GET", "/api/checkins/export.csv", params=params)
    output = resp.get_data()
    return Response(output, mimetype="text/csv", headers={"Content-Disposition": "attachment; filename=checkins.csv"})


@checkins_bp.route("/add", methods=["GET", "POST"])
@roles_required("judge", "admin")
def add_checkin():
    teams = _fetch_teams()
    checkpoints = _fetch_checkpoints_for_user()
    now = datetime.utcnow()
    default_checkpoint_id = None
    if get_current_competition_role() == "judge":
        default_row = (
            JudgeCheckpoint.query
            .filter(JudgeCheckpoint.user_id == current_user.id, JudgeCheckpoint.is_default.is_(True))
            .first()
        )
        default_checkpoint_id = default_row.checkpoint_id if default_row else None

    context = {
        "teams": teams,
        "checkpoints": checkpoints,
        "now": now,
        "dup_team_id": None,
        "dup_checkpoint_id": None,
        "selected_checkpoint_id": default_checkpoint_id,
        "timestamp_prefill": request.form.get("timestamp_local") if request.method == "POST" else now.astimezone(DEFAULT_TIMEZONE).strftime("%Y-%m-%dT%H:%M:%S"),
        "suggest_override": request.form.get("override") == "replace",
    }

    if request.method == "POST":
        team_id = request.form.get("team_id", type=int)
        checkpoint_id = request.form.get("checkpoint_id", type=int)
        override = request.form.get("override")

        payload = {
            "team_id": team_id,
            "checkpoint_id": checkpoint_id,
            "timestamp": _parse_timestamp_from_form(now).isoformat(),
        }
        if override == "replace":
            payload["override"] = "replace"

        resp, body = api_json("POST", "/api/checkins", json=payload)

        if resp.status_code in (200, 201):
            message = "Existing check-in replaced." if resp.status_code == 200 else "Check-in recorded."
            flash(message, "success")
            return redirect(url_for("checkins.list_checkins"))

        if resp.status_code == 409 and body.get("error") == "duplicate":
            flash("This team has already checked in at this checkpoint. Submit again to replace the timestamp.", "warning")
            context.update(
                {
                    "dup_team_id": team_id,
                    "dup_checkpoint_id": checkpoint_id,
                    "selected_checkpoint_id": checkpoint_id,
                    "suggest_override": True,
                }
            )
            return render_template("add_checkin.html", **context)

        flash(body.get("detail") or body.get("error") or "Could not record check-in.", "warning")

    return render_template("add_checkin.html", **context)


def _load_checkin(checkin_id: int):
    resp, payload = api_json("GET", f"/api/checkins/{checkin_id}")
    if resp.status_code != 200:
        return None, None
    decorated = _decorate_checkins([payload])[0]
    timestamp_local_value = ""
    if decorated["timestamp"]:
        timestamp_local_value = decorated["timestamp"].strftime("%Y-%m-%dT%H:%M:%S")
    team = payload.get("team") or {}
    checkpoint = payload.get("checkpoint") or {}
    decorated.update(
        {
            "team_id": team.get("id"),
            "checkpoint_id": checkpoint.get("id"),
            "timestamp_local": timestamp_local_value,
        }
    )
    return payload, decorated


@checkins_bp.route("/<int:checkin_id>/edit", methods=["GET", "POST"])
@roles_required("judge", "admin")
def edit_checkin(checkin_id: int):
    raw_checkin, decorated = _load_checkin(checkin_id)
    if not raw_checkin:
        flash("Check-in not found.", "warning")
        return redirect(url_for("checkins.list_checkins"))

    teams = _fetch_teams()
    checkpoints = _fetch_checkpoints()

    context = {
        "c": decorated,
        "teams": teams,
        "checkpoints": checkpoints,
        "timestamp_local": decorated.get("timestamp_local"),
        "suggest_override": request.form.get("override") == "replace",
        "pending_team_id": None,
        "pending_cp_id": None,
    }
    if request.method == "GET" and get_current_competition_role() == "judge":
        default_row = (
            JudgeCheckpoint.query
            .filter(JudgeCheckpoint.user_id == current_user.id, JudgeCheckpoint.is_default.is_(True))
            .first()
        )
        if default_row and not context["c"].get("checkpoint_id"):
            context["c"]["checkpoint_id"] = default_row.checkpoint_id

    if request.method == "POST":
        team_id = request.form.get("team_id", type=int)
        checkpoint_id = request.form.get("checkpoint_id", type=int)
        override = request.form.get("override")

        payload = {
            "team_id": team_id,
            "checkpoint_id": checkpoint_id,
            "timestamp": _parse_timestamp_from_form(decorated.get("timestamp") or datetime.utcnow()).isoformat(),
        }
        if override == "replace":
            payload["override"] = "replace"

        resp, body = api_json("PATCH", f"/api/checkins/{checkin_id}", json=payload)

        if resp.status_code == 200:
            flash("Check-in updated.", "success")
            return redirect(url_for("checkins.list_checkins"))

        if resp.status_code == 409 and body.get("error") == "duplicate":
            flash("Another check-in exists for that team and checkpoint. Submit again to replace it.", "warning")
            context["c"]["team_id"] = team_id
            context["c"]["checkpoint_id"] = checkpoint_id
            context["timestamp_local"] = request.form.get("timestamp_local") or context.get("timestamp_local")
            context.update(
                {
                    "suggest_override": True,
                    "pending_team_id": team_id,
                    "pending_cp_id": checkpoint_id,
                }
            )
            return render_template("checkin_edit.html", **context)

        flash(body.get("detail") or body.get("error") or "Could not update check-in.", "warning")
        decorated.update(
            {
                "team_id": team_id,
                "checkpoint_id": checkpoint_id,
                "timestamp_local": request.form.get("timestamp_local") or decorated.get("timestamp_local"),
            }
        )
        return render_template("checkin_edit.html", **context)

    return render_template("checkin_edit.html", **context)


@checkins_bp.route("/<int:checkin_id>/delete", methods=["POST"])
@roles_required("admin")
def delete_checkin(checkin_id: int):
    resp, body = api_json("DELETE", f"/api/checkins/{checkin_id}")

    if resp.status_code == 200:
        flash("Check-in deleted.", "success")
    else:
        flash(body.get("detail") or body.get("error") or "Could not delete check-in.", "warning")

    return redirect(url_for("checkins.list_checkins"))


@checkins_bp.route("/import_json", methods=["GET", "POST"])
@roles_required("judge", "admin")
def import_checkins_json():
    flash("JSON import via UI is disabled. Use the /api/checkins endpoint instead.", "warning")
    return redirect(url_for("checkins.list_checkins"))
