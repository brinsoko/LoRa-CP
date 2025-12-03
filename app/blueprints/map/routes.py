# app/blueprints/map/routes.py
from flask import Blueprint, render_template
from app.models import Team, LoRaDevice

maps_bp = Blueprint("maps", __name__, template_folder="../../templates")

@maps_bp.route("/", methods=["GET"])
def index():
    teams = Team.query.order_by(Team.name.asc()).all()
    return render_template("map.html", teams=teams)


@maps_bp.route("/devices", methods=["GET"])
@maps_bp.route("/lora", methods=["GET"])
def lora_map():
    devices = LoRaDevice.query.order_by(LoRaDevice.dev_num.asc()).all()
    return render_template("lora_map.html", devices=devices)
