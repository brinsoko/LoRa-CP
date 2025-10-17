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
        dev_eui = (request.form.get("dev_eui") or "").strip()
        name    = (request.form.get("name") or "").strip() or None
        note    = (request.form.get("note") or "").strip() or None
        if not dev_eui:
            flash("Device EUI is required.", "warning")
            return render_template("lora_add.html")
        if LoRaDevice.query.filter_by(dev_eui=dev_eui).first():
            flash("A device with that EUI already exists.", "warning")
            return render_template("lora_add.html")
        d = LoRaDevice(dev_eui=dev_eui, name=name, note=note, active=True)
        db.session.add(d); db.session.commit()
        flash("LoRa device added.", "success")
        return redirect(url_for("lora.lora_list"))
    return render_template("lora_add.html")

@lora_bp.route("/<int:device_id>/edit", methods=["GET","POST"])
@roles_required("judge","admin")
def edit_device(device_id):
    d = LoRaDevice.query.get_or_404(device_id)
    if request.method == "POST":
        d.dev_eui = (request.form.get("dev_eui") or "").strip()
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

# ---------- Webhook (optional) ----------
@lora_bp.route("/webhook", methods=["POST"])
def lora_webhook():
    """
    Minimal example payload (customize to match your network):
    {
      "secret": "XYZ123",               # simple shared secret
      "dev_eui": "ABCDEF1234567890",
      "uid": "04AABBCCDD",              # RFID card UID read at that checkpoint (optional)
      "rssi": -85,                      # optional
      "battery": 3.7                    # optional
    }
    Behavior:
      - find device by dev_eui -> map to checkpoint
      - if uid present -> map to Team via RFIDCard.uid and create/replace check-in for that team at that checkpoint
      - update device last_seen/rssi/battery
    """
    data = request.get_json(silent=True) or {}
    secret = (data.get("secret") or "").strip()
    if not secret or secret != getattr(current_app.config, "LORA_WEBHOOK_SECRET", None):
        abort(403)

    dev_eui = (data.get("dev_eui") or "").strip()
    if not dev_eui:
        return jsonify({"ok": False, "error": "Missing dev_eui"}), 400

    d = LoRaDevice.query.filter_by(dev_eui=dev_eui).first()
    if not d:
        return jsonify({"ok": False, "error": "Unknown device"}), 404

    # update telemetry
    d.last_seen = datetime.utcnow()
    if "rssi" in data:    d.last_rssi = data.get("rssi")
    if "battery" in data: d.battery   = data.get("battery")

    cp = d.checkpoint
    if not cp:
        db.session.commit()
        return jsonify({"ok": True, "warning": "Device not linked to a checkpoint; nothing recorded."}), 200

    uid = (data.get("uid") or "").strip()
    if not uid:
        db.session.commit()
        return jsonify({"ok": True, "info": "No UID provided; only device status updated."}), 200

    # Map UID -> Team via RFIDCard
    card = RFIDCard.query.filter_by(uid=uid).first()
    if not card or not card.team_id:
        db.session.commit()
        return jsonify({"ok": True, "warning": "UID not mapped to a team; status updated only."}), 200

    team_id = card.team_id

    # Enforce one check-in per team per checkpoint: replace if exists
    existing = Checkin.query.filter_by(team_id=team_id, checkpoint_id=cp.id).first()
    if existing:
        existing.timestamp = datetime.utcnow()
        db.session.commit()
        return jsonify({"ok": True, "action": "replaced"}), 200
    else:
        db.session.add(Checkin(team_id=team_id, checkpoint_id=cp.id, timestamp=datetime.utcnow()))
        db.session.commit()
        return jsonify({"ok": True, "action": "created"}), 201