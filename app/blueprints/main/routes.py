from flask import Blueprint, render_template, request, Response
from sqlalchemy.orm import joinedload
from app.extensions import db
from app.models import Team, Checkpoint, Checkin
from app.utils.time import to_datetime_local
import io, csv
from datetime import datetime, timedelta

main_bp = Blueprint('main', __name__)

@main_bp.route("/")
def index():
    return render_template("base.html")




def _parse_date_range(date_from_str, date_to_str):
    start = end = None
    try:
        if date_from_str: start = datetime.fromisoformat(date_from_str)
        if date_to_str: end = datetime.fromisoformat(date_to_str) + timedelta(days=1)
    except ValueError:
        pass
    return start, end

def _filtered_checkins(team_id, checkpoint_id, date_from_str, date_to_str):
    q = (Checkin.query
         .options(joinedload(Checkin.team), joinedload(Checkin.checkpoint)))
    if team_id: q = q.filter(Checkin.team_id == team_id)
    if checkpoint_id: q = q.filter(Checkin.checkpoint_id == checkpoint_id)
    date_from, date_to = _parse_date_range(date_from_str, date_to_str)
    if date_from: q = q.filter(Checkin.timestamp >= date_from)
    if date_to: q = q.filter(Checkin.timestamp < date_to)
    return q.order_by(Checkin.timestamp.desc())

@main_bp.route("/checkins")
def view_checkins():
    teams = Team.query.order_by(Team.name.asc()).all()
    cps = Checkpoint.query.order_by(Checkpoint.name.asc()).all()
    team_id = request.args.get('team_id', type=int)
    cp_id = request.args.get('checkpoint_id', type=int)
    df = request.args.get('date_from')
    dt = request.args.get('date_to')
    checkins = _filtered_checkins(team_id, cp_id, df, dt).all()
    return render_template("view_checkins.html",
        checkins=checkins, teams=teams, checkpoints=cps,
        selected_team_id=team_id, selected_checkpoint_id=cp_id,
        selected_date_from=df or "", selected_date_to=dt or "")

@main_bp.route("/checkins.csv")
def export_checkins_csv():
    team_id = request.args.get('team_id', type=int)
    cp_id = request.args.get('checkpoint_id', type=int)
    df = request.args.get('date_from'); dt = request.args.get('date_to')
    rows = _filtered_checkins(team_id, cp_id, df, dt).all()
    si = io.StringIO(); w = csv.writer(si)
    w.writerow(["timestamp_utc","team_id","team_name","checkpoint_id","checkpoint_name"])
    for r in rows:
        w.writerow([r.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
                    r.team.id if r.team else "",
                    r.team.name if r.team else "",
                    r.checkpoint.id if r.checkpoint else "",
                    r.checkpoint.name if r.checkpoint else ""])
    return Response(si.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=checkins.csv"})