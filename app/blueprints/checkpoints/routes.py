# app/blueprints/checkpoints/routes.py
from __future__ import annotations

import json

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _

from app.utils.frontend_api import api_json
from app.utils.perms import roles_required
from app.utils.validators import validate_finite_float

checkpoints_bp = Blueprint("checkpoints", __name__, template_folder="../../templates")


def _checkpoint_error_message(payload: dict, default_msg: str) -> str:
    error = payload.get("error")
    if error == "device_in_use":
        cp_name = payload.get("checkpoint_name") or payload.get("checkpoint")
        if cp_name:
            return _("Device is already assigned to %(checkpoint)s.", checkpoint=cp_name)
        return _("Device is already assigned to another checkpoint.")
    if error == "invalid_device":
        return _("Selected device does not exist.")
    if error == "invalid_device_competition":
        return _("Selected device is not available for this competition.")
    if error == "duplicate":
        return _("Checkpoint name already exists.")
    if payload.get("detail"):
        return payload.get("detail")
    return _(default_msg)


def _fetch_groups():
    resp, payload = api_json("GET", "/api/groups")
    if resp.status_code != 200:
        flash(_("Could not load groups."), "warning")
        return []
    return payload.get("groups", [])


def _fetch_devices():
    resp, payload = api_json("GET", "/api/devices")
    if resp.status_code != 200:
        flash(_("Could not load devices."), "warning")
        return []
    return payload.get("devices", [])


def _fetch_checkpoints():
    resp, payload = api_json("GET", "/api/checkpoints")
    if resp.status_code != 200:
        flash(_("Could not load checkpoints."), "warning")
        return []
    return payload.get("checkpoints", [])


def _parse_optional_int(raw_value, field_label: str) -> tuple[int | None, str | None]:
    if raw_value in (None, ""):
        return None, None
    try:
        return int(str(raw_value).strip()), None
    except (TypeError, ValueError):
        return None, _("%(field)s must be an integer.", field=field_label)


def _parse_int_list(values, field_label: str) -> tuple[list[int], str | None]:
    parsed: list[int] = []
    for value in values or []:
        item, err = _parse_optional_int(value, field_label)
        if err:
            return [], err
        if item is not None:
            parsed.append(item)
    return parsed, None


def _normalize_checkpoint_form(form):
    name = (form.get("name") or "").strip()
    location = (form.get("location") or "").strip() or None
    description = (form.get("description") or "").strip() or None
    scoring_text = (form.get("scoring_text") or "").strip() or None
    judges_note = (form.get("judges_note") or "").strip() or None
    easting, easting_error = validate_finite_float(form.get("easting"), field_name="Easting")
    northing, northing_error = validate_finite_float(form.get("northing"), field_name="Northing")
    lora_device_id, lora_device_error = _parse_optional_int(form.get("lora_device_id"), "Device ID")
    group_ids, group_ids_error = _parse_int_list(form.getlist("group_ids"), "Group ID")

    is_virtual = form.get("is_virtual") in ("on", "true", "True", "1")

    return {
        "name": name,
        "location": location,
        "description": description,
        "scoring_text": scoring_text,
        "judges_note": judges_note,
        "easting": easting,
        "northing": northing,
        "lora_device_id": lora_device_id,
        "group_ids": group_ids,
        "is_virtual": is_virtual,
    }, easting_error or northing_error or lora_device_error or group_ids_error


def _fetch_competition_members() -> list[dict]:
    """Active members of the current competition, for the judges-edit modal.

    Returns a minimal dict per user so the template can render checkboxes
    without touching the ORM directly.
    """
    from app.extensions import db as _db
    from app.models import CompetitionMember, User
    from app.utils.competition import get_current_competition_id

    comp_id = get_current_competition_id()
    if not comp_id:
        return []
    rows = (
        _db.session.query(User.id, User.username, CompetitionMember.role)
        .join(CompetitionMember, CompetitionMember.user_id == User.id)
        .filter(
            CompetitionMember.competition_id == comp_id,
            CompetitionMember.active.is_(True),
        )
        .order_by(User.username.asc())
        .all()
    )
    return [{"id": uid, "username": uname, "role": role} for (uid, uname, role) in rows]


@checkpoints_bp.route("/", methods=["GET"])
@roles_required("judge", "admin")
def list_checkpoints():
    checkpoints = _fetch_checkpoints()
    eligible_users = _fetch_competition_members()
    return render_template(
        "checkpoints_list.html",
        checkpoints=checkpoints,
        eligible_users=eligible_users,
    )


@checkpoints_bp.route("/<int:cp_id>/judges", methods=["POST"])
@roles_required("admin")
def update_checkpoint_judges(cp_id: int):
    """Replace the set of judges assigned to a checkpoint.

    Scope every read/write by competition_id so editing one competition's
    judge assignments can never wipe a user's assignments in another. The
    JudgeCheckpoint.competition_id column (added in c6d7e8f9a0b1) makes
    this enforceable.
    """
    from flask_login import current_user

    from app.extensions import db as _db
    from app.models import Checkpoint as _Checkpoint
    from app.models import CompetitionMember, JudgeCheckpoint
    from app.utils.audit import record_audit_event
    from app.utils.competition import get_current_competition_id

    comp_id = get_current_competition_id()
    if not comp_id:
        flash(_("Select a competition first."), "warning")
        return redirect(url_for("main.select_competition"))

    cp = _Checkpoint.query.filter(_Checkpoint.competition_id == comp_id, _Checkpoint.id == cp_id).first()
    if not cp:
        flash(_("Checkpoint not found."), "warning")
        return redirect(url_for("checkpoints.list_checkpoints"))

    raw_ids = request.form.getlist("user_ids")
    parsed_ids: list[int] = []
    for raw in raw_ids:
        try:
            parsed_ids.append(int(raw))
        except (TypeError, ValueError):
            continue

    # Only allow assigning users who are active members of THIS competition.
    eligible_ids = {
        uid
        for (uid,) in _db.session.query(CompetitionMember.user_id)
        .filter(
            CompetitionMember.competition_id == comp_id,
            CompetitionMember.active.is_(True),
        )
        .all()
    }
    desired_ids = {uid for uid in parsed_ids if uid in eligible_ids}

    existing = JudgeCheckpoint.query.filter(
        JudgeCheckpoint.checkpoint_id == cp.id,
        JudgeCheckpoint.competition_id == comp_id,
    ).all()
    existing_ids = {row.user_id for row in existing}

    to_delete = [row for row in existing if row.user_id not in desired_ids]
    to_add = desired_ids - existing_ids

    for row in to_delete:
        _db.session.delete(row)
    for uid in to_add:
        _db.session.add(JudgeCheckpoint(user_id=uid, checkpoint_id=cp.id, competition_id=comp_id, is_default=False))

    record_audit_event(
        competition_id=comp_id,
        event_type="checkpoint_judges_updated",
        entity_type="checkpoint",
        entity_id=cp.id,
        actor_user=current_user if current_user.is_authenticated else None,
        summary=f"Checkpoint {cp.name} judges updated.",
        details={
            "before": sorted(existing_ids),
            "after": sorted(desired_ids),
            "added": sorted(to_add),
            "removed": sorted(uid for uid in existing_ids if uid not in desired_ids),
        },
    )
    _db.session.commit()
    flash(_("Checkpoint judges updated."), "success")
    return redirect(url_for("checkpoints.list_checkpoints"))


@checkpoints_bp.route("/add", methods=["GET", "POST"])
@roles_required("judge", "admin")
def add_checkpoint():
    groups = _fetch_groups()
    devices = _fetch_devices()

    form_data, form_error = _normalize_checkpoint_form(request.form) if request.method == "POST" else (None, None)

    if request.method == "POST":
        if form_error:
            flash(form_error, "warning")
            return render_template(
                "add_checkpoint.html",
                groups=groups,
                devices=devices,
                selected_group_ids=form_data["group_ids"] if form_data else [],
                selected_device_id=(form_data["lora_device_id"] if form_data else "") or "",
            )
        if not form_data["name"]:
            flash(_("Name is required."), "warning")
            return render_template(
                "add_checkpoint.html",
                groups=groups,
                devices=devices,
                selected_group_ids=form_data["group_ids"],
                selected_device_id=form_data["lora_device_id"] or "",
            )

        resp, payload = api_json("POST", "/api/checkpoints", json=form_data)
        if resp.status_code == 201:
            flash(_("Checkpoint added."), "success")
            return redirect(url_for("checkpoints.list_checkpoints"))

        flash(_checkpoint_error_message(payload, "Could not add checkpoint."), "warning")

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
        flash(_("Checkpoint not found."), "warning")
        return redirect(url_for("checkpoints.list_checkpoints"))

    checkpoint = cp_payload or {}
    if not isinstance(checkpoint, dict):
        checkpoint = {}
    groups = _fetch_groups()
    devices = _fetch_devices()

    existing_group_ids = [g.get("id") for g in checkpoint.get("groups", []) if isinstance(g, dict)]

    if request.method == "POST":
        form_data, form_error = _normalize_checkpoint_form(request.form)

        if form_error:
            flash(form_error, "warning")
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

        if not form_data["name"]:
            flash(_("Name is required."), "warning")
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
            flash(_("Checkpoint updated."), "success")
            return redirect(url_for("checkpoints.list_checkpoints"))

        flash(_checkpoint_error_message(payload, "Could not update checkpoint."), "warning")
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
        flash(_("Checkpoint deleted."), "success")
    else:
        message = payload.get("detail") or payload.get("error") or _("Could not delete checkpoint.")
        if isinstance(message, str):
            flash(message, "warning")
        else:
            flash(_("Could not delete checkpoint."), "warning")

    return redirect(url_for("checkpoints.list_checkpoints"))


@checkpoints_bp.route("/import_json", methods=["GET", "POST"])
@roles_required("judge", "admin")
def import_checkpoints_json():
    if request.method == "GET":
        sample_array = {
            "items": [
                {"name": "CP-01", "description": "Start", "easting": 123.45, "northing": 456.78},
                {"name": "CP-02", "location": "Forest edge"},
            ]
        }
        sample_cps = {
            "cp_size": 0.003,
            "cps": [
                {"e": 451226, "n": 66954, "name": ""},
                {"e": 450952.33, "n": 66900.33, "name": "B"},
            ],
            "bounds": [448002, 63545, 452542, 70208],
        }
        return render_template("checkpoints_import_json.html", sample=sample_array, sample_cps=sample_cps)

    upload = request.files.get("file")
    payload_text = ""
    if upload and upload.filename:
        try:
            payload_text = upload.read().decode("utf-8")
        except Exception:
            payload_text = ""
    if not payload_text:
        payload_text = (request.form.get("payload") or "").strip()
    if not payload_text:
        flash(_("Paste JSON payload to import."), "warning")
        return redirect(url_for("checkpoints.import_checkpoints_json"))

    try:
        parsed = json.loads(payload_text)
    except Exception as exc:
        flash(_("Invalid JSON: %(error)s", error=str(exc)), "warning")
        return redirect(url_for("checkpoints.import_checkpoints_json"))

    items = None
    if isinstance(parsed, list):
        items = parsed
    elif isinstance(parsed, dict) and "items" in parsed:
        items = parsed.get("items")
    elif isinstance(parsed, dict) and "cps" in parsed:
        cps = parsed.get("cps") or []
        converted = []
        for idx, cp in enumerate(cps):
            if not isinstance(cp, dict):
                continue
            e = cp.get("e")
            n = cp.get("n")
            if e is None or n is None:
                continue
            name = (cp.get("name") or "").strip() or f"CP-{idx + 1}"
            converted.append({"name": name, "easting": e, "northing": n})
        items = converted

    if not items:
        flash(
            _("JSON must be an array of checkpoints, an object with 'items', or an object with 'cps' list."),
            "warning",
        )
        return redirect(url_for("checkpoints.import_checkpoints_json"))

    resp, payload = api_json("POST", "/api/checkpoints/import", json={"items": items})
    if resp.status_code != 200:
        detail = payload.get("detail") or payload.get("error") or _("Import failed.")
        flash(_("Import failed: %(detail)s", detail=detail), "warning")
        return redirect(url_for("checkpoints.import_checkpoints_json"))

    summary = payload.get("summary") or {}
    errors = payload.get("errors") or []
    flash(
        _(
            "Imported checkpoints: created %(created)s, updated %(updated)s, skipped %(skipped)s.",
            created=summary.get("created", 0),
            updated=summary.get("updated", 0),
            skipped=summary.get("skipped", 0),
        ),
        "success",
    )
    if errors:
        flash(_("%(count)s row(s) reported errors; see logs.", count=len(errors)), "warning")
        current_app.logger.warning("Checkpoint import errors: %s", errors)

    return redirect(url_for("checkpoints.list_checkpoints"))
