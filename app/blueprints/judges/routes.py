# app/blueprints/judges/routes.py
from __future__ import annotations

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_babel import gettext as _

from app.extensions import db
from app.models import User, CompetitionMember, Checkpoint, JudgeCheckpoint
from app.utils.competition import get_current_competition_id
from app.utils.perms import roles_required

judges_bp = Blueprint("judges", __name__, template_folder="../../templates")


def _require_competition():
    comp_id = get_current_competition_id()
    if not comp_id:
        flash(_("Select a competition first."), "warning")
        return None, redirect(url_for("main.select_competition"))
    return comp_id, None


@judges_bp.route("/assign", methods=["GET", "POST"])
@roles_required("admin")
def assign_checkpoints():
    comp_id, redirect_resp = _require_competition()
    if redirect_resp:
        return redirect_resp

    members = (
        db.session.query(User, CompetitionMember)
        .join(CompetitionMember, CompetitionMember.user_id == User.id)
        .filter(
            CompetitionMember.competition_id == comp_id,
            CompetitionMember.active.is_(True),
            CompetitionMember.role.in_(["judge", "admin"]),
        )
        .order_by(User.username.asc())
        .all()
    )
    judges = [{"id": u.id, "username": u.username, "role": m.role} for u, m in members]
    checkpoints = (
        Checkpoint.query
        .filter(Checkpoint.competition_id == comp_id)
        .order_by(Checkpoint.name.asc())
        .all()
    )

    if request.method == "POST":
        judge_id = request.form.get("judge_id", type=int)
        selected_ids = request.form.getlist("checkpoint_ids")
        default_id = request.form.get("default_checkpoint_id", type=int)

        if not judge_id:
            flash(_("Select a judge."), "warning")
            return redirect(url_for("judges.assign_checkpoints"))

        member = (
            CompetitionMember.query
            .filter(
                CompetitionMember.competition_id == comp_id,
                CompetitionMember.user_id == judge_id,
                CompetitionMember.active.is_(True),
            )
            .first()
        )
        if not member or member.role not in ("judge", "admin"):
            flash(_("Invalid judge selection."), "warning")
            return redirect(url_for("judges.assign_checkpoints"))

        try:
            selected_ids = [int(x) for x in selected_ids]
        except Exception:
            selected_ids = []

        allowed_ids = {
            c.id for c in checkpoints
        }
        selected_ids = [cid for cid in selected_ids if cid in allowed_ids]

        if default_id and default_id not in selected_ids:
            default_id = None

        existing = (
            JudgeCheckpoint.query
            .filter(JudgeCheckpoint.user_id == judge_id)
            .all()
        )
        existing_ids = {jc.checkpoint_id for jc in existing}
        selected_set = set(selected_ids)

        for jc in existing:
            if jc.checkpoint_id not in selected_set:
                db.session.delete(jc)

        for cid in selected_ids:
            if cid not in existing_ids:
                db.session.add(JudgeCheckpoint(user_id=judge_id, checkpoint_id=cid))

        if selected_ids and not default_id:
            default_id = selected_ids[0]

        if selected_ids:
            (
                JudgeCheckpoint.query
                .filter(JudgeCheckpoint.user_id == judge_id)
                .update({JudgeCheckpoint.is_default: False}, synchronize_session=False)
            )
            JudgeCheckpoint.query.filter(
                JudgeCheckpoint.user_id == judge_id,
                JudgeCheckpoint.checkpoint_id == default_id,
            ).update({JudgeCheckpoint.is_default: True}, synchronize_session=False)

        db.session.commit()
        flash(_("Judge checkpoints updated."), "success")
        return redirect(url_for("judges.assign_checkpoints", judge_id=judge_id))

    selected_judge_id = request.args.get("judge_id", type=int)
    assigned = []
    default_checkpoint_id = None
    if selected_judge_id:
        assigned = (
            JudgeCheckpoint.query
            .filter(JudgeCheckpoint.user_id == selected_judge_id)
            .all()
        )
        default_row = next((jc for jc in assigned if jc.is_default), None)
        default_checkpoint_id = default_row.checkpoint_id if default_row else None

    assigned_ids = {jc.checkpoint_id for jc in assigned}
    return render_template(
        "judge_assign.html",
        judges=judges,
        checkpoints=checkpoints,
        selected_judge_id=selected_judge_id,
        assigned_ids=assigned_ids,
        default_checkpoint_id=default_checkpoint_id,
    )
