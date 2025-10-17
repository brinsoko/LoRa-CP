# app/blueprints/checkpoints/routes.py
from __future__ import annotations

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask import current_app as cap
from sqlalchemy import func

from app.extensions import db
from app.models import Checkpoint
from app.utils.perms import roles_required

import json

checkpoints_bp = Blueprint("checkpoints", __name__, template_folder="../../templates")


# -----------------------------
# List / CRUD
# -----------------------------
@checkpoints_bp.route("/", methods=["GET"])
@roles_required("judge", "admin")
def list_checkpoints():
    cap.logger.debug("[checkpoints] LIST")
    checkpoints = Checkpoint.query.order_by(Checkpoint.name.asc()).all()
    cap.logger.debug("[checkpoints] LIST -> count=%d", len(checkpoints))
    return render_template("checkpoints_list.html", checkpoints=checkpoints)


@checkpoints_bp.route("/add", methods=["GET", "POST"])
@roles_required("judge", "admin")
def add_checkpoint():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        location = (request.form.get("location") or "").strip()
        description = (request.form.get("description") or "").strip()
        easting = request.form.get("easting", type=float)
        northing = request.form.get("northing", type=float)

        cap.logger.debug("[checkpoints] ADD POST name=%r e=%r n=%r", name, easting, northing)

        if not name:
            flash("Name is required.", "warning")
            return render_template("add_checkpoint.html")

        cp = Checkpoint(
            name=name,
            location=location or None,
            description=description or None,
            easting=easting,
            northing=northing,
        )
        db.session.add(cp)
        db.session.commit()
        flash("Checkpoint added.", "success")
        cap.logger.debug("[checkpoints] ADD -> created id=%s", cp.id)
        return redirect(url_for("checkpoints.list_checkpoints"))

    cap.logger.debug("[checkpoints] ADD GET")
    return render_template("add_checkpoint.html")


@checkpoints_bp.route("/<int:cp_id>/edit", methods=["GET", "POST"])
@roles_required("judge", "admin")
def edit_checkpoint(cp_id: int):
    cp = Checkpoint.query.get_or_404(cp_id)
    if request.method == "POST":
        before = (cp.name, cp.easting, cp.northing, cp.location, cp.description)
        cp.name = (request.form.get("name") or "").strip()
        cp.location = (request.form.get("location") or "").strip() or None
        cp.description = (request.form.get("description") or "").strip() or None
        cp.easting = request.form.get("easting", type=float)
        cp.northing = request.form.get("northing", type=float)

        cap.logger.debug(
            "[checkpoints] EDIT POST id=%s before=%r after=%r",
            cp_id, before, (cp.name, cp.easting, cp.northing, cp.location, cp.description)
        )

        if not cp.name:
            flash("Name is required.", "warning")
            return render_template("checkpoint_edit.html", cp=cp)

        db.session.commit()
        flash("Checkpoint updated.", "success")
        return redirect(url_for("checkpoints.list_checkpoints"))

    cap.logger.debug("[checkpoints] EDIT GET id=%s", cp_id)
    return render_template("checkpoint_edit.html", cp=cp)


@checkpoints_bp.route("/<int:cp_id>/delete", methods=["POST"])
@roles_required("admin")
def delete_checkpoint(cp_id: int):
    cp = Checkpoint.query.get_or_404(cp_id)
    cap.logger.debug("[checkpoints] DELETE id=%s", cp_id)

    if cp.checkins:
        flash("Cannot delete checkpoint with existing check-ins.", "warning")
        cap.logger.debug("[checkpoints] DELETE blocked: has checkins")
        return redirect(url_for("checkpoints.list_checkpoints"))

    db.session.delete(cp)
    db.session.commit()
    flash("Checkpoint deleted.", "success")
    cap.logger.debug("[checkpoints] DELETE ok")
    return redirect(url_for("checkpoints.list_checkpoints"))


# -----------------------------
# JSON Import (upload → preview → confirm)
# -----------------------------
@checkpoints_bp.route("/import_json", methods=["GET", "POST"])
@roles_required("judge", "admin")
def import_checkpoints_json():
    """
    Step 1 (GET): show upload form
    Step 2 (POST action=preview): parse JSON, normalize, diff, render preview
    Step 3 (POST action=confirm): apply upsert based on normalized payload from hidden field
    """
    if request.method == "GET":
        cap.logger.debug("[checkpoints/import_json] GET -> render upload form")
        return render_template("checkpoints_import.html")

    action = request.form.get("action")
    cap.logger.debug("[checkpoints/import_json] POST action=%r", action)

    # ---------- Step 3: CONFIRM ----------
    if action == "confirm":
        payload_text = request.form.get("payload", "")
        cap.logger.debug("[checkpoints/import_json] CONFIRM payload bytes=%d", len(payload_text.encode("utf-8")))

        try:
            normalized_items = json.loads(payload_text)
        except Exception:
            cap.logger.exception("[checkpoints/import_json] CONFIRM payload parse failed")
            flash("Could not parse confirmation payload. Please try again.", "warning")
            return redirect(url_for("checkpoints.import_checkpoints_json"))

        created = updated = skipped = 0

        for it in normalized_items:
            name = (it.get("name") or "").strip()
            if not name:
                skipped += 1
                continue

            easting = it.get("easting", None)
            northing = it.get("northing", None)
            desc = it.get("description") or None
            loc = it.get("location") or None
            action_kind = it.get("action")

            cp = Checkpoint.query.filter(func.lower(Checkpoint.name) == name.lower()).first()
            if cp:
                if action_kind == "update":
                    cp.easting = easting
                    cp.northing = northing
                    cp.description = desc
                    cp.location = loc
                    updated += 1
                else:
                    skipped += 1
            else:
                if action_kind in ("create", "update"):  # allow update->create if missing
                    db.session.add(Checkpoint(
                        name=name, easting=easting, northing=northing,
                        description=desc, location=loc
                    ))
                    created += 1
                else:
                    skipped += 1

        db.session.commit()
        cap.logger.debug(
            "[checkpoints/import_json] CONFIRM -> created=%d updated=%d skipped=%d",
            created, updated, skipped
        )
        flash(f"Import complete: {created} created, {updated} updated, {skipped} skipped.", "success")
        return redirect(url_for("checkpoints.list_checkpoints"))

    # ---------- Step 2: PREVIEW ----------
    # Default branch if not confirm
    file = request.files.get("file")
    cap.logger.debug("[checkpoints/import_json] PREVIEW file present=%s", bool(file))
    if not file:
        flash("Please choose a JSON file.", "warning")
        cap.logger.debug("[checkpoints/import_json] PREVIEW -> no file, render upload")
        return render_template("checkpoints_import.html")

    try:
        raw = file.stream.read().decode("utf-8", errors="ignore")
        payload = json.loads(raw)
        cap.logger.debug("[checkpoints/import_json] PREVIEW raw_len=%d type=%s",
                         len(raw), type(payload).__name__)
    except Exception as e:
        cap.logger.exception("[checkpoints/import_json] PREVIEW JSON parse failed")
        flash(f"Could not parse JSON: {e}", "warning")
        return render_template("checkpoints_import.html")

    # normalize supported shapes:
    # 1) [ {...}, ... ]
    # 2) { "checkpoints": [ ... ] }
    # 3) { "cps": [ ... ] }  # your kt_Cerknica.json format
    if isinstance(payload, list):
        items = payload; source = "array"
    elif isinstance(payload, dict):
        if isinstance(payload.get("checkpoints"), list):
            items = payload["checkpoints"]; source = "checkpoints"
        elif isinstance(payload.get("cps"), list):
            items = payload["cps"]; source = "cps"
        else:
            items = None; source = "unknown-dict"
    else:
        items = None; source = "unknown"

    if not isinstance(items, list):
        cap.logger.debug("[checkpoints/import_json] PREVIEW invalid shape source=%s", source)
        flash("JSON must be an array or an object with 'checkpoints' or 'cps' array.", "warning")
        return render_template("checkpoints_import.html")

    if len(items) == 0:
        cap.logger.debug("[checkpoints/import_json] PREVIEW zero items")
        flash("JSON contained zero checkpoints.", "warning")
        return render_template("checkpoints_import.html")

    # Build set of existing names to avoid collisions for generated names
    existing_names = {
        name for (name,) in db.session.query(Checkpoint.name).filter(Checkpoint.name.isnot(None)).all()
    }

    def _to_float(v):
        if v in (None, ""):
            return None
        try:
            return float(v)
        except Exception:
            return None

    def _next_name(counter=[1]):
        # generate CP-1, CP-2, ... avoiding collisions
        while True:
            candidate = f"CP-{counter[0]}"
            counter[0] += 1
            if candidate not in existing_names:
                existing_names.add(candidate)
                return candidate

    preview_rows = []
    create_n = update_n = skip_n = 0

    for row in items:
        if not isinstance(row, dict):
            skip_n += 1
            continue

        raw_name = (row.get("name") or row.get("label") or "").strip()
        name = raw_name if raw_name else _next_name()

        easting = _to_float(row.get("e"))
        northing = _to_float(row.get("n"))
        desc = (row.get("description") or "").strip() or None
        loc = (row.get("location") or "").strip() or None

        current = Checkpoint.query.filter(func.lower(Checkpoint.name) == name.lower()).first()
        if current:
            changed = (
                (current.easting or None) != easting or
                (current.northing or None) != northing or
                (current.description or None) != desc or
                (current.location or None) != loc
            )
            action = "update" if changed else "skip"
            if changed: update_n += 1
            else:       skip_n += 1
        else:
            action = "create"
            create_n += 1

        preview_rows.append({
            "name": name,
            "easting": easting,
            "northing": northing,
            "description": desc,
            "location": loc,
            "action": action,
        })

    MAX_PREVIEW = 5000
    if len(preview_rows) > MAX_PREVIEW:
        cap.logger.debug("[checkpoints/import_json] PREVIEW truncated %d -> %d", len(preview_rows), MAX_PREVIEW)
        flash(f"Preview truncated to first {MAX_PREVIEW} rows (total: {len(preview_rows)}).", "warning")
        preview_rows = preview_rows[:MAX_PREVIEW]

    payload_text = json.dumps(preview_rows, ensure_ascii=False)
    cap.logger.debug(
        "[checkpoints/import_json] PREVIEW -> create=%d update=%d skip=%d rows_out=%d",
        create_n, update_n, skip_n, len(preview_rows)
    )

    return render_template(
        "checkpoints_import_preview.html",
        rows=preview_rows,
        count_create=create_n,
        count_update=update_n,
        count_skip=skip_n,
        payload_text=payload_text
    )