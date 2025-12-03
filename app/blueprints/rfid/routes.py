# app/blueprints/rfid/routes.py
from __future__ import annotations

from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify

from app.utils.frontend_api import api_json
from app.utils.perms import roles_required

rfid_bp = Blueprint("rfid", __name__, template_folder="../../templates")


def _fetch_cards():
    resp, payload = api_json("GET", "/api/rfid/cards")
    if resp.status_code != 200:
        flash("Could not load RFID mappings.", "warning")
        return []
    return payload.get("cards", [])


def _fetch_teams():
    resp, payload = api_json("GET", "/api/teams", params={"sort": "name_asc"})
    if resp.status_code != 200:
        flash("Could not load teams.", "warning")
        return []
    return payload.get("teams", [])


def _fetch_devices():
    resp, payload = api_json("GET", "/api/devices")
    if resp.status_code != 200:
        flash("Could not load devices.", "warning")
        return []
    devices = payload.get("devices", [])
    try:
        devices = sorted(devices, key=lambda d: (d.get("dev_num") is None, d.get("dev_num")))
    except Exception:
        pass
    return devices


@rfid_bp.route("/", methods=["GET"])
@roles_required("judge", "admin")
def list_rfid():
    cards = _fetch_cards()
    return render_template("rfid_list.html", cards=cards)


@rfid_bp.route("/add", methods=["GET", "POST"])
@roles_required("judge", "admin")
def add_rfid():
    teams = _fetch_teams()
    selected_team_id = request.form.get("team_id", type=int) if request.method == "POST" else None
    uid_value = request.form.get("uid", "") if request.method == "POST" else ""
    number_value = request.form.get("number", "") if request.method == "POST" else ""

    if request.method == "POST":
        uid = (request.form.get("uid") or "").strip()
        team_id = request.form.get("team_id", type=int)
        number = request.form.get("number", type=int)

        payload = {"uid": uid, "team_id": team_id, "number": number}

        resp, body = api_json("POST", "/api/rfid/cards", json=payload)
        if resp.status_code == 201:
            flash("RFID mapping created.", "success")
            return redirect(url_for("rfid.list_rfid"))

        flash(body.get("detail") or body.get("error") or "Could not create RFID mapping.", "warning")

    return render_template(
        "rfid_add.html",
        teams=teams,
        selected_team_id=selected_team_id,
        uid_value=uid_value,
        number_value=number_value,
    )


@rfid_bp.route("/<int:card_id>/edit", methods=["GET", "POST"])
@roles_required("judge", "admin")
def edit_rfid(card_id: int):
    card_resp, card_payload = api_json("GET", f"/api/rfid/cards/{card_id}")
    if card_resp.status_code != 200:
        flash("RFID card not found.", "warning")
        return redirect(url_for("rfid.list_rfid"))

    card = card_payload
    teams = _fetch_teams()
    selected_team_id = request.form.get("team_id", type=int) if request.method == "POST" else (card.get("team", {}) or {}).get("id")

    if request.method == "POST":
        uid = (request.form.get("uid") or "").strip()
        team_id = request.form.get("team_id", type=int)
        number = request.form.get("number", type=int)

        payload = {"uid": uid, "team_id": team_id, "number": number}

        resp, body = api_json("PATCH", f"/api/rfid/cards/{card_id}", json=payload)
        if resp.status_code == 200:
            flash("RFID mapping updated.", "success")
            return redirect(url_for("rfid.list_rfid"))

        flash(body.get("detail") or body.get("error") or "Could not update RFID mapping.", "warning")
        card.update({"uid": uid, "number": number})
        if selected_team_id:
            team_info = next((t for t in teams if t.get("id") == selected_team_id), None)
            card["team"] = {
                "id": team_info.get("id") if team_info else selected_team_id,
                "name": team_info.get("name") if team_info else None,
                "number": team_info.get("number") if team_info else None,
            }
        else:
            card["team"] = None

    return render_template(
        "rfid_edit.html",
        card=card,
        teams=teams,
        selected_team_id=selected_team_id,
    )


@rfid_bp.route("/judge-console", methods=["GET"])
@roles_required("judge", "admin")
def judge_console():
    devices = _fetch_devices()
    return render_template("rfid_judge.html", devices=devices)


@rfid_bp.route("/finish", methods=["GET"])
@roles_required("judge", "admin")
def finish_console():
    devices = _fetch_devices()
    return render_template("rfid_finish.html", devices=devices)


@rfid_bp.route("/<int:card_id>/delete", methods=["POST"])
@roles_required("admin")
def delete_rfid(card_id: int):
    resp, body = api_json("DELETE", f"/api/rfid/cards/{card_id}")
    if resp.status_code == 200:
        flash("RFID mapping deleted.", "success")
    else:
        flash(body.get("detail") or body.get("error") or "Could not delete RFID mapping.", "warning")
    return redirect(url_for("rfid.list_rfid"))


@rfid_bp.route("/scan_once", methods=["POST"])
@roles_required("judge", "admin")
def rfid_scan_once():
    resp, body = api_json("POST", "/api/rfid/scan")
    return jsonify(body), resp.status_code


@rfid_bp.route("/upload_csv", methods=["GET"])
@roles_required("admin")
def rfid_upload_csv_form():
    flash("CSV upload via UI is disabled. Use /api/rfid/import instead.", "warning")
    return redirect(url_for("rfid.list_rfid"))


@rfid_bp.route("/upload_csv", methods=["POST"])
@roles_required("admin")
def rfid_upload_csv():
    flash("CSV upload via UI is disabled. Use /api/rfid/import instead.", "warning")
    return redirect(url_for("rfid.list_rfid"))
