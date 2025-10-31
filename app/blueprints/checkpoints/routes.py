# app/blueprints/checkpoints/routes.py
from __future__ import annotations

from flask import Blueprint, render_template, request, redirect, url_for, flash

from app.utils.frontend_api import api_json
from app.utils.perms import roles_required


checkpoints_bp = Blueprint("checkpoints", __name__, template_folder="../../templates")


def _fetch_groups():
    resp, payload = api_json("GET", "/api/groups")
    if resp.status_code != 200:
        flash("Could not load groups.", "warning")
        return []
    return payload.get("groups", [])


def _fetch_devices():
    resp, payload = api_json("GET", "/api/lora/devices")
    if resp.status_code != 200:
        flash("Could not load LoRa devices.", "warning")
        return []
    return payload.get("devices", [])


def _fetch_checkpoints():
    resp, payload = api_json("GET", "/api/checkpoints")
    if resp.status_code != 200:
        flash("Could not load checkpoints.", "warning")
        return []
    return payload.get("checkpoints", [])


def _normalize_checkpoint_form(form):
    name = (form.get("name") or "").strip()
    location = (form.get("location") or "").strip() or None
    description = (form.get("description") or "").strip() or None
    easting_raw = form.get("easting")
    northing_raw = form.get("northing")
    easting = float(easting_raw) if easting_raw else None
    northing = float(northing_raw) if northing_raw else None
    lora_device_raw = form.get("lora_device_id")
    lora_device_id = int(lora_device_raw) if lora_device_raw else None
    group_ids = [int(x) for x in form.getlist("group_ids")]

    return {
        "name": name,
        "location": location,
        "description": description,
        "easting": easting,
        "northing": northing,
        "lora_device_id": lora_device_id,
        "group_ids": group_ids,
    }


@checkpoints_bp.route("/", methods=["GET"])
@roles_required("judge", "admin")
def list_checkpoints():
    checkpoints = _fetch_checkpoints()
    return render_template("checkpoints_list.html", checkpoints=checkpoints)


@checkpoints_bp.route("/add", methods=["GET", "POST"])
@roles_required("judge", "admin")
def add_checkpoint():
    groups = _fetch_groups()
    devices = _fetch_devices()

    form_data = _normalize_checkpoint_form(request.form) if request.method == "POST" else None

    if request.method == "POST":
        if not form_data["name"]:
            flash("Name is required.", "warning")
            return render_template(
                "add_checkpoint.html",
                groups=groups,
                devices=devices,
                selected_group_ids=form_data["group_ids"],
                selected_device_id=form_data["lora_device_id"] or "",
            )

        resp, payload = api_json("POST", "/api/checkpoints", json=form_data)
        if resp.status_code == 201:
            flash("Checkpoint added.", "success")
            return redirect(url_for("checkpoints.list_checkpoints"))

        flash(payload.get("error") or payload.get("detail") or "Could not add checkpoint.", "warning")

    return render_template(
        "add_checkpoint.html",
        groups=groups,
        devices=devices,
        selected_group_ids=form_data["group_ids"] if form_data else [],
        selected_device_id=form_data["lora_device_id"] if form_data else "",
    )


@checkpoints_bp.route("/<int:cp_id>/edit", methods=["GET", "POST"])
@roles_required("judge", "admin")
def edit_checkpoint(cp_id: int):
    cp_resp, cp_payload = api_json("GET", f"/api/checkpoints/{cp_id}")
    if cp_resp.status_code != 200:
        flash("Checkpoint not found.", "warning")
        return redirect(url_for("checkpoints.list_checkpoints"))

    checkpoint = cp_payload or {}
    if not isinstance(checkpoint, dict):
        checkpoint = {}
    groups = _fetch_groups()
    devices = _fetch_devices()

    existing_group_ids = [g.get("id") for g in checkpoint.get("groups", []) if isinstance(g, dict)]

    if request.method == "POST":
        form_data = _normalize_checkpoint_form(request.form)

        if not form_data["name"]:
            flash("Name is required.", "warning")
            checkpoint.update({k: v for k, v in form_data.items() if k != "group_ids"})
            checkpoint["groups"] = [
                next((g for g in groups if g.get("id") == gid), {"id": gid, "name": "Unknown"})
                for gid in form_data["group_ids"]
            ]
            return render_template(
                "checkpoint_edit.html",
                cp=checkpoint,
                groups=groups,
                devices=devices,
                selected_group_ids=form_data["group_ids"],
                selected_device_id=form_data["lora_device_id"] or "",
            )

        resp, payload = api_json("PATCH", f"/api/checkpoints/{cp_id}", json=form_data)
        if resp.status_code == 200:
            flash("Checkpoint updated.", "success")
            return redirect(url_for("checkpoints.list_checkpoints"))

        flash(payload.get("error") or payload.get("detail") or "Could not update checkpoint.", "warning")
        checkpoint.update({k: v for k, v in form_data.items() if k != "group_ids"})
        checkpoint["groups"] = [
            next((g for g in groups if g.get("id") == gid), {"id": gid, "name": "Unknown"})
            for gid in form_data["group_ids"]
        ]
        selected_ids = form_data["group_ids"]
        selected_device_id = form_data["lora_device_id"] or ""
    else:
        selected_ids = existing_group_ids
        lora_info = checkpoint.get("lora_device") or {}
        if not isinstance(lora_info, dict):
            lora_info = {}
        selected_device_id = lora_info.get("id") or ""

    return render_template(
        "checkpoint_edit.html",
        cp=checkpoint,
        groups=groups,
        devices=devices,
        selected_group_ids=selected_ids,
        selected_device_id=selected_device_id,
    )


@checkpoints_bp.route("/<int:cp_id>/delete", methods=["POST"])
@roles_required("admin")
def delete_checkpoint(cp_id: int):
    resp, payload = api_json("DELETE", f"/api/checkpoints/{cp_id}")

    if resp.status_code == 200:
        flash("Checkpoint deleted.", "success")
    else:
        flash(payload.get("detail") or payload.get("error") or "Could not delete checkpoint.", "warning")

    return redirect(url_for("checkpoints.list_checkpoints"))


@checkpoints_bp.route("/import_json", methods=["GET", "POST"])
@roles_required("judge", "admin")
def import_checkpoints_json():
    # TODO: reimplement using API if bulk import endpoint becomes available
    flash("JSON import via UI is temporarily disabled; use the API directly.", "warning")
    return redirect(url_for("checkpoints.list_checkpoints"))
