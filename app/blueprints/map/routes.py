# app/blueprints/map/routes.py
from flask import Blueprint, render_template, jsonify, request, current_app
from sqlalchemy.orm import joinedload
from app.models import Checkpoint, Team, Checkin

maps_bp = Blueprint("maps", __name__, template_folder="../../templates")

# ---------- PAGE ----------
@maps_bp.route("/", methods=["GET"])
def index():
    # Public page; map JS will fetch JSON below.
    # Provide teams list for the dropdown.
    teams = Team.query.order_by(Team.name.asc()).all()
    return render_template("map.html", teams=teams)

# ---------- API: all checkpoints ----------
@maps_bp.route("/api/checkpoints", methods=["GET"])
def api_checkpoints():
    cps = Checkpoint.query.order_by(Checkpoint.name.asc()).all()
    # return minimal fields the map needs
    return jsonify([
        {
            "id": c.id,
            "name": c.name,
            "easting": c.easting,
            "northing": c.northing,
        }
        for c in cps
    ])

# ---------- API: which checkpoints a team has found ----------
@maps_bp.route("/api/team_found", methods=["GET"])
def api_team_found():
    team_id = request.args.get("team_id", type=int)
    if not team_id:
        return jsonify({"found": []})
    found_ids = (
        Checkin.query
        .with_entities(Checkin.checkpoint_id)
        .filter(Checkin.team_id == team_id)
        .distinct()
        .all()
    )
    return jsonify({"found": [cid for (cid,) in found_ids]})