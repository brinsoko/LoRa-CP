# app/blueprints/checkins/routes.py
from __future__ import annotations

from datetime import datetime, timedelta
import io, csv

from flask import Blueprint, render_template, request, redirect, url_for, flash, Response
from sqlalchemy.orm import joinedload

from app.extensions import db
from app.models import Checkin, Team, Checkpoint
from app.utils.perms import roles_required
from app.utils.time import to_datetime_local, from_datetime_local


checkins_bp = Blueprint("checkins", __name__, template_folder="../../templates")




# --------------------------- helpers ---------------------------

def _parse_date_range(date_from_str: str | None, date_to_str: str | None):
    """Build an inclusive day range for YYYY-MM-DD inputs."""
    start = end = None
    try:
        if date_from_str:
            start = datetime.fromisoformat(date_from_str)
        if date_to_str:
            # end is exclusive: add 1 day so we can use '< end'
            end = datetime.fromisoformat(date_to_str) + timedelta(days=1)
    except ValueError:
        # leave as None if user typed a bad date
        pass
    return start, end


def _filtered_query(team_id: int | None, checkpoint_id: int | None,
                    date_from_str: str | None, date_to_str: str | None):
    """Return a SQLAlchemy query over Checkin with eager-loaded relations and filters applied."""
    q = (Checkin.query
         .options(joinedload(Checkin.team), joinedload(Checkin.checkpoint)))

    if team_id:
        q = q.filter(Checkin.team_id == team_id)
    if checkpoint_id:
        q = q.filter(Checkin.checkpoint_id == checkpoint_id)

    start, end = _parse_date_range(date_from_str, date_to_str)
    if start:
        q = q.filter(Checkin.timestamp >= start)
    if end:
        q = q.filter(Checkin.timestamp < end)

    return q.order_by(Checkin.timestamp.desc())



def _parse_timestamp_from_form(fallback_dt: datetime | None = None) -> datetime:
    """
    Parse <input name="timestamp"> or <input name="timestamp_local"> with an optional
    <select name="timezone">. Never returns None. If parsing fails or fields are blank,
    return fallback_dt, or utcnow() if fallback_dt is None.
    """
    ts_str = (request.form.get("timestamp") or
              request.form.get("timestamp_local") or "").strip()
    tz_name = (request.form.get("timezone") or
               request.form.get("tz") or "").strip()

    # Default result if anything goes wrong
    default_dt = fallback_dt or datetime.utcnow()

    if not ts_str:
        return default_dt

    # Try local->UTC first if a timezone was provided
    if tz_name:
        try:
            dt = from_datetime_local(ts_str, tz_name)
            if dt:                       # <- guard against None returns
                return dt
        except Exception:
            pass  # fall through to ISO parser / default

    # Try plain ISO (datetime-local produces e.g. 2025-10-17T02:36 or with seconds)
    try:
        return datetime.fromisoformat(ts_str)
    except Exception:
        return default_dt


# --------------------------- view & export ---------------------------

@checkins_bp.route("/", methods=["GET"])
def list_checkins():
    """
    Public view with filters.
    Query params: team_id, checkpoint_id, date_from (YYYY-MM-DD), date_to (YYYY-MM-DD), sort
    sort: 'new' (default), 'old', 'team'
    """
    teams = Team.query.order_by(Team.name.asc()).all()
    checkpoints = Checkpoint.query.order_by(Checkpoint.name.asc()).all()

    team_id = request.args.get("team_id", type=int)
    checkpoint_id = request.args.get("checkpoint_id", type=int)
    date_from = request.args.get("date_from")  # str | None
    date_to = request.args.get("date_to")      # str | None
    sort = (request.args.get("sort") or "new").lower()

    q = _filtered_query(team_id, checkpoint_id, date_from, date_to)

    # Sorting:
    # - new: timestamp desc
    # - old: timestamp asc
    # - team: Team.name asc, Team.number asc (NULLS LAST), then timestamp asc
    if sort == "old":
        q = q.order_by(Checkin.timestamp.asc())
    elif sort == "team":
        # join Team for ordering by team fields
        q = q.join(Team, Checkin.team_id == Team.id).order_by(
            Team.name.asc(),
            Team.number.asc().nulls_last(),
            Checkin.timestamp.asc(),
        )
    else:
        # default 'new'
        q = q.order_by(Checkin.timestamp.desc())

    checkins = q.all()

    return render_template(
        "view_checkins.html",
        checkins=checkins,
        teams=teams,
        checkpoints=checkpoints,
        selected_team_id=team_id,
        selected_checkpoint_id=checkpoint_id,
        selected_date_from=date_from or "",
        selected_date_to=date_to or "",
        selected_sort=sort,
    )


@checkins_bp.route("/export.csv", methods=["GET"])
def export_checkins_csv():
    """CSV export with the same filters and sort as the list view."""
    team_id = request.args.get("team_id", type=int)
    checkpoint_id = request.args.get("checkpoint_id", type=int)
    date_from = request.args.get("date_from")
    date_to = request.args.get("date_to")
    sort = (request.args.get("sort") or "new").lower()

    q = _filtered_query(team_id, checkpoint_id, date_from, date_to)

    if sort == "old":
        q = q.order_by(Checkin.timestamp.asc())
    elif sort == "team":
        q = q.join(Team, Checkin.team_id == Team.id).order_by(
            Team.name.asc(),
            Team.number.asc().nulls_last(),
            Checkin.timestamp.asc(),
        )
    else:
        q = q.order_by(Checkin.timestamp.desc())

    rows = q.all()

    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "timestamp_utc",
        "team_id",
        "team_name",
        "team_number",
        "checkpoint_id",
        "checkpoint_name",
    ])
    for r in rows:
        w.writerow([
            r.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            r.team.id if r.team else "",
            r.team.name if r.team else "",
            r.team.number if r.team and r.team.number is not None else "",
            r.checkpoint.id if r.checkpoint else "",
            r.checkpoint.name if r.checkpoint else "",
        ])

    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=checkins.csv"},
    )




# --------------------------- add / edit / delete ---------------------------

@checkins_bp.route("/add", methods=["GET", "POST"])
@roles_required("judge", "admin")
def add_checkin():
    teams = Team.query.order_by(Team.name.asc()).all()
    checkpoints = Checkpoint.query.order_by(Checkpoint.name.asc()).all()

    if request.method == "POST":
        team_id = request.form.get("team_id", type=int)
        checkpoint_id = request.form.get("checkpoint_id", type=int)
        override = request.form.get("override")

        # validate FKs
        if not Team.query.get(team_id) or not Checkpoint.query.get(checkpoint_id):
            flash("Invalid team or checkpoint.", "warning")
            return render_template("add_checkin.html", teams=teams, checkpoints=checkpoints, now=datetime.utcnow())

        # parse local → UTC; fallback is now()
        timestamp = _parse_timestamp_from_form(datetime.utcnow())

        existing = Checkin.query.filter_by(team_id=team_id, checkpoint_id=checkpoint_id).first()
        if existing and override != "replace":
            flash("This team has already checked in at this checkpoint.", "warning")
            return render_template(
                "add_checkin.html",
                teams=teams,
                checkpoints=checkpoints,
                dup_team_id=team_id,
                dup_checkpoint_id=checkpoint_id,
                # keep user input in the field if they posted it
                timestamp_prefill=request.form.get("timestamp_local") or "",
                suggest_override=True,
            )

        if existing and override == "replace":
            existing.timestamp = timestamp
            db.session.commit()
            flash("Existing check-in replaced with the new timestamp.", "success")
            return redirect(url_for("checkins.list_checkins"))

        db.session.add(Checkin(team_id=team_id, checkpoint_id=checkpoint_id, timestamp=timestamp))
        db.session.commit()
        flash("Check-in recorded.", "success")
        return redirect(url_for("checkins.list_checkins"))

    # GET
    return render_template("add_checkin.html", teams=teams, checkpoints=checkpoints, now=datetime.utcnow())


@checkins_bp.route("/<int:checkin_id>/edit", methods=["GET", "POST"])
@roles_required("judge", "admin")
def edit_checkin(checkin_id: int):
    c = Checkin.query.get_or_404(checkin_id)
    teams = Team.query.order_by(Team.name.asc()).all()
    checkpoints = Checkpoint.query.order_by(Checkpoint.name.asc()).all()

    if request.method == "POST":
        new_team_id = request.form.get("team_id", type=int)
        new_cp_id = request.form.get("checkpoint_id", type=int)
        override = request.form.get("override")


        # validate FKs
        if not Team.query.get(new_team_id) or not Checkpoint.query.get(new_cp_id):
            flash("Invalid team or checkpoint.", "warning")
            return render_template(
                "checkin_edit.html",
                c=c,
                teams=teams,
                checkpoints=checkpoints,
                timestamp_local=to_datetime_local(c.timestamp),
            )

        # parse local → UTC; fallback is current stored timestamp
        new_ts = _parse_timestamp_from_form(c.timestamp)

        # Is there another row with same (team, checkpoint)?
        dup = (
            Checkin.query
            .filter(
                Checkin.team_id == new_team_id,
                Checkin.checkpoint_id == new_cp_id,
                Checkin.id != checkin_id,
            )
            .first()
        )

        if dup and override != "replace":
            flash("Another check-in for that team & checkpoint already exists.", "warning")
            return render_template(
                "checkin_edit.html",
                c=c,
                teams=teams,
                checkpoints=checkpoints,
                timestamp_local=to_datetime_local(new_ts),
                suggest_override=True,
                pending_team_id=new_team_id,
                pending_cp_id=new_cp_id,
            )

        if dup and override == "replace":
            db.session.delete(dup)
            db.session.flush()
            c.team_id = new_team_id
            c.checkpoint_id = new_cp_id
            c.timestamp = new_ts
            db.session.commit()
            flash("Replaced the other check-in and saved your changes.", "success")
            return redirect(url_for("checkins.list_checkins"))

        # Normal update
        c.team_id = new_team_id
        c.checkpoint_id = new_cp_id
        c.timestamp = new_ts
        db.session.commit()
        flash("Check-in updated.", "success")
        return redirect(url_for("checkins.list_checkins"))

    # ---- GET: render the form ----
    return render_template(
        "checkin_edit.html",
        c=c,
        teams=teams,
        checkpoints=checkpoints,
        timestamp_local=to_datetime_local(c.timestamp),
    )


@checkins_bp.route("/<int:checkin_id>/delete", methods=["POST"])
@roles_required("judge", "admin")
def delete_checkin(checkin_id: int):
    c = Checkin.query.get_or_404(checkin_id)
    db.session.delete(c)
    db.session.commit()
    flash("Check-in deleted.", "success")
    return redirect(url_for("checkins.list_checkins"))