from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from app.extensions import db
from app.models import RFIDCard, Team
from app.utils.perms import roles_required
from app.utils.serial_helpers import normalize_uid, read_uid_once
from config import Config
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import joinedload
import io, csv

rfid_bp = Blueprint('rfid', __name__, template_folder="../../templates")

# config access
BAUD = Config.SERIAL_BAUDRATE
HINT = Config.SERIAL_HINT
TIMEOUT = Config.SERIAL_TIMEOUT


# -------------------------------
# Helpers
# -------------------------------
def parse_positive_int_or_none(value: str | None):
    if not value:
        return None
    try:
        n = int(value)
        return n if n > 0 else None
    except ValueError:
        return None


# -------------------------------
# List all RFID mappings
# -------------------------------
@rfid_bp.route("/", methods=["GET"])
def list_rfid():
    cards = RFIDCard.query.order_by(RFIDCard.number.asc().nulls_last(), RFIDCard.uid.asc()).all()
    teams = Team.query.order_by(Team.name.asc()).all()
    return render_template("rfid_list.html", cards=cards, teams=teams)


# -------------------------------
# Add a new RFID mapping
# -------------------------------
@rfid_bp.route("/add", methods=["GET", "POST"])
@roles_required("judge", "admin")
def add_rfid():
    teams = Team.query.order_by(Team.name.asc()).all()

    if request.method == "POST":
        uid_raw = request.form.get("uid", "").strip()
        uid = normalize_uid(uid_raw)
        team_id = request.form.get("team_id", type=int)
        number = request.form.get("number", type=int)

        if not uid:
            flash("UID is required.", "warning")
            return render_template("rfid_add.html", teams=teams)

        if not Team.query.get(team_id):
            flash("Invalid team.", "warning")
            return render_template("rfid_add.html", teams=teams)

        if RFIDCard.query.filter_by(team_id=team_id).first():
            flash("This team already has an RFID card assigned.", "warning")
            return render_template("rfid_add.html", teams=teams)

        if request.form.get("number") and number is None:
            flash("Card number must be a positive integer (or leave blank).", "warning")
            return render_template("rfid_add.html", teams=teams)

        card = RFIDCard(uid, team_id, number)
        db.session.add(card)

        try:
            db.session.commit()
            flash("RFID mapping created.", "success")
            return redirect(url_for("rfid.list_rfid"))
        except IntegrityError:
            db.session.rollback()
            flash("UID already exists. Use Edit to reassign or change UID.", "warning")
            return render_template("rfid_add.html", teams=teams)

    return render_template("rfid_add.html", teams=teams)


# -------------------------------
# Edit an existing RFID mapping
# -------------------------------
@rfid_bp.route("/<int:card_id>/edit", methods=["GET", "POST"])
@roles_required("judge", "admin")
def edit_rfid(card_id):
    card = RFIDCard.query.get_or_404(card_id)
    teams = Team.query.order_by(Team.name.asc()).all()

    if request.method == "POST":
        new_uid = normalize_uid(request.form.get("uid", "").strip())
        new_team_id = request.form.get("team_id", type=int)
        new_number = request.form.get("number", type=int)

        if not new_uid:
            flash("UID is required.", "warning")
            return render_template("rfid_edit.html", card=card, teams=teams)

        if not Team.query.get(new_team_id):
            flash("Invalid team.", "warning")
            return render_template("rfid_edit.html", card=card, teams=teams)

        if request.form.get("number") and new_number is None:
            flash("Card number must be a positive integer (or leave blank).", "warning")
            return render_template("rfid_edit.html", card=card, teams=teams)

        # Prevent multiple cards for same team
        existing_for_team = RFIDCard.query.filter(
            RFIDCard.team_id == new_team_id, RFIDCard.id != card.id
        ).first()
        if existing_for_team:
            flash("That team already has an RFID card assigned.", "warning")
            return render_template("rfid_edit.html", card=card, teams=teams)

        card.uid = new_uid
        card.team_id = new_team_id
        card.number = new_number

        try:
            db.session.commit()
            flash("RFID mapping updated.", "success")
            return redirect(url_for("rfid.list_rfid"))
        except IntegrityError:
            db.session.rollback()
            flash("UID already exists. Choose a different UID.", "warning")
            return render_template("rfid_edit.html", card=card, teams=teams)

    return render_template("rfid_edit.html", card=card, teams=teams)


# -------------------------------
# Delete RFID mapping
# -------------------------------
@rfid_bp.route("/<int:card_id>/delete", methods=["POST"])
@roles_required("admin")
def delete_rfid(card_id):
    card = RFIDCard.query.get_or_404(card_id)
    db.session.delete(card)
    db.session.commit()
    flash("RFID mapping deleted.", "success")
    return redirect(url_for("rfid.list_rfid"))


# -------------------------------
# Scan RFID via serial
# -------------------------------
@rfid_bp.route("/scan_once", methods=["POST"])
@roles_required("judge", "admin")
def rfid_scan_once():
    uid = read_uid_once(BAUD, HINT, TIMEOUT)
    if not uid:
        return jsonify({"ok": False, "error": "No UID read (check device, cable, or increase timeout)."}), 200
    return jsonify({"ok": True, "uid": uid}), 200


# -------------------------------
# CSV upload (supports "number" column)
# -------------------------------
@rfid_bp.route("/upload_csv", methods=["GET"])
@roles_required("admin")
def rfid_upload_csv_form():
    return render_template("rfid_upload_csv.html")


@rfid_bp.route("/upload_csv", methods=["POST"])
@roles_required("admin")
def rfid_upload_csv():
    file = request.files.get('file')
    if not file:
        flash("Please choose a CSV file.", "warning")
        return redirect(url_for("rfid.rfid_upload_csv_form"))

    try:
        stream = io.StringIO(file.stream.read().decode('utf-8', errors='ignore'))
        reader = csv.DictReader(stream)
    except Exception:
        flash("Could not read CSV. Ensure it has a header row.", "warning")
        return redirect(url_for("rfid.rfid_upload_csv_form"))

    created = updated = skipped = 0
    errors = []

    for i, row in enumerate(reader, start=2):
        raw_uid = (row.get('uid') or '').strip()
        uid = normalize_uid(raw_uid)
        if not uid:
            skipped += 1
            errors.append(f"Line {i}: missing uid")
            continue

        team = None
        team_id_val = (row.get('team_id') or '').strip()
        team_name = (row.get('team_name') or '').strip()
        team_number = (row.get('team_number') or '').strip()

        if team_id_val:
            try:
                tid = int(team_id_val)
                team = Team.query.get(tid)
            except Exception:
                pass
        else:
            if team_name:
                q = Team.query.filter(Team.name == team_name)
                if team_number:
                    try:
                        q = q.filter(Team.number == int(team_number))
                    except ValueError:
                        pass
                team = q.first()

        if not team:
            skipped += 1
            errors.append(f"Line {i}: could not resolve team")
            continue

        number = parse_positive_int_or_none((row.get('number') or '').strip())

        card = RFIDCard.query.filter_by(uid=uid).first()
        if card:
            changed = False
            if card.team_id != team.id:
                card.team_id = team.id
                changed = True
            if card.number != number:
                card.number = number
                changed = True
            if changed:
                try:
                    db.session.commit()
                    updated += 1
                except IntegrityError:
                    db.session.rollback()
                    skipped += 1
                    errors.append(f"Line {i}: DB error updating {uid}")
            else:
                skipped += 1
        else:
            try:
                db.session.add(RFIDCard(uid=uid, team_id=team.id, number=number))
                db.session.commit()
                created += 1
            except IntegrityError:
                db.session.rollback()
                skipped += 1
                errors.append(f"Line {i}: UID exists {uid}")

    msg = f"CSV processed: {created} created, {updated} updated, {skipped} skipped."
    if errors:
        preview = "\n".join(errors[:8])
        flash(msg + " Some issues:\n" + preview + ("" if len(errors) <= 8 else "\n…"), "warning")
    else:
        flash(msg, "success")

    return redirect(url_for("rfid.list_rfid"))

@rfid_bp.route("/public", methods=["GET"])
def public_mappings():
    cards = (
        RFIDCard.query
        .options(joinedload(RFIDCard.team))
        .order_by(RFIDCard.number.asc().nulls_last(), RFIDCard.uid.asc())
        .all()
    )
    return render_template("rfid_list.html", cards=cards, read_only=True)

# PUBLIC JSON (optional)
@rfid_bp.route("/public.json", methods=["GET"])
def public_mappings_json():
    cards = (
        RFIDCard.query
        .options(joinedload(RFIDCard.team))
        .order_by(RFIDCard.number.asc().nulls_last(), RFIDCard.uid.asc())
        .all()
    )
    return {
        "cards": [
            {
                "id": c.id,
                "uid": c.uid,
                "number": c.number,
                "team": {
                    "id": c.team.id if c.team else None,
                    "name": c.team.name if c.team else None,
                    "number": c.team.number if c.team else None,
                },
            }
            for c in cards
        ]
    }, 200