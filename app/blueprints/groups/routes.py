# app/blueprints/groups/routes.py
from __future__ import annotations
from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from sqlalchemy.orm import joinedload
from app.extensions import db
from app.models import CheckpointGroup, Checkpoint, CheckpointGroupLink, TeamGroup, Team
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

def _sync_group_checkpoints(group: CheckpointGroup, ordered_ids: list[int]) -> None:
    """
    Update group's checkpoints to match ordered_ids while preserving links that remain
    and assigning sequential position values for ordering.
    """
    existing = {link.checkpoint_id: link for link in group.checkpoint_links}
    new_links: list[CheckpointGroupLink] = []

    for position, cp_id in enumerate(ordered_ids):
        link = existing.pop(cp_id, None)
        if link is None:
            checkpoint = db.session.get(Checkpoint, cp_id)
            if not checkpoint:
                continue  # skip invalid IDs silently
            link = CheckpointGroupLink(group=group, checkpoint=checkpoint)
        link.position = position
        new_links.append(link)

    # Remove links that are no longer selected
    for obsolete in existing.values():
        db.session.delete(obsolete)

    group.checkpoint_links = new_links


def _partition_checkpoints(all_checkpoints: list[Checkpoint], ordered_ids: list[int]) -> tuple[list[Checkpoint], list[Checkpoint]]:
    lookup = {cp.id: cp for cp in all_checkpoints}
    selected = [lookup[cp_id] for cp_id in ordered_ids if cp_id in lookup]
    selected_ids = set(ordered_ids)
    available = [cp for cp in all_checkpoints if cp.id not in selected_ids]
    return selected, available

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
        has_checkpoint_payload = request.form.get("checkpoint_ids_present") == "1"
        selected_ids = _parse_checkpoint_ids(request.form.getlist("checkpoint_ids")) if has_checkpoint_payload else []
        selected_items, available_items = _partition_checkpoints(cps, selected_ids)

        if not name:
            flash("Group name is required.", "warning")
            return render_template(
                "group_edit.html",
                mode="add",
                cps=cps,
                selected_ids=selected_ids,
                selected_checkpoints=selected_items,
                available_checkpoints=available_items,
            )

        g = CheckpointGroup(name=name, description=desc)
        db.session.add(g)
        db.session.flush()

        if has_checkpoint_payload:
            _sync_group_checkpoints(g, selected_ids)

        db.session.commit()
        flash("Group created.", "success")
        return redirect(url_for("groups.list_groups"))

    selected_items, available_items = _partition_checkpoints(cps, [])
    return render_template(
        "group_edit.html",
        mode="add",
        cps=cps,
        selected_ids=[],
        selected_checkpoints=selected_items,
        available_checkpoints=available_items,
    )

# ---------- Edit group (incl. assign checkpoints) ----------
@groups_bp.route("/<int:group_id>/edit", methods=["GET", "POST"])
@roles_required("judge", "admin")
def edit_group(group_id: int):
    g = (
        db.session.query(CheckpointGroup)
        .options(
            joinedload(CheckpointGroup.checkpoint_links)
            .joinedload(CheckpointGroupLink.checkpoint)
        )
        .get_or_404(group_id)
    )
    cps = db.session.query(Checkpoint).order_by(Checkpoint.name.asc()).all()

    if request.method == "POST":
        g.name = (request.form.get("name") or "").strip()
        g.description = (request.form.get("description") or "").strip() or None
        has_checkpoint_payload = request.form.get("checkpoint_ids_present") == "1"
        selected_ids = _parse_checkpoint_ids(request.form.getlist("checkpoint_ids")) if has_checkpoint_payload else []
        selected_items, available_items = _partition_checkpoints(cps, selected_ids)

        if not g.name:
            flash("Group name is required.", "warning")
            if not selected_items:
                selected_items = [link.checkpoint for link in g.checkpoint_links]
                existing_ids = {link.checkpoint_id for link in g.checkpoint_links}
                available_items = [cp for cp in cps if cp.id not in existing_ids]
            return render_template(
                "group_edit.html",
                mode="edit",
                g=g,
                cps=cps,
                selected_ids=[link.checkpoint_id for link in g.checkpoint_links],
                selected_checkpoints=selected_items,
                available_checkpoints=available_items,
            )

        if has_checkpoint_payload:
            _sync_group_checkpoints(g, selected_ids)

        db.session.commit()
        flash("Group updated.", "success")
        return redirect(url_for("groups.list_groups"))

    selected_ids = [link.checkpoint_id for link in g.checkpoint_links]
    selected_items, available_items = _partition_checkpoints(cps, selected_ids)
    return render_template(
        "group_edit.html",
        mode="edit",
        g=g,
        cps=cps,
        selected_ids=selected_ids,
        selected_checkpoints=selected_items,
        available_checkpoints=available_items,
    )

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
