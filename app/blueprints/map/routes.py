from flask import Blueprint, render_template, request, jsonify
from app.models import Team
from app.utils.status import compute_team_statuses

# rename blueprint: was "map", now "maps"
maps_bp = Blueprint("maps", __name__, template_folder="../../templates")

@maps_bp.route("/", methods=["GET"])
def index():
    teams = Team.query.order_by(Team.name.asc()).all()
    return render_template("map.html", teams=teams, google_maps_key=None)

@maps_bp.route("/api/team_targets", methods=["GET"])
def api_team_targets():
    team_id = request.args.get("team_id", type=int)
    if not team_id:
        return jsonify({"error": "team_id required"}), 400
    ordered, status = compute_team_statuses(team_id)
    return jsonify([
        {
            "checkpoint_id": cp.id,
            "name": cp.name,
            "easting": cp.easting,
            "northing": cp.northing,
            "status": status.get(cp.id, "not_found"),
        }
        for cp in ordered
    ])