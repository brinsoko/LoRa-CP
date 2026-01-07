# app/blueprints/map/routes.py
from flask import Blueprint, render_template
from app.models import Team, LoRaDevice
from app.utils.perms import roles_required
from app.utils.competition import get_current_competition_id

maps_bp = Blueprint("maps", __name__, template_folder="../../templates")

@maps_bp.route("/", methods=["GET"])
@roles_required("judge", "admin")
def index():
    comp_id = get_current_competition_id()
    if comp_id:
        teams = Team.query.filter(Team.competition_id == comp_id).order_by(Team.name.asc()).all()
    else:
        teams = []
    return render_template("map.html", teams=teams)


@maps_bp.route("/devices", methods=["GET"])
@maps_bp.route("/lora", methods=["GET"])
@roles_required("judge", "admin")
def lora_map():
    comp_id = get_current_competition_id()
    if comp_id:
        devices = (
            LoRaDevice.query
            .filter(LoRaDevice.competition_id == comp_id)
            .order_by(LoRaDevice.dev_num.asc())
            .all()
        )
    else:
        devices = []
    return render_template("lora_map.html", devices=devices)
