# app/blueprints/lora/routes.py
from __future__ import annotations

from flask import Blueprint, render_template, request, redirect, url_for, flash

from datetime import datetime

from app.utils.frontend_api import api_json
from app.utils.perms import roles_required


lora_bp = Blueprint("lora", __name__, template_folder="../../templates")


def _decorate_devices(devices):
    decorated = []
    for device in devices:
        last_seen = device.get("last_seen")
        if last_seen:
            try:
                dt = datetime.fromisoformat(last_seen)
                display_last_seen = dt.strftime("%Y-%m-%d %H:%M:%S")
            except Exception:
                display_last_seen = last_seen
        else:
            display_last_seen = "—"

        checkpoint = device.get("checkpoint") or {}
        checkpoint_name = checkpoint.get("name") if isinstance(checkpoint, dict) else None

        decorated.append(
            {
                **device,
                "display_last_seen": display_last_seen,
                "checkpoint_name": checkpoint_name,
            }
        )
    return decorated


def _fetch_devices():
    resp, payload = api_json("GET", "/api/lora/devices")
    if resp.status_code != 200:
        flash("Could not load LoRa devices.", "warning")
        return []
    return _decorate_devices(payload.get("devices", []))


@lora_bp.route("/", methods=["GET"])
@roles_required("judge", "admin")
def lora_list():
    devices = _fetch_devices()
    return render_template("lora_list.html", devices=devices)


@lora_bp.route("/add", methods=["GET", "POST"])
@roles_required("judge", "admin")
def add_device():
    if request.method == "POST":
        dev_eui = (request.form.get("dev_eui") or "").strip() or None
        dev_num = (request.form.get("dev_num") or "").strip()
        name = (request.form.get("name") or "").strip() or None
        note = (request.form.get("note") or "").strip() or None
        model = (request.form.get("model") or "").strip() or None
        active = bool(request.form.get("active", "on"))

        if not dev_num:
            flash("Device number is required.", "warning")
            return render_template("lora_add.html")

        resp, payload = api_json(
            "POST",
            "/api/lora/devices",
            json={
                "dev_eui": dev_eui,
                "dev_num": dev_num,
                "name": name,
                "note": note,
                "model": model,
                "active": active,
            },
        )

        if resp.status_code == 201:
            flash("LoRa device added.", "success")
            return redirect(url_for("lora.lora_list"))

        flash(payload.get("detail") or payload.get("error") or "Could not add device.", "warning")

    return render_template("lora_add.html")


@lora_bp.route("/<int:device_id>/edit", methods=["GET", "POST"])
@roles_required("judge", "admin")
def edit_device(device_id: int):
    device_resp, device_payload = api_json("GET", f"/api/lora/devices/{device_id}")
    if device_resp.status_code != 200:
        flash("Device not found.", "warning")
        return redirect(url_for("lora.lora_list"))

    device = _decorate_devices([device_payload])[0] if device_payload else {}

    if request.method == "POST":
        dev_eui = (request.form.get("dev_eui") or "").strip() or None
        dev_num = (request.form.get("dev_num") or "").strip()
        name = (request.form.get("name") or "").strip() or None
        note = (request.form.get("note") or "").strip() or None
        model = (request.form.get("model") or "").strip() or None
        active = bool(request.form.get("active"))

        if not dev_num:
            flash("Device number is required.", "warning")
            device.update({
                "dev_eui": dev_eui,
                "dev_num": dev_num,
                "name": name,
                "note": note,
                "model": model,
                "active": active,
            })
            return render_template("lora_edit.html", d=device)

        resp, payload = api_json(
            "PATCH",
            f"/api/lora/devices/{device_id}",
            json={
                "dev_eui": dev_eui,
                "dev_num": dev_num,
                "name": name,
                "note": note,
                "model": model,
                "active": active,
            },
        )

        if resp.status_code == 200:
            flash("LoRa device updated.", "success")
            return redirect(url_for("lora.lora_list"))

        flash(payload.get("detail") or payload.get("error") or "Could not update device.", "warning")
        device.update({
            "dev_eui": dev_eui,
            "dev_num": dev_num,
            "name": name,
            "note": note,
            "model": model,
            "active": active,
        })
        device = _decorate_devices([device])[0]

    return render_template("lora_edit.html", d=device)


@lora_bp.route("/<int:device_id>/delete", methods=["POST"])
@roles_required("admin")
def delete_device(device_id: int):
    resp, payload = api_json("DELETE", f"/api/lora/devices/{device_id}")

    if resp.status_code == 200:
        flash("LoRa device deleted.", "success")
    else:
        flash(payload.get("detail") or payload.get("error") or "Could not delete device.", "warning")

    return redirect(url_for("lora.lora_list"))
