# app/blueprints/groups/routes.py
from flask import Blueprint, render_template, request, redirect, url_for, flash
from sqlalchemy.orm import joinedload
from app.extensions import db
from app.models import Checkpoint, Team, CheckpointGroup, GroupCheckpoint, TeamGroup
from app.utils.perms import roles_required

groups_bp = Blueprint("groups", __name__, template_folder="../../templates")

# ---- List groups ----
@groups_bp.route("/", methods=["GET"])
@roles_required("judge","admin")
def list_groups():
    groups = CheckpointGroup.query.order_by(CheckpointGroup.name.asc()).all()
    return render_template("groups_list.html", groups=groups)

# ---- Add group ----
@groups_bp.route("/add", methods=["GET","POST"])
@roles_required("judge","admin")
def add_group():
    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        desc = (request.form.get("description") or "").strip()
        if not name:
            flash("Name required.", "warning")
            return render_template("group_add.html")
        if CheckpointGroup.query.filter_by(name=name).first():
            flash("Group name already exists.", "warning")
            return render_template("group_add.html")
        g = CheckpointGroup(name=name, description=desc)
        db.session.add(g); db.session.commit()
        flash("Group created.", "success")
        return redirect(url_for("groups.list_groups"))
    return render_template("group_add.html")

# ---- Manage group membership (add/remove checkpoints, order) ----
@groups_bp.route("/<int:group_id>/members", methods=["GET","POST"])
@roles_required("judge","admin")
def group_members(group_id):
    g = CheckpointGroup.query.get_or_404(group_id)

    if request.method == "POST":
        action = request.form.get("action")
        if action == "add_cp":
            cp_id = request.form.get("checkpoint_id", type=int)
            if not cp_id:
                flash("Choose a checkpoint.", "warning")
            else:
                exists = GroupCheckpoint.query.filter_by(group_id=g.id, checkpoint_id=cp_id).first()
                if exists:
                    flash("Checkpoint already in group.", "warning")
                else:
                    # seq_index = next index
                    max_seq = db.session.query(db.func.max(GroupCheckpoint.seq_index)).filter_by(group_id=g.id).scalar() or 0
                    db.session.add(GroupCheckpoint(group_id=g.id, checkpoint_id=cp_id, seq_index=max_seq+1))
                    db.session.commit()
                    flash("Added checkpoint.", "success")

        elif action in ("move_up","move_down"):
            link_id = request.form.get("link_id", type=int)
            link = GroupCheckpoint.query.get_or_404(link_id)
            if link.group_id != g.id:
                flash("Invalid item.", "warning")
            else:
                delta = -1 if action == "move_up" else 1
                neighbor = (GroupCheckpoint.query
                            .filter_by(group_id=g.id, seq_index=link.seq_index + delta)
                            .first())
                if neighbor:
                    link.seq_index, neighbor.seq_index = neighbor.seq_index, link.seq_index
                    db.session.commit()

        elif action == "remove_cp":
            link_id = request.form.get("link_id", type=int)
            link = GroupCheckpoint.query.get_or_404(link_id)
            if link.group_id == g.id:
                db.session.delete(link); db.session.commit()
                flash("Removed checkpoint.", "success")

        return redirect(url_for("groups.group_members", group_id=g.id))

    # GET: show current members + available checkpoints
    members = (GroupCheckpoint.query
               .options(joinedload(GroupCheckpoint.checkpoint))
               .filter_by(group_id=g.id)
               .order_by(GroupCheckpoint.seq_index.asc())
               .all())
    member_cp_ids = [m.checkpoint_id for m in members]
    available = Checkpoint.query.filter(~Checkpoint.id.in_(member_cp_ids)).order_by(Checkpoint.name.asc()).all()

    return render_template("group_members.html", group=g, members=members, available=available)

# ---- Assign team to a group (or change) ----
@groups_bp.route("/assign", methods=["GET","POST"])
@roles_required("judge","admin")
def assign_team_group():
    teams = Team.query.order_by(Team.name.asc()).all()
    groups = CheckpointGroup.query.order_by(CheckpointGroup.name.asc()).all()

    if request.method == "POST":
        team_id = request.form.get("team_id", type=int)
        group_id = request.form.get("group_id", type=int)
        if not team_id or not group_id:
            flash("Choose both team and group.", "warning")
            return render_template("assign_team_group.html", teams=teams, groups=groups)

        # deactivate existing actives then set selected active
        TeamGroup.query.filter_by(team_id=team_id, active=True).update({"active": False})
        # ensure unique row exists
        tg = TeamGroup.query.filter_by(team_id=team_id, group_id=group_id).first()
        if not tg:
            tg = TeamGroup(team_id=team_id, group_id=group_id, active=True)
            db.session.add(tg)
        else:
            tg.active = True
        db.session.commit()
        flash("Team assignment updated.", "success")
        return redirect(url_for("groups.assign_team_group"))

    return render_template("assign_team_group.html", teams=teams, groups=groups)