# app/blueprints/groups/routes.py
from __future__ import annotations
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from sqlalchemy.orm import joinedload
from app.extensions import db
from app.models import CheckpointGroup, Checkpoint, TeamGroup, Team
from app.utils.perms import roles_required

groups_bp = Blueprint("groups", __name__, template_folder="../../templates")

# ---------- Helpers ----------
def _parse_checkpoint_ids(form_field_values) -> list[int]:
    ids: list[int] = []
    for v in (form_field_values or []):
        try:
            n = int(v)
            if n > 0:
                ids.append(n)
        except Exception:
            pass
    return ids

# ---------- List groups ----------
@groups_bp.route("/", methods=["GET"])
@roles_required("judge","admin")
def list_groups():
    groups = CheckpointGroup.query.order_by(CheckpointGroup.name.asc()).all()
    # In template use: {{ g.checkpoints|length }}
    return render_template("groups_list.html", groups=groups)

# ---------- Create group ----------
@groups_bp.route("/add", methods=["GET", "POST"])
@roles_required("judge", "admin")
def add_group():
    cps = db.session.query(Checkpoint).order_by(Checkpoint.name.asc()).all()

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        desc = (request.form.get("description") or "").strip() or None
        selected_ids = _parse_checkpoint_ids(request.form.getlist("checkpoint_ids"))

        if not name:
            flash("Group name is required.", "warning")
            return render_template("group_edit.html", mode="add", cps=cps)

        g = CheckpointGroup(name=name, description=desc)
        if selected_ids:
            g.checkpoints = db.session.query(Checkpoint).filter(Checkpoint.id.in_(selected_ids)).all()

        db.session.add(g)
        db.session.commit()
        flash("Group created.", "success")
        return redirect(url_for("groups.list_groups"))

    return render_template("group_edit.html", mode="add", cps=cps)

# ---------- Edit group (incl. assign checkpoints) ----------
@groups_bp.route("/<int:group_id>/edit", methods=["GET", "POST"])
@roles_required("judge", "admin")
def edit_group(group_id: int):
    g = (
        db.session.query(CheckpointGroup)
        .options(joinedload(CheckpointGroup.checkpoints))
        .get_or_404(group_id)
    )
    cps = db.session.query(Checkpoint).order_by(Checkpoint.name.asc()).all()

    if request.method == "POST":
        g.name = (request.form.get("name") or "").strip()
        g.description = (request.form.get("description") or "").strip() or None
        selected_ids = _parse_checkpoint_ids(request.form.getlist("checkpoint_ids"))

        if not g.name:
            flash("Group name is required.", "warning")
            return render_template("group_edit.html", mode="edit", g=g, cps=cps, selected_ids={cp.id for cp in g.checkpoints})

        # Replace many-to-many members with exactly the selected ones
        if selected_ids:
            g.checkpoints = db.session.query(Checkpoint).filter(Checkpoint.id.in_(selected_ids)).all()
        else:
            g.checkpoints = []

        db.session.commit()
        flash("Group updated.", "success")
        return redirect(url_for("groups.list_groups"))

    selected_ids = {cp.id for cp in g.checkpoints}
    return render_template("group_edit.html", mode="edit", g=g, cps=cps, selected_ids=selected_ids)

# ---------- Delete group ----------
@groups_bp.route("/<int:group_id>/delete", methods=["POST"])
@roles_required("admin")
def delete_group(group_id: int):
    g = db.session.query(CheckpointGroup).get_or_404(group_id)

    # If you want to forbid deleting groups that are active for teams:
    active_refs = db.session.query(TeamGroup).filter(
        TeamGroup.group_id == group_id,
        TeamGroup.active.is_(True),
    ).count()
    if active_refs:
        flash("Cannot delete a group that is active for one or more teams.", "warning")
        return redirect(url_for("groups.list_groups"))

    db.session.delete(g)
    db.session.commit()
    flash("Group deleted.", "success")
    return redirect(url_for("groups.list_groups"))

# ---------- Set a team's active group (quick action) ----------
@groups_bp.route("/set_active", methods=["POST"])
@roles_required("judge", "admin")
def set_active_group_for_team():
    team_id = request.form.get("team_id", type=int)
    group_id = request.form.get("group_id", type=int)

    if not team_id or not group_id:
        flash("team_id and group_id are required.", "warning")
        return redirect(url_for("groups.list_groups"))

    team = db.session.query(Team).get(team_id)
    group = db.session.query(CheckpointGroup).get(group_id)
    if not team or not group:
        flash("Invalid team or group.", "warning")
        return redirect(url_for("groups.list_groups"))

    # Deactivate existing active assignment(s) and set the new one active
    TeamGroup.query.filter_by(team_id=team.id, active=True).update({"active": False})
    tg = TeamGroup.query.filter_by(team_id=team.id, group_id=group.id).first()
    if tg:
        tg.active = True
    else:
        db.session.add(TeamGroup(team_id=team.id, group_id=group.id, active=True))

    db.session.commit()
    flash(f"Set active group for team '{team.name}' to '{group.name}'.", "success")
    return redirect(url_for("groups.list_groups"))

