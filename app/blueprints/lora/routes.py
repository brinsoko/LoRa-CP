from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app, jsonify, abort
from sqlalchemy.orm import joinedload
from datetime import datetime
from app.extensions import db
from app.models import LoRaDevice, Checkpoint, RFIDCard, Checkin, Team
from app.utils.perms import roles_required

lora_bp = Blueprint("lora", __name__, template_folder="../../templates")



# ---------- CRUD ----------
@lora_bp.route("/")
def lora_list():
    devices = LoRaDevice.query.order_by(LoRaDevice.name.asc().nulls_last()).all()
    return render_template("lora_list.html", devices=devices)

@lora_bp.route("/add", methods=["GET","POST"])
@roles_required("judge","admin")
def add_device():
    if request.method == "POST":
        dev_eui = (request.form.get("dev_eui") or "").strip() or None
        dev_num  = (request.form.get("dev_num") or "").strip()
        name    = (request.form.get("name") or "").strip() or None
        note    = (request.form.get("note") or "").strip() or None
        if not dev_num:
            flash("Device number is required.", "warning")
            return render_template("lora_add.html")
        if LoRaDevice.query.filter_by(dev_num=dev_num).first():
            flash("A device with that number already exists.", "warning")
            return render_template("lora_add.html")
        d = LoRaDevice(dev_eui=dev_eui,dev_num=dev_num, name=name, note=note, active=True)
        db.session.add(d); db.session.commit()
        flash("LoRa device added.", "success")
        return redirect(url_for("lora.lora_list"))
    return render_template("lora_add.html")

@lora_bp.route("/<int:device_id>/edit", methods=["GET","POST"])
@roles_required("judge","admin")
def edit_device(device_id):
    d = LoRaDevice.query.get_or_404(device_id)
    if request.method == "POST":
        d.dev_eui = (request.form.get("dev_eui") or "").strip() or None
        d.dev_num  = (request.form.get("dev_num") or "").strip()
        d.name    = (request.form.get("name") or "").strip() or None
        d.note    = (request.form.get("note") or "").strip() or None
        d.active  = bool(request.form.get("active"))
        if not d.dev_eui:
            flash("Device EUI is required.", "warning")
            return render_template("lora_edit.html", d=d)
        # ensure uniqueness
        exists = LoRaDevice.query.filter(LoRaDevice.dev_eui==d.dev_eui, LoRaDevice.id!=d.id).first()
        if exists:
            flash("Another device already uses that EUI.", "warning")
            return render_template("lora_edit.html", d=d)
        db.session.commit()
        flash("LoRa device updated.", "success")
        return redirect(url_for("lora.lora_list"))
    return render_template("lora_edit.html", d=d)

@lora_bp.route("/<int:device_id>/delete", methods=["POST"])
@roles_required("admin")
def delete_device(device_id):
    d = LoRaDevice.query.get_or_404(device_id)
    # Unlink from checkpoint if linked (because lora_device_id is unique)
    cp = d.checkpoint
    if cp:
        cp.lora_device_id = None
    db.session.delete(d)
    db.session.commit()
    flash("LoRa device deleted.", "success")
    return redirect(url_for("lora.lora_list"))
