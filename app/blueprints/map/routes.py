# app/blueprints/map/routes.py
from flask import Blueprint, render_template
from app.models import Team

maps_bp = Blueprint("maps", __name__, template_folder="../../templates")

@maps_bp.route("/", methods=["GET"])
def index():
    teams = Team.query.order_by(Team.name.asc()).all()
    return render_template("map.html", teams=teams)