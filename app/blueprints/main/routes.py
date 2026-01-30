from flask import Blueprint, render_template, request, Response, session, redirect, url_for, current_app, abort, flash
from flask_babel import gettext as _
from flask_login import login_required, current_user
from sqlalchemy.orm import joinedload
from app.extensions import db
from app.models import Team, Checkpoint, Checkin, Competition, CompetitionMember, CompetitionInvite, User
from app.utils.time import to_datetime_local
from app.utils.competition import get_current_competition_id, get_user_memberships, set_current_competition_id, create_invite
from app.utils.perms import roles_required
import io, csv
from datetime import datetime, timedelta

main_bp = Blueprint('main', __name__)

@main_bp.route("/")
def index():
    if current_user.is_authenticated:
        return redirect(url_for("main.select_competition"))
    return render_template("landing.html")


@main_bp.route("/public-results", methods=["GET"])
def public_results():
    competitions = (
        Competition.query
        .filter(Competition.public_results.is_(True))
        .order_by(Competition.name.asc())
        .all()
    )
    return render_template("public_results.html", competitions=competitions)


@main_bp.route("/competitions", methods=["GET"])
@login_required
def select_competition():
    memberships = get_user_memberships(current_user.id)
    return render_template("competition_select.html", memberships=memberships)


@main_bp.route("/competitions/create", methods=["POST"])
@login_required
def create_competition():
    name = (request.form.get("name") or "").strip()
    if not name:
        flash(_("Competition name is required."), "warning")
        return redirect(url_for("main.select_competition"))

    existing = Competition.query.filter_by(name=name).first()
    if existing:
        flash(_("A competition with that name already exists."), "warning")
        return redirect(url_for("main.select_competition"))

    competition = Competition(name=name, created_by_user_id=current_user.id)
    db.session.add(competition)
    db.session.flush()

    db.session.add(
        CompetitionMember(
            competition_id=competition.id,
            user_id=current_user.id,
            role="admin",
            active=True,
        )
    )
    db.session.commit()

    set_current_competition_id(competition.id)
    flash(_("Competition created."), "success")
    return redirect(url_for("main.select_competition"))


@main_bp.route("/competitions/select/<int:competition_id>", methods=["POST"])
@login_required
def set_competition(competition_id: int):
    if not set_current_competition_id(competition_id):
        flash(_("You don't have access to that competition."), "warning")
        return redirect(url_for("main.select_competition"))
    flash(_("Competition selected."), "success")
    return redirect(url_for("teams.list_teams"))


@main_bp.route("/lang/<lang_code>", methods=["GET", "POST"])
def set_language(lang_code: str):
    languages = current_app.config.get("LANGUAGES", {})
    if lang_code not in languages:
        abort(404)
    session["lang"] = lang_code
    next_url = request.args.get("next") or request.referrer or url_for("main.index")
    return redirect(next_url)


def _parse_date_range(date_from_str, date_to_str):
    start = end = None
    try:
        if date_from_str: start = datetime.fromisoformat(date_from_str)
        if date_to_str: end = datetime.fromisoformat(date_to_str) + timedelta(days=1)
    except ValueError:
        pass
    return start, end

def _filtered_checkins(team_id, checkpoint_id, date_from_str, date_to_str):
    comp_id = get_current_competition_id()
    q = (Checkin.query
         .options(joinedload(Checkin.team), joinedload(Checkin.checkpoint)))
    if comp_id:
        q = q.filter(Checkin.competition_id == comp_id)
    if team_id: q = q.filter(Checkin.team_id == team_id)
    if checkpoint_id: q = q.filter(Checkin.checkpoint_id == checkpoint_id)
    date_from, date_to = _parse_date_range(date_from_str, date_to_str)
    if date_from: q = q.filter(Checkin.timestamp >= date_from)
    if date_to: q = q.filter(Checkin.timestamp < date_to)
    return q.order_by(Checkin.timestamp.desc())

@main_bp.route("/checkins")
def view_checkins():
    comp_id = get_current_competition_id()
    if not comp_id:
        flash(_("Select a competition first."), "warning")
        return redirect(url_for("main.select_competition"))
    teams = Team.query.filter(Team.competition_id == comp_id).order_by(Team.name.asc()).all()
    cps = Checkpoint.query.filter(Checkpoint.competition_id == comp_id).order_by(Checkpoint.name.asc()).all()
    team_id = request.args.get('team_id', type=int)
    cp_id = request.args.get('checkpoint_id', type=int)
    df = request.args.get('date_from')
    dt = request.args.get('date_to')
    checkins = _filtered_checkins(team_id, cp_id, df, dt).all()
    return render_template("view_checkins.html",
        checkins=checkins, teams=teams, checkpoints=cps,
        selected_team_id=team_id, selected_checkpoint_id=cp_id,
        selected_date_from=df or "", selected_date_to=dt or "")

@main_bp.route("/checkins.csv")
def export_checkins_csv():
    team_id = request.args.get('team_id', type=int)
    cp_id = request.args.get('checkpoint_id', type=int)
    df = request.args.get('date_from'); dt = request.args.get('date_to')
    rows = _filtered_checkins(team_id, cp_id, df, dt).all()
    si = io.StringIO(); w = csv.writer(si)
    w.writerow(["timestamp_utc","team_id","team_name","checkpoint_id","checkpoint_name"])
    for r in rows:
        w.writerow([r.timestamp.strftime('%Y-%m-%d %H:%M:%S'),
                    r.team.id if r.team else "",
                    r.team.name if r.team else "",
                    r.checkpoint.id if r.checkpoint else "",
                    r.checkpoint.name if r.checkpoint else ""])
    return Response(si.getvalue(), mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=checkins.csv"})


@main_bp.route("/competition/settings", methods=["GET", "POST"])
@roles_required("admin")
def competition_settings():
    comp_id = get_current_competition_id()
    if not comp_id:
        flash(_("Select a competition first."), "warning")
        return redirect(url_for("main.select_competition"))

    competition = Competition.query.filter(Competition.id == comp_id).first()
    if not competition:
        flash(_("Competition not found."), "warning")
        return redirect(url_for("main.select_competition"))

    if request.method == "POST":
        action = request.form.get("action") or "settings"
        if action == "invite":
            email = (request.form.get("invite_email") or "").strip().lower()
            role = (request.form.get("invite_role") or "judge").strip().lower()
            if not email or "@" not in email:
                flash(_("Valid email is required."), "warning")
                return redirect(url_for("main.competition_settings"))
            if role not in ("viewer", "judge", "admin"):
                flash(_("Invalid role selected."), "warning")
                return redirect(url_for("main.competition_settings"))
            existing_invite = (
                CompetitionInvite.query
                .filter(
                    CompetitionInvite.competition_id == competition.id,
                    CompetitionInvite.invited_email.ilike(email),
                    CompetitionInvite.used_at.is_(None),
                    CompetitionInvite.expires_at > datetime.utcnow(),
                )
                .first()
            )
            if existing_invite:
                flash(_("An active invite already exists for this email."), "warning")
                return redirect(url_for("main.competition_settings"))

            invite = create_invite(competition.id, current_user.id, role=role, invited_email=email)
            user = User.query.filter(User.email.ilike(email)).first()
            if user:
                membership = (
                    CompetitionMember.query
                    .filter(
                        CompetitionMember.competition_id == competition.id,
                        CompetitionMember.user_id == user.id,
                    )
                    .first()
                )
                if not membership:
                    db.session.add(
                        CompetitionMember(
                            competition_id=competition.id,
                            user_id=user.id,
                            role=role,
                            active=True,
                        )
                    )
                invite.invited_user_id = user.id
                invite.used_at = datetime.utcnow()
            db.session.commit()
            flash(_("Invite saved."), "success")
            return redirect(url_for("main.competition_settings"))

        new_name = (request.form.get("name") or "").strip()
        if not new_name:
            flash(_("Competition name is required."), "warning")
            return redirect(url_for("main.competition_settings"))
        existing = (
            Competition.query
            .filter(Competition.id != competition.id, Competition.name == new_name)
            .first()
        )
        if existing:
            flash(_("A competition with that name already exists."), "warning")
            return redirect(url_for("main.competition_settings"))
        competition.name = new_name
        competition.public_results = bool(request.form.get("public_results"))
        if request.form.get("clear_ingest_password"):
            competition.set_ingest_password(None)
        else:
            ingest_pw = (request.form.get("ingest_password") or "").strip()
            if ingest_pw:
                competition.set_ingest_password(ingest_pw)
        db.session.commit()
        flash(_("Competition settings updated."), "success")
        return redirect(url_for("main.competition_settings"))

    public_url = url_for("scores.public_scores", competition_id=competition.id, _external=True)
    now = datetime.utcnow()
    invites = (
        CompetitionInvite.query
        .filter(CompetitionInvite.competition_id == competition.id)
        .order_by(CompetitionInvite.created_at.desc())
        .all()
    )
    invite_rows = []
    for inv in invites:
        status = "pending"
        if inv.used_at:
            status = "used"
        elif inv.expires_at and inv.expires_at < now:
            status = "expired"
        invite_rows.append({
            "email": inv.invited_email or "",
            "role": inv.role,
            "status": status,
            "expires_at": inv.expires_at,
            "id": inv.id,
        })
    return render_template(
        "competition_settings.html",
        competition=competition,
        public_url=public_url,
        invites=invite_rows,
    )


@main_bp.route("/competition/invites/<int:invite_id>/revoke", methods=["POST"])
@roles_required("admin")
def revoke_invite(invite_id: int):
    comp_id = get_current_competition_id()
    if not comp_id:
        flash(_("Select a competition first."), "warning")
        return redirect(url_for("main.select_competition"))

    invite = (
        CompetitionInvite.query
        .filter(
            CompetitionInvite.id == invite_id,
            CompetitionInvite.competition_id == comp_id,
        )
        .first()
    )
    if not invite:
        flash(_("Invite not found."), "warning")
        return redirect(url_for("main.competition_settings"))

    db.session.delete(invite)
    db.session.commit()
    flash(_("Invite revoked."), "success")
    return redirect(url_for("main.competition_settings"))


@main_bp.route("/competition/delete", methods=["POST"])
@roles_required("superadmin")
def delete_competition():
    comp_id = get_current_competition_id()
    if not comp_id:
        flash(_("Select a competition first."), "warning")
        return redirect(url_for("main.select_competition"))

    competition = Competition.query.filter(Competition.id == comp_id).first()
    if not competition:
        flash(_("Competition not found."), "warning")
        return redirect(url_for("main.select_competition"))

    try:
        db.session.query(User).filter(User.last_competition_id == comp_id).update(
            {User.last_competition_id: None},
            synchronize_session=False,
        )
        db.session.delete(competition)
        db.session.commit()
        session.pop("competition_id", None)
        flash(_("Competition deleted."), "success")
    except Exception:
        db.session.rollback()
        flash(_("Could not delete competition."), "warning")
    return redirect(url_for("main.select_competition"))
