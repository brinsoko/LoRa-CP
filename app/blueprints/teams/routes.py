# app/blueprints/teams/routes.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from sqlalchemy.orm import joinedload
from app.extensions import db
from app.models import Team, CheckpointGroup, TeamGroup
from app.utils.perms import roles_required

teams_bp = Blueprint("teams", __name__, template_folder="../../templates")


def _active_group_id_for(team: Team) -> int | None:
    for tg in team.group_assignments:
        if tg.active:
            return tg.group_id
    return None


# ============ LIST ============
@teams_bp.route("/", methods=["GET"])
def list_teams():
    teams = (
        Team.query
        .options(joinedload(Team.group_assignments).joinedload(TeamGroup.group))
        .order_by(Team.name.asc())
        .all()
    )
    # No sets in template needed here
    return render_template("teams_list.html", teams=teams)


# ============ ADD ============
@teams_bp.route("/add", methods=["GET", "POST"])
@roles_required("judge", "admin")
def add_team():
    groups = CheckpointGroup.query.order_by(CheckpointGroup.name.asc()).all()

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        number = request.form.get("number", type=int)

        # Gather checkbox selections; DO NOT call set() in template
        selected_ids = {int(x) for x in request.form.getlist("group_ids")}
        current_app.logger.debug("[teams.add] name=%r number=%r selected_ids=%r",
                                 name, number, selected_ids)

        if not name:
            flash("Team name is required.", "warning")
            return render_template("add_team.html",
                                   groups=groups,
                                   selected_group_ids=set())

        t = Team(name=name, number=number)
        db.session.add(t)
        db.session.flush()  # get t.id

        for gid in selected_ids:
            db.session.add(TeamGroup(team_id=t.id, group_id=gid, active=True))

        db.session.commit()
        flash("Team created.", "success")
        return redirect(url_for("teams.list_teams"))

    # Provide an empty set so template can do: {% if g.id in selected_group_ids %}
    return render_template("add_team.html", groups=groups, selected_group_ids=set())


# ============ EDIT ============
@teams_bp.route("/<int:team_id>/edit", methods=["GET", "POST"])
@roles_required("judge", "admin")
def edit_team(team_id):
    team = (
        Team.query
        .options(joinedload(Team.group_assignments))
        .get_or_404(team_id)
    )
    groups = CheckpointGroup.query.order_by(CheckpointGroup.name.asc()).all()

    if request.method == "POST":
        team.name = (request.form.get("name") or "").strip()
        team.number = request.form.get("number", type=int)

        selected_ids = {int(x) for x in request.form.getlist("group_ids")}
        current_ids = {tg.group_id for tg in team.group_assignments}

        current_app.logger.debug(
            "[teams.edit] team_id=%s selected_ids=%r current_ids=%r",
            team.id, selected_ids, current_ids
        )

        # Delete assignments that are no longer checked
        if current_ids - selected_ids:
            q = (db.session.query(TeamGroup)
                 .filter(TeamGroup.team_id == team.id))
            if selected_ids:
                q = q.filter(~TeamGroup.group_id.in_(list(selected_ids)))
            # If no selections, delete all rows for this team
            q.delete(synchronize_session=False)

        # Add new assignments
        for gid in (selected_ids - current_ids):
            db.session.add(TeamGroup(team_id=team.id, group_id=gid, active=True))

        db.session.commit()
        flash("Team updated.", "success")
        return redirect(url_for("teams.list_teams"))

    # Provide a ready-to-use set for the template
    selected_group_ids = {tg.group_id for tg in team.group_assignments}
    return render_template("team_edit.html",
                           team=team,
                           groups=groups,
                           selected_group_ids=selected_group_ids)


# ============ DELETE ============
@teams_bp.route("/<int:team_id>/delete", methods=["POST"])
@roles_required("admin")
def delete_team(team_id):
    team = Team.query.get_or_404(team_id)

    if team.checkins:
        flash("Cannot delete team with existing check-ins.", "warning")
        return redirect(url_for("teams.list_teams"))

    TeamGroup.query.filter_by(team_id=team.id).delete()
    db.session.delete(team)
    db.session.commit()
    flash("Team deleted.", "success")
    return redirect(url_for("teams.list_teams"))