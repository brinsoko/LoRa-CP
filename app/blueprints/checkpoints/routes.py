# app/blueprints/checkpoints/routes.py
from flask import Blueprint, render_template, request, redirect, url_for, flash
from app.extensions import db
from app.models import Checkpoint
from app.utils.perms import roles_required

checkpoints_bp = Blueprint("checkpoints", __name__, template_folder="../../templates")

@checkpoints_bp.route("/", methods=["GET"])
@roles_required("judge", "admin")
def list_checkpoints():
    checkpoints = Checkpoint.query.order_by(Checkpoint.name.asc()).all()
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
        return redirect(url_for("checkpoints.list_checkpoints"))

    return render_template("add_checkpoint.html")

@checkpoints_bp.route("/<int:cp_id>/edit", methods=["GET", "POST"])
@roles_required("judge", "admin")
def edit_checkpoint(cp_id):
    cp = Checkpoint.query.get_or_404(cp_id)
    if request.method == "POST":
        cp.name = (request.form.get("name") or "").strip()
        cp.location = (request.form.get("location") or "").strip() or None
        cp.description = (request.form.get("description") or "").strip() or None
        cp.easting = request.form.get("easting", type=float)
        cp.northing = request.form.get("northing", type=float)

        if not cp.name:
            flash("Name is required.", "warning")
            return render_template("checkpoint_edit.html", cp=cp)

        db.session.commit()
        flash("Checkpoint updated.", "success")
        return redirect(url_for("checkpoints.list_checkpoints"))

    return render_template("checkpoint_edit.html", cp=cp)

@checkpoints_bp.route("/<int:cp_id>/delete", methods=["POST"])
@roles_required("admin")
def delete_checkpoint(cp_id):
    cp = Checkpoint.query.get_or_404(cp_id)
    if cp.checkins:
        flash("Cannot delete checkpoint with existing check-ins.", "warning")
        return redirect(url_for("checkpoints.list_checkpoints"))
    db.session.delete(cp)
    db.session.commit()
    flash("Checkpoint deleted.", "success")
    return redirect(url_for("checkpoints.list_checkpoints"))

# --- JSON import (upload) ---
@checkpoints_bp.route("/import_json", methods=["GET", "POST"])
@roles_required("judge", "admin")
def import_checkpoints_json():
    """
    Accepts a JSON file with either:
      A) an array of checkpoints:
         [
           {"name": "CP-1", "e": 412345, "n": 987654, "description": "...", "location": "..." },
           ...
         ]
      B) or an object with a "checkpoints" key:
         { "checkpoints": [ {...}, {...} ] }

    Fields:
      - name (required, unique)
      - e (optional) -> stored as easting (float)
      - n (optional) -> stored as northing (float)
      - description (optional)
      - location (optional)
    Upsert by 'name': if a checkpoint with same name exists, it’s updated; otherwise created.
    """
    if request.method == "GET":
        return render_template("checkpoints_import.html")

    file = request.files.get("file")
    if not file:
        flash("Please choose a JSON file.", "warning")
        return render_template("checkpoints_import.html")

    import json
    try:
        payload = json.loads(file.stream.read().decode("utf-8", errors="ignore"))
    except Exception as e:
        flash(f"Could not parse JSON: {e}", "warning")
        return render_template("checkpoints_import.html")

    # Normalize to a list of items
    items = payload.get("checkpoints") if isinstance(payload, dict) else payload
    if not isinstance(items, list):
        flash("JSON must be an array of checkpoints or an object with 'checkpoints' array.", "warning")
        return render_template("checkpoints_import.html")

    from app.models import Checkpoint  # local import to avoid circulars

    created = updated = skipped = 0
    for i, row in enumerate(items, start=1):
        if not isinstance(row, dict):
            skipped += 1
            continue

        name = (str(row.get("name") or "")).strip()
        if not name:
            skipped += 1
            continue

        # Optional fields
        e = row.get("e")
        n = row.get("n")
        desc = row.get("description")
        loc = row.get("location")

        # Convert e/n to floats when possible
        def _to_float(v):
            if v is None or v == "":
                return None
            try:
                return float(v)
            except Exception:
                return None

        easting = _to_float(e)
        northing = _to_float(n)

        # Upsert by name
        cp = Checkpoint.query.filter_by(name=name).first()
        if cp:
            changed = False
            if cp.easting != easting: cp.easting, changed = easting, True
            if cp.northing != northing: cp.northing, changed = northing, True
            new_desc = (desc or None)
            new_loc = (loc or None)
            if cp.description != new_desc: cp.description, changed = new_desc, True
            if cp.location != new_loc: cp.location, changed = new_loc, True

            if changed:
                updated += 1
        else:
            cp = Checkpoint(
                name=name,
                easting=easting,
                northing=northing,
                description=(desc or None),
                location=(loc or None),
            )
            db.session.add(cp)
            created += 1

    db.session.commit()
    flash(f"Import done: {created} created, {updated} updated, {skipped} skipped.", "success")
    return redirect(url_for("checkpoints.list_checkpoints"))