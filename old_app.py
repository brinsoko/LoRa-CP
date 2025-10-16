from flask import Flask, render_template, request, redirect, url_for, Response, flash, abort, session
from sqlalchemy.orm import joinedload
from datetime import datetime, timedelta
from sqlalchemy.exc import IntegrityError
import io
import csv
import os
import serial
from serial.tools import list_ports
from models import db, Team, Checkpoint, Checkin, RFIDCard, User
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
from functools import wraps






# --- Serial reader config ---
SERIAL_BAUDRATE = int(os.environ.get("SERIAL_BAUDRATE", "9600"))
SERIAL_HINT = os.environ.get("SERIAL_HINT", "")  # optional substring to match the port
SERIAL_TIMEOUT = float(os.environ.get("SERIAL_TIMEOUT", "8.0"))  # seconds



app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///database.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret")  # set early
db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "login"         # redirect here when @login_required fails
login_manager.login_message_category = "warning"

@login_manager.user_loader
def load_user(user_id: str):
    return User.query.get(int(user_id))

def normalize_uid(uid: str) -> str:
    if not uid: 
        return ""
    return uid.replace(":", "").replace("-", "").strip().upper()

def roles_required(*roles: str):
    """Use as @roles_required('judge', 'admin') to restrict routes."""
    def wrapper(view):
        @wraps(view)
        def inner(*args, **kwargs):
            if not current_user.is_authenticated:
                return login_manager.unauthorized()
            if current_user.role not in roles:
                # 403 forbidden if logged in but wrong role
                abort(403)
            return view(*args, **kwargs)
        return inner
    return wrapper

from flask_login import login_required, current_user
from werkzeug.security import check_password_hash  # only if you need it elsewhere

def _validate_new_password(username: str, pw1: str, pw2: str) -> str | None:
    """Return an error message string if invalid; None if OK."""
    if not pw1 or not pw2:
        return "Please fill in all fields."
    if pw1 != pw2:
        return "New passwords do not match."
    if len(pw1) < 8:
        return "New password must be at least 8 characters."
    if username.lower() in pw1.lower():
        return "Password should not contain your username."
    return None

@app.route("/change_password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        current_pw = request.form.get("current_password") or ""
        new_pw = request.form.get("new_password") or ""
        new_pw2 = request.form.get("confirm_password") or ""

        # Verify current password
        if not current_user.check_password(current_pw):
            flash("Current password is incorrect.", "warning")
            return render_template("change_password.html")

        # Validate new password
        err = _validate_new_password(current_user.username, new_pw, new_pw2)
        if err:
            flash(err, "warning")
            return render_template("change_password.html")

        # Save
        current_user.set_password(new_pw)
        db.session.commit()

        # (Optional) re-log the user to refresh session data
        # from flask_login import login_user
        # login_user(current_user)

        flash("Password changed successfully.", "success")
        return redirect(url_for("index"))

    return render_template("change_password.html")


def find_serial_port(hint: str = "") -> str | None:
    """
    Try to find a USB serial device on macOS/Linux.
    If 'hint' provided, prefer matching device (e.g., 'usbserial', 'cp210', 'ch340', 'lora', etc.).
    """
    ports = list(list_ports.comports())
    if not ports:
        return None

    # First: match by hint
    hint_low = hint.lower()
    for p in ports:
        cand = f"{p.device} {p.description} {p.manufacturer}".lower()
        if hint_low and hint_low in cand:
            return p.device

    # Second: common device name heuristics
    for p in ports:
        d = p.device.lower()
        if "usbserial" in d or "usbmodem" in d or "ttyacm" in d or "ttyusb" in d:
            return p.device

    # Fallback: first available
    return ports[0].device

def read_uid_once() -> str | None:
    """
    Open serial, wait for one line, return normalized UID.
    Expect your reader to print a line containing the UID (e.g., '04A3BC192F90').
    """
    port = find_serial_port(SERIAL_HINT)
    if not port:
        return None

    try:
        with serial.Serial(port, SERIAL_BAUDRATE, timeout=SERIAL_TIMEOUT) as ser:
            line = ser.readline().decode('utf-8', errors='ignore').strip()
            if not line:
                return None
            # If lines include extra text, try to extract hex-like token:
            # e.g. "UID: 04:A3:BC:19:2F:90"
            # Simple heuristic: take the longest hex/colon/dash token in the line
            import re
            tokens = re.findall(r'[0-9A-Fa-f:\-]{6,}', line)
            candidate = max(tokens, key=len) if tokens else line
            return normalize_uid(candidate)
    except Exception:
        return None
    

@app.context_processor
def inject_perms():
    def has_role(*roles: str) -> bool:
        return current_user.is_authenticated and current_user.role in roles
    return dict(has_role=has_role)


@app.route('/')
def index():
    return render_template('base.html')


# ----- Helpers for datetime-local parsing/formatting -----
from datetime import datetime
from zoneinfo import ZoneInfo

# Default app timezone (for display)
DEFAULT_TZ = ZoneInfo("Europe/Ljubljana")   # or any other default

def to_datetime_local(dt: datetime, tz: ZoneInfo = DEFAULT_TZ) -> str:
    """Convert UTC datetime → local string for datetime-local input."""
    if not dt:
        return ""
    local_dt = dt.replace(tzinfo=ZoneInfo("UTC")).astimezone(tz)
    return local_dt.strftime("%Y-%m-%dT%H:%M:%S")

def from_datetime_local(s: str, tz_name: str | None = None) -> datetime | None:
    """Parse local datetime string (with chosen tz) → UTC naive datetime."""
    if not s:
        return None
    tz = ZoneInfo(tz_name) if tz_name else DEFAULT_TZ
    try:
        local_dt = datetime.strptime(s, "%Y-%m-%dT%H:%M:%S")
    except ValueError:
        try:
            local_dt = datetime.strptime(s, "%Y-%m-%dT%H:%M")
        except ValueError:
            return None
    aware_local = local_dt.replace(tzinfo=tz)
    utc_dt = aware_local.astimezone(ZoneInfo("UTC"))
    return utc_dt.replace(tzinfo=None)  # store naive UTC in DB


# =====================================================
# ADD TEAM
# =====================================================
@app.route('/add_team', methods=['GET', 'POST'])
def add_team():
    if request.method == 'POST':
        name = request.form['name'].strip()
        number_raw = request.form.get('number', '').strip()
        number = int(number_raw)
        if number is not None and number <= 0:
            flash("Team number must be a positive integer.", "warning")
            return render_template('add_team.html')  # coerce to int/None

        team = Team(name=name, number=number)
        db.session.add(team)
        db.session.commit()

        return render_template('success.html', message=f"Team '{name}' added successfully!")
    return render_template('add_team.html')


# =====================================================
# ADD CHECKPOINT
# =====================================================
@app.route('/add_checkpoint', methods=['GET', 'POST'])
@roles_required('admin')
def add_checkpoint():
    if request.method == 'POST':
        name = request.form['name'].strip()
        location = request.form.get('location', '').strip()
        description = request.form.get('description', '').strip()

        checkpoint = Checkpoint(name=name, location=location, description=description)
        db.session.add(checkpoint)
        db.session.commit()

        return render_template('success.html', message=f"Checkpoint '{name}' added successfully!")
    return render_template('add_checkpoint.html')


# =====================================================
# ADD CHECK-IN
# =====================================================
@app.route('/add_checkin', methods=['GET', 'POST'])
@roles_required('judge', 'admin')
def add_checkin():
    teams = Team.query.order_by(Team.name.asc()).all()
    checkpoints = Checkpoint.query.order_by(Checkpoint.name.asc()).all()

    if request.method == 'POST':
        team_id = request.form.get('team_id', type=int)          # ✅ coerce to int
        checkpoint_id = request.form.get('checkpoint_id', type=int)

        # Optional validation
        if not Team.query.get(team_id):
            flash("Invalid team.", "warning")
            return render_template('add_checkin.html', teams=teams, checkpoints=checkpoints)
        if not Checkpoint.query.get(checkpoint_id):
            flash("Invalid checkpoint.", "warning")
            return render_template('add_checkin.html', teams=teams, checkpoints=checkpoints)

        timestamp = datetime.utcnow()
        checkin = Checkin(team_id=team_id, checkpoint_id=checkpoint_id, timestamp=timestamp)
        db.session.add(checkin)
        db.session.commit()

        return render_template('success.html', message=f"Check-in recorded for Team ID {team_id} at Checkpoint ID {checkpoint_id}")
    return render_template('add_checkin.html', teams=teams, checkpoints=checkpoints)


# ===== FILTER UTILITIES FOR /checkins =====
def _parse_date_range(date_from_str, date_to_str):
    """HTML <input type='date'> returns YYYY-MM-DD. Create inclusive [from, to] range."""
    date_from = None
    date_to = None
    try:
        if date_from_str:
            # start of the day
            date_from = datetime.fromisoformat(date_from_str)
        if date_to_str:
            # end of the day (exclusive by adding 1 day)
            date_to = datetime.fromisoformat(date_to_str) + timedelta(days=1)
    except ValueError:
        pass
    return date_from, date_to


def _filtered_checkins(team_id, checkpoint_id, date_from_str, date_to_str):
    """Return a filtered SQLAlchemy query with eager-loaded relations."""
    q = (Checkin.query
         .options(joinedload(Checkin.team), joinedload(Checkin.checkpoint)))

    if team_id:
        q = q.filter(Checkin.team_id == team_id)
    if checkpoint_id:
        q = q.filter(Checkin.checkpoint_id == checkpoint_id)

    date_from, date_to = _parse_date_range(date_from_str, date_to_str)
    if date_from:
        q = q.filter(Checkin.timestamp >= date_from)
    if date_to:
        q = q.filter(Checkin.timestamp < date_to)

    return q.order_by(Checkin.timestamp.desc())


# KEEP ONLY ONE /checkins ROUTE
@app.route('/checkins')
def view_checkins():
    # dropdown data
    teams = Team.query.order_by(Team.name.asc()).all()
    checkpoints = Checkpoint.query.order_by(Checkpoint.name.asc()).all()

    # current filters (from query string)
    team_id = request.args.get('team_id', type=int)
    checkpoint_id = request.args.get('checkpoint_id', type=int)
    date_from = request.args.get('date_from', type=str)
    date_to = request.args.get('date_to', type=str)

    checkins = _filtered_checkins(team_id, checkpoint_id, date_from, date_to).all()

    return render_template(
        'view_checkins.html',
        checkins=checkins,
        teams=teams,
        checkpoints=checkpoints,
        selected_team_id=team_id,
        selected_checkpoint_id=checkpoint_id,
        selected_date_from=date_from or "",
        selected_date_to=date_to or ""
    )


@app.route('/checkins.csv')
def export_checkins_csv():
    # same filters as the HTML view
    team_id = request.args.get('team_id', type=int)
    checkpoint_id = request.args.get('checkpoint_id', type=int)
    date_from = request.args.get('date_from', type=str)
    date_to = request.args.get('date_to', type=str)

    rows = _filtered_checkins(team_id, checkpoint_id, date_from, date_to).all()

    # build CSV in-memory
    si = io.StringIO()
    writer = csv.writer(si)
    writer.writerow(["timestamp_utc", "team_id", "team_name", "checkpoint_id", "checkpoint_name"])
    for r in rows:
        writer.writerow([
            r.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
            r.team.id if r.team else "",
            r.team.name if r.team else "",
            r.checkpoint.id if r.checkpoint else "",
            r.checkpoint.name if r.checkpoint else "",
        ])
    csv_data = si.getvalue()

    return Response(
        csv_data,
        mimetype='text/csv',
        headers={"Content-Disposition": "attachment; filename=checkins.csv"}
    )


# ============ TEAMS: LIST ============
@app.route('/teams')
def list_teams():
    teams = Team.query.order_by(Team.name.asc()).all()
    return render_template('teams_list.html', teams=teams)

# ============ TEAMS: EDIT ============
@app.route('/teams/<int:team_id>/edit', methods=['GET', 'POST'])
@roles_required('admin')
def edit_team(team_id):
    team = Team.query.get_or_404(team_id)
    if request.method == 'POST':
        team.name = request.form['name'].strip()
        number_raw = request.form.get('number', '').strip()
        team.number = int(number_raw)
        if team.number <= 0:
            flash("Team number must be a positive integer.", "warning")
            return render_template(f'teams_list.html')
        db.session.commit()
        flash(f"Team '{team.name}' updated.", 'success')
        return redirect(url_for('list_teams'))
    return render_template('team_edit.html', team=team)

# ============ TEAMS: DELETE ============
@app.route('/teams/<int:team_id>/delete', methods=['POST'])
@roles_required('admin')
def delete_team(team_id):
    team = Team.query.get_or_404(team_id)
    # Optional: prevent delete if there are checkins
    if team.checkins:
        flash("Cannot delete team with existing check-ins.", 'warning')
        return redirect(url_for('list_teams'))
    db.session.delete(team)
    db.session.commit()
    flash("Team deleted.", 'success')
    return redirect(url_for('list_teams'))


# ============ CHECKPOINTS: LIST ============
@app.route('/checkpoints')
@roles_required('judge', 'admin')
def list_checkpoints():
    checkpoints = Checkpoint.query.order_by(Checkpoint.name.asc()).all()
    return render_template('checkpoints_list.html', checkpoints=checkpoints)

# ============ CHECKPOINTS: EDIT ============
@app.route('/checkpoints/<int:cp_id>/edit', methods=['GET', 'POST'])
@roles_required('admin')
def edit_checkpoint(cp_id):
    cp = Checkpoint.query.get_or_404(cp_id)
    if request.method == 'POST':
        cp.name = request.form['name'].strip()
        cp.location = request.form.get('location', '').strip()
        cp.description = request.form.get('description', '').strip()
        db.session.commit()
        flash(f"Checkpoint '{cp.name}' updated.", 'success')
        return redirect(url_for('list_checkpoints'))
    return render_template('checkpoint_edit.html', cp=cp)

# ============ CHECKPOINTS: DELETE ============
@app.route('/checkpoints/<int:cp_id>/delete', methods=['POST'])
@roles_required('admin')
def delete_checkpoint(cp_id):
    cp = Checkpoint.query.get_or_404(cp_id)
    # Optional: prevent delete if there are checkins
    if cp.checkins:
        flash("Cannot delete checkpoint with existing check-ins.", 'warning')
        return redirect(url_for('list_checkpoints'))
    db.session.delete(cp)
    db.session.commit()
    flash("Checkpoint deleted.", 'success')
    return redirect(url_for('list_checkpoints'))


# ===================== CHECK-INS: EDIT =====================
@app.route('/checkins/<int:checkin_id>/edit', methods=['GET', 'POST'])
@roles_required('judge', 'admin')
def edit_checkin(checkin_id):
    c = Checkin.query.get_or_404(checkin_id)
    teams = Team.query.order_by(Team.name.asc()).all()
    checkpoints = Checkpoint.query.order_by(Checkpoint.name.asc()).all()

    if request.method == 'POST':
        new_team_id = request.form.get('team_id', type=int)
        new_cp_id = request.form.get('checkpoint_id', type=int)
        ts_str = request.form.get('timestamp')
        tz_name = request.form.get('timezone')

        new_ts = from_datetime_local(ts_str, tz_name) or c.timestamp
        c.timestamp = new_ts

        # Basic validations
        if not Team.query.get(new_team_id):
            flash("Invalid team.", "warning")
            return redirect(url_for('edit_checkin', checkin_id=checkin_id))
        if not Checkpoint.query.get(new_cp_id):
            flash("Invalid checkpoint.", "warning")
            return redirect(url_for('edit_checkin', checkin_id=checkin_id))

        new_ts = from_datetime_local(ts_str) or c.timestamp  # keep old if parsing failed

        c.team_id = new_team_id
        c.checkpoint_id = new_cp_id
        c.timestamp = new_ts

        db.session.commit()
        flash("Check-in updated.", "success")
        return redirect(url_for('view_checkins'))

    # GET
    return render_template(
        'checkin_edit.html',
        c=c,
        teams=teams,
        checkpoints=checkpoints,
        timestamp_local=to_datetime_local(c.timestamp),
    )


# ===================== CHECK-INS: DELETE =====================
@app.route('/checkins/<int:checkin_id>/delete', methods=['POST'])
@roles_required('admin')
def delete_checkin(checkin_id):
    c = Checkin.query.get_or_404(checkin_id)
    db.session.delete(c)
    db.session.commit()
    flash("Check-in deleted.", "success")
    return redirect(url_for('view_checkins'))

# ===================== RFID MAPPINGS: LIST =====================
@app.route('/rfid')
@roles_required('admin')
def list_rfid():
    # Show mapping with team joined
    cards = (RFIDCard.query
             .options(joinedload(RFIDCard.team))
             .order_by(RFIDCard.uid.asc())
             .all())
    teams = Team.query.order_by(Team.name.asc()).all()
    return render_template('rfid_list.html', cards=cards, teams=teams)

# ===================== RFID MAPPINGS: ADD =====================
@app.route('/rfid/add', methods=['GET', 'POST'])
@roles_required('admin')
def add_rfid():
    teams = Team.query.order_by(Team.name.asc()).all()

    if request.method == 'POST':
        uid = request.form.get('uid', '').strip()
        team_id = request.form.get('team_id', type=int)

        # Basic validation
        if not uid:
            flash("UID is required.", "warning")
            return render_template('rfid_add.html', teams=teams)
        if not Team.query.get(team_id):
            flash("Invalid team.", "warning")
            return render_template('rfid_add.html', teams=teams)
        existing_team_card = RFIDCard.query.filter_by(team_id=team_id).first()
        if existing_team_card:
            flash("This team already has an RFID card assigned.", "warning")
            return render_template('rfid_add.html', teams=teams)

        card = RFIDCard(uid=uid, team_id=team_id)
        db.session.add(card)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("UID already exists. Use Edit to reassign or change UID.", "warning")
            return render_template('rfid_add.html', teams=teams)

        flash("RFID mapping created.", "success")
        return redirect(url_for('list_rfid'))

    return render_template('rfid_add.html', teams=teams)

# ===================== RFID MAPPINGS: EDIT =====================
@app.route('/rfid/<int:card_id>/edit', methods=['GET', 'POST'])
@roles_required('admin')
def edit_rfid(card_id):
    card = RFIDCard.query.get_or_404(card_id)
    teams = Team.query.order_by(Team.name.asc()).all()

    if request.method == 'POST':
        new_uid = request.form.get('uid', '').strip()
        new_team_id = request.form.get('team_id', type=int)

        if not new_uid:
            flash("UID is required.", "warning")
            return render_template('rfid_edit.html', card=card, teams=teams)
        if not Team.query.get(new_team_id):
            flash("Invalid team.", "warning")
            return render_template('rfid_edit.html', card=card, teams=teams)

        card.uid = new_uid
        card.team_id = new_team_id
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            flash("UID already exists. Choose a different UID.", "warning")
            return render_template('rfid_edit.html', card=card, teams=teams)

        flash("RFID mapping updated.", "success")
        return redirect(url_for('list_rfid'))

    return render_template('rfid_edit.html', card=card, teams=teams)

# ===================== RFID MAPPINGS: DELETE =====================
@app.route('/rfid/<int:card_id>/delete', methods=['POST'])
@roles_required('admin')
def delete_rfid(card_id):
    card = RFIDCard.query.get_or_404(card_id)
    db.session.delete(card)
    db.session.commit()
    flash("RFID mapping deleted.", "success")
    return redirect(url_for('list_rfid'))


# ================== RFID SCAN ==================
@app.route('/rfid/scan_once', methods=['POST'])
@roles_required('admin')
def rfid_scan_once():
    uid = read_uid_once()
    if not uid:
        return {"ok": False, "error": "No UID read (check device, cable, or increase timeout)."}, 200
    return {"ok": True, "uid": uid}, 200



# ===================== RFID CSV UPLOAD (UI) =====================
@app.route('/rfid/upload_csv', methods=['GET'])
@roles_required('admin')
def rfid_upload_csv_form():
    return render_template('rfid_upload_csv.html')


# ===================== RFID CSV UPLOAD (POST) =====================
@app.route('/rfid/upload_csv', methods=['POST'])
@roles_required('admin')
def rfid_upload_csv():
    """
    Accepts a CSV with columns:
      - Option A: uid,team_id
      - Option B: uid,team_name[,team_number]
    Notes:
      - UID is normalized (remove colons/hyphens, uppercase).
      - team_id takes precedence if present.
      - team_name + optional team_number resolves to a team.
    """
    file = request.files.get('file')
    if not file:
        flash("Please choose a CSV file.", "warning")
        return redirect(url_for('rfid_upload_csv_form'))

    try:
        # Read as text
        stream = io.StringIO(file.stream.read().decode('utf-8', errors='ignore'))
        reader = csv.DictReader(stream)
    except Exception:
        flash("Could not read CSV. Ensure it has a header row.", "warning")
        return redirect(url_for('rfid_upload_csv_form'))

    created, updated, skipped, errors = 0, 0, 0, []

    for i, row in enumerate(reader, start=2):  # start=2 => account for header line=1
        raw_uid = (row.get('uid') or '').strip()
        uid = normalize_uid(raw_uid)
        if not uid:
            skipped += 1
            errors.append(f"Line {i}: missing uid")
            continue

        # Resolve team
        team = None
        team_id_val = (row.get('team_id') or '').strip()
        team_name = (row.get('team_name') or '').strip()
        team_number = (row.get('team_number') or '').strip()

        if team_id_val:
            try:
                tid = int(team_id_val)
                team = Team.query.get(tid)
            except ValueError:
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
            errors.append(f"Line {i}: could not resolve team (team_id or team_name required)")
            continue

        # Upsert logic: if UID exists, update team; else create mapping
        card = RFIDCard.query.filter_by(uid=uid).first()
        if card:
            if card.team_id != team.id:
                card.team_id = team.id
                try:
                    db.session.commit()
                    updated += 1
                except IntegrityError:
                    db.session.rollback()
                    skipped += 1
                    errors.append(f"Line {i}: DB integrity error when updating {uid}")
            else:
                skipped += 1  # no change
        else:
            try:
                db.session.add(RFIDCard(uid=uid, team_id=team.id))
                db.session.commit()
                created += 1
            except IntegrityError:
                db.session.rollback()
                skipped += 1
                errors.append(f"Line {i}: UID already exists {uid}")

    # Summarize
    msg = f"CSV processed: {created} created, {updated} updated, {skipped} skipped."
    if errors:
        # Show first few errors to not overwhelm UI
        preview = "\n".join(errors[:8])
        flash(msg + " Some issues:\n" + preview + ("" if len(errors) <= 8 else "\n…"), "warning")
    else:
        flash(msg, "success")

    return redirect(url_for('list_rfid'))


@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)   # logs in + sets session
            flash("Signed in.", "success")
            next_url = request.args.get("next") or url_for("index")
            return redirect(next_url)
        flash("Invalid username or password.", "warning")
    return render_template("login.html")

@app.route("/logout", methods=["POST"])
@login_required
def logout():
    logout_user()
    flash("Signed out.", "success")
    return redirect(url_for("index"))

# Admin can create users
@app.route("/register", methods=["GET", "POST"])
@roles_required("admin")
def register():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        role = (request.form.get("role") or "public").strip()
        if not username or not password or role not in ("public", "judge", "admin"):
            flash("Invalid form data.", "warning")
            return render_template("register.html")
        if User.query.filter_by(username=username).first():
            flash("Username already exists.", "warning")
            return render_template("register.html")
        u = User(username=username, role=role)
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        flash(f"User '{username}' created with role '{role}'.", "success")
        return redirect(url_for("index"))
    return render_template("register.html")


@app.route("/__create_admin__")
def __create_admin__():
    if User.query.filter_by(role="admin").first():
        return "Admin already exists."
    u = User(username="admin", role="admin")
    u.set_password("change-me-now")
    db.session.add(u)
    db.session.commit()
    return "Admin created. Go login and then DELETE this route!"

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
    app.run(debug=True)