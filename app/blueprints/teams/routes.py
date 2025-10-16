# app/blueprints/teams/routes.py
from flask import Blueprint, render_template, request, redirect, url_for, flash
from sqlalchemy.orm import joinedload
from app.extensions import db
from app.models import Team, CheckpointGroup, TeamGroup
from app.utils.perms import roles_required

teams_bp = Blueprint("teams", __name__, template_folder="../../templates")


def _active_group_id_for(team: Team) -> int | None:
    """Return the active group_id for a team, if any."""
    for tg in team.group_assignments:
        if tg.active:
            return tg.group_id
    return None


# ============ LIST ============
@teams_bp.route("/", methods=["GET"])
def list_teams():
    # Everyone can see teams
    teams = (
        Team.query
        .options(joinedload(Team.group_assignments).joinedload(TeamGroup.group))
        .order_by(Team.name.asc())
        .all()
    )
    return render_template("teams_list.html", teams=teams)


# ============ ADD ============
@teams_bp.route("/add", methods=["GET", "POST"])
@roles_required("judge", "admin")
def add_team():
    groups = CheckpointGroup.query.order_by(CheckpointGroup.name.asc()).all()

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        number_raw = (request.form.get("number") or "").strip()
        group_id = request.form.get("group_id", type=int)

        # Basic validation
        if not name:
            flash("Team name is required.", "warning")
            return render_template("add_team.html", groups=groups)

        number = None
        if number_raw:
            try:
                number = int(number_raw)
                if number <= 0:
                    raise ValueError()
            except ValueError:
                flash("Team number must be a positive integer.", "warning")
                return render_template("add_team.html", groups=groups)

        # Create team
        team = Team(name=name, number=number)
        db.session.add(team)
        db.session.commit()  # so team.id exists

        # Optional group assignment
        if group_id:
            # deactivate any existing (just in case) and set chosen active
            TeamGroup.query.filter_by(team_id=team.id, active=True).update({"active": False})
            db.session.add(TeamGroup(team_id=team.id, group_id=group_id, active=True))
            db.session.commit()

        flash(f"Team '{team.name}' created.", "success")
        return redirect(url_for("teams.list_teams"))

    # GET
    return render_template("add_team.html", groups=groups)


# ============ EDIT ============
@teams_bp.route("/<int:team_id>/edit", methods=["GET", "POST"])
@roles_required("judge", "admin")
def edit_team(team_id):
    team = Team.query.get_or_404(team_id)
    groups = CheckpointGroup.query.order_by(CheckpointGroup.name.asc()).all()

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        number_raw = (request.form.get("number") or "").strip()
        group_id = request.form.get("group_id", type=int)

        if not name:
            flash("Team name is required.", "warning")
            return render_template("team_edit.html", team=team, groups=groups)

        # number handling
        if number_raw:
            try:
                n = int(number_raw)
                if n <= 0:
                    raise ValueError()
                team.number = n
            except ValueError:
                flash("Team number must be a positive integer.", "warning")
                return render_template("team_edit.html", team=team, groups=groups)
        else:
            team.number = None

        team.name = name

        # Group assignment update
        if group_id:
            # Deactivate all current actives for this team
            TeamGroup.query.filter_by(team_id=team.id, active=True).update({"active": False})
            # Ensure (team, group) row exists; set active
            tg = TeamGroup.query.filter_by(team_id=team.id, group_id=group_id).first()
            if tg:
                tg.active = True
            else:
                db.session.add(TeamGroup(team_id=team.id, group_id=group_id, active=True))
        else:
            # If "None" selected (empty), deactivate any active assignments
            TeamGroup.query.filter_by(team_id=team.id, active=True).update({"active": False})

        db.session.commit()
        flash("Team updated.", "success")
        return redirect(url_for("teams.list_teams"))

    # GET
    active_gid = _active_group_id_for(team)
    return render_template("team_edit.html", team=team, groups=groups, active_gid=active_gid)


# ============ DELETE ============
@teams_bp.route("/<int:team_id>/delete", methods=["POST"])
@roles_required("admin")
def delete_team(team_id):
    team = Team.query.get_or_404(team_id)

    if team.checkins:
        flash("Cannot delete team with existing check-ins.", "warning")
        return redirect(url_for("teams.list_teams"))

    # Delete group assignment rows first (cascade may also handle this)
    TeamGroup.query.filter_by(team_id=team.id).delete()
    db.session.delete(team)
    db.session.commit()
    flash("Team deleted.", "success")
    return redirect(url_for("teams.list_teams"))