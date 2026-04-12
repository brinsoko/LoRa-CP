# app/blueprints/firmware/routes.py
from __future__ import annotations

import io
import os
import uuid

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)
from flask_babel import gettext as _
from flask_login import current_user
from werkzeug.utils import secure_filename

from app.extensions import db
from app.models import FirmwareFile, LoRaDevice
from app.utils.audit import record_audit_event
from app.utils.competition import get_current_competition_id
from app.utils.perms import roles_required

firmware_bp = Blueprint("firmware", __name__, template_folder="../../templates")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _require_comp():
    comp_id = get_current_competition_id()
    if not comp_id:
        flash(_("Select a competition first."), "warning")
        return None, redirect(url_for("main.select_competition"))
    return comp_id, None


def _firmware_dir(comp_id: int) -> str:
    base = os.path.join(current_app.instance_path, "firmware", str(comp_id))
    os.makedirs(base, exist_ok=True)
    return base


def _mask(value: str) -> str:
    """Show first 4 chars then *** to avoid leaking secrets in JSON responses."""
    if not value:
        return "***"
    return value[:4] + "***"


# ---------------------------------------------------------------------------
# Firmware management
# ---------------------------------------------------------------------------

@firmware_bp.route("/", methods=["GET"])
@roles_required("admin")
def firmware_list():
    comp_id, redir = _require_comp()
    if redir:
        return redir
    files = (
        FirmwareFile.query
        .filter_by(competition_id=comp_id)
        .order_by(FirmwareFile.device_type.asc(), FirmwareFile.uploaded_at.desc())
        .all()
    )
    return render_template("firmware_list.html", files=files)


@firmware_bp.route("/upload", methods=["GET", "POST"])
@roles_required("admin")
def firmware_upload():
    comp_id, redir = _require_comp()
    if redir:
        return redir

    if request.method == "POST":
        upload = request.files.get("file")
        name = (request.form.get("name") or "").strip()
        device_type = (request.form.get("device_type") or "").strip()
        version = (request.form.get("version") or "").strip() or None
        nvs_offset_raw = request.form.get("nvs_offset") or "0x9000"
        nvs_size_raw = request.form.get("nvs_size") or "0x5000"
        app_offset_raw = request.form.get("app_offset") or "0x10000"

        errors = []
        if not upload or not upload.filename:
            errors.append(_("No file selected."))
        elif not upload.filename.lower().endswith(".bin"):
            errors.append(_("Only .bin files are allowed."))
        if not name:
            errors.append(_("Name is required."))
        if device_type not in ("receiver", "sender"):
            errors.append(_("Device type must be 'receiver' or 'sender'."))
        try:
            nvs_offset = int(nvs_offset_raw, 0)
            nvs_size = int(nvs_size_raw, 0)
            app_offset = int(app_offset_raw, 0)
        except (ValueError, TypeError):
            errors.append(_("Offsets must be valid integers (e.g. 0x9000 or 36864)."))
            nvs_offset = nvs_size = app_offset = 0

        if nvs_offset >= app_offset:
            errors.append(_("NVS offset must be less than app offset."))
        if nvs_size <= 0 or nvs_size % 4096 != 0:
            errors.append(_("NVS size must be a positive multiple of 4096 (e.g. 0x5000)."))
        if nvs_offset + nvs_size > app_offset:
            errors.append(_("NVS partition must fit before the app partition offset."))

        if errors:
            for e in errors:
                flash(e, "warning")
            return render_template("firmware_upload.html")

        safe_orig = secure_filename(upload.filename)
        stored_filename = f"{uuid.uuid4().hex}_{safe_orig}"
        dest_path = os.path.join(_firmware_dir(comp_id), stored_filename)
        upload.save(dest_path)

        fw = FirmwareFile(
            competition_id=comp_id,
            name=name,
            device_type=device_type,
            version=version,
            filename=stored_filename,
            nvs_offset=nvs_offset,
            nvs_size=nvs_size,
            app_offset=app_offset,
            uploaded_by_user_id=current_user.id,
        )
        db.session.add(fw)
        db.session.flush()
        record_audit_event(
            competition_id=comp_id,
            event_type="firmware_uploaded",
            entity_type="firmware_file",
            entity_id=fw.id,
            actor_user=current_user,
            summary=f"Firmware '{fw.name}' ({fw.device_type}) uploaded.",
            details={"name": fw.name, "device_type": fw.device_type, "version": fw.version},
        )
        db.session.commit()
        flash(_("Firmware '%(name)s' uploaded.", name=name), "success")
        return redirect(url_for("firmware.firmware_list"))

    return render_template("firmware_upload.html")


@firmware_bp.route("/<int:fw_id>/delete", methods=["POST"])
@roles_required("admin")
def firmware_delete(fw_id: int):
    comp_id, redir = _require_comp()
    if redir:
        return redir
    fw = FirmwareFile.query.filter_by(id=fw_id, competition_id=comp_id).first_or_404()

    disk_path = os.path.join(_firmware_dir(comp_id), fw.filename)
    if os.path.isfile(disk_path):
        os.remove(disk_path)

    record_audit_event(
        competition_id=comp_id,
        event_type="firmware_deleted",
        entity_type="firmware_file",
        entity_id=fw.id,
        actor_user=current_user,
        summary=f"Firmware '{fw.name}' deleted.",
        details={"name": fw.name, "device_type": fw.device_type},
    )
    db.session.delete(fw)
    db.session.commit()
    flash(_("Firmware '%(name)s' deleted.", name=fw.name), "success")
    return redirect(url_for("firmware.firmware_list"))


@firmware_bp.route("/<int:fw_id>/download")
@roles_required("admin")
def firmware_download(fw_id: int):
    comp_id, redir = _require_comp()
    if redir:
        return redir
    fw = FirmwareFile.query.filter_by(id=fw_id, competition_id=comp_id).first_or_404()
    disk_path = os.path.join(_firmware_dir(comp_id), fw.filename)
    if not os.path.isfile(disk_path):
        abort(404)
    return send_file(
        disk_path,
        mimetype="application/octet-stream",
        as_attachment=False,
        download_name=fw.filename,
    )


# ---------------------------------------------------------------------------
# Flash UI
# ---------------------------------------------------------------------------

@firmware_bp.route("/flash", methods=["GET"])
@roles_required("admin")
def firmware_flash():
    comp_id, redir = _require_comp()
    if redir:
        return redir
    devices = (
        LoRaDevice.query
        .filter_by(competition_id=comp_id, active=True)
        .order_by(LoRaDevice.dev_num.asc())
        .all()
    )
    firmware_files = (
        FirmwareFile.query
        .filter_by(competition_id=comp_id)
        .order_by(FirmwareFile.device_type.asc(), FirmwareFile.name.asc())
        .all()
    )
    return render_template(
        "firmware_flash.html",
        devices=devices,
        firmware_files=firmware_files,
    )


# ---------------------------------------------------------------------------
# JSON / binary API (called by browser JS)
# ---------------------------------------------------------------------------

@firmware_bp.route("/api/config/<int:device_id>/<int:fw_id>")
@roles_required("admin")
def firmware_config_preview(device_id: int, fw_id: int):
    comp_id, _ = _require_comp()
    if not comp_id:
        return jsonify({"error": "no_competition", "code": 400}), 400

    device = LoRaDevice.query.filter_by(id=device_id, competition_id=comp_id).first_or_404()
    fw = FirmwareFile.query.filter_by(id=fw_id, competition_id=comp_id).first_or_404()

    cfg = current_app.config
    card_secret = cfg.get("DEVICE_CARD_SECRET") or ""
    webhook_secret = cfg.get("LORA_WEBHOOK_SECRET") or ""
    hmac_len = cfg.get("DEVICE_CARD_HMAC_LEN", 12)

    if card_secret and card_secret == cfg.get("SECRET_KEY"):
        current_app.logger.warning(
            "DEVICE_CARD_SECRET is falling back to SECRET_KEY — set a dedicated value in .env"
        )

    return jsonify({
        "nvs_namespace": "config",
        "entries": {
            "dev_num":        device.dev_num,
            "competition_id": comp_id,
            "card_secret":    _mask(card_secret),
            "hmac_len":       hmac_len,
            "webhook_secret": _mask(webhook_secret),
        },
        "flash_plan": [
            {
                "label":  "NVS partition",
                "offset": hex(fw.nvs_offset),
                "source": f"generated on server ({hex(fw.nvs_size)})",
            },
            {
                "label":  "Application firmware",
                "offset": hex(fw.app_offset),
                "source": f"{fw.name}" + (f" v{fw.version}" if fw.version else ""),
            },
        ],
    })


@firmware_bp.route("/api/nvs/<int:device_id>/<int:fw_id>")
@roles_required("admin")
def firmware_nvs_binary(device_id: int, fw_id: int):
    comp_id, _ = _require_comp()
    if not comp_id:
        return jsonify({"error": "no_competition", "code": 400}), 400

    device = LoRaDevice.query.filter_by(id=device_id, competition_id=comp_id).first_or_404()
    fw = FirmwareFile.query.filter_by(id=fw_id, competition_id=comp_id).first_or_404()

    cfg = current_app.config
    card_secret = cfg.get("DEVICE_CARD_SECRET") or ""
    webhook_secret = cfg.get("LORA_WEBHOOK_SECRET") or ""
    hmac_len = int(cfg.get("DEVICE_CARD_HMAC_LEN", 12))

    partition_size = int(fw.nvs_size or 0)
    if partition_size <= 0 or partition_size % 4096 != 0:
        abort(400, f"Invalid NVS partition size: {partition_size:#x}")
    if fw.nvs_offset + partition_size > fw.app_offset:
        abort(400, f"NVS partition overruns app offset: {fw.nvs_offset + partition_size:#x} > {fw.app_offset:#x}")

    from app.utils.nvs_gen import generate_nvs_partition
    try:
        nvs_bytes = generate_nvs_partition(
            dev_num=device.dev_num,
            competition_id=comp_id,
            card_secret=card_secret,
            hmac_len=hmac_len,
            webhook_secret=webhook_secret,
            partition_size=partition_size,
        )
    except Exception as exc:
        current_app.logger.exception("NVS generation failed: %s", exc)
        return jsonify({"error": "nvs_generation_failed", "detail": str(exc)}), 500

    return send_file(
        io.BytesIO(nvs_bytes),
        mimetype="application/octet-stream",
        as_attachment=False,
        download_name=f"nvs_dev{device.dev_num}.bin",
    )
