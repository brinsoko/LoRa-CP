# app/blueprints/users/routes.py
from __future__ import annotations
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_babel import gettext as _
from flask_login import login_required
from app.extensions import db
from app.models import User, CompetitionMember
from app.utils.competition import get_current_competition_id
from app.utils.perms import roles_required  # already in your project

users_bp = Blueprint("users", __name__, template_folder="../../templates")

@users_bp.route("/", methods=["GET"])
@roles_required("admin")
def list_users():
    comp_id = get_current_competition_id()
    if not comp_id:
        flash(_("Select a competition first."), "warning")
        return redirect(url_for("main.select_competition"))
    rows = (
        db.session.query(User, CompetitionMember)
        .join(CompetitionMember, CompetitionMember.user_id == User.id)
        .filter(
            CompetitionMember.competition_id == comp_id,
            CompetitionMember.active.is_(True),
        )
        .order_by(User.username.asc())
        .all()
    )
    users = []
    for user, membership in rows:
        user.membership_role = membership.role
        users.append(user)
    return render_template("users_list.html", users=users)

@users_bp.route("/add", methods=["GET", "POST"])
@roles_required("admin")
def add_user():
    if request.method == "POST":
        comp_id = get_current_competition_id()
        if not comp_id:
            flash(_("Select a competition first."), "warning")
            return redirect(url_for("main.select_competition"))
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        role = (request.form.get("role") or "viewer").strip()

        if not username or not password or role not in ("viewer", "judge", "admin"):
            flash(_("Please fill all fields. Role must be viewer/judge/admin."), "warning")
            return render_template("user_edit.html", mode="add")

        if User.query.filter_by(username=username).first():
            flash(_("Username already exists."), "warning")
            return render_template("user_edit.html", mode="add")

        user_role = "public" if role == "viewer" else role
        u = User(username=username, role=user_role)
        u.set_password(password)
        db.session.add(u)
        db.session.flush()
        membership_role = role
        db.session.add(
            CompetitionMember(
                competition_id=comp_id,
                user_id=u.id,
                role=membership_role,
                active=True,
            )
        )
        db.session.commit()
        flash(_("User '%(user)s' created.", user=username), "success")
        return redirect(url_for("users.list_users"))

    return render_template("user_edit.html", mode="add")

@users_bp.route("/<int:user_id>/edit", methods=["GET", "POST"])
@roles_required("admin")
def edit_user(user_id: int):
    comp_id = get_current_competition_id()
    if not comp_id:
        flash(_("Select a competition first."), "warning")
        return redirect(url_for("main.select_competition"))
    row = (
        db.session.query(User, CompetitionMember)
        .join(CompetitionMember, CompetitionMember.user_id == User.id)
        .filter(
            User.id == user_id,
            CompetitionMember.competition_id == comp_id,
        )
        .first()
    )
    u = row[0] if row else None
    membership = row[1] if row else None
    if not u:
        flash(_("User not found."), "warning")
        return redirect(url_for("users.list_users"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        role = (request.form.get("role") or "viewer").strip()
        new_pw = request.form.get("new_password") or ""
        new_pw2 = request.form.get("confirm_password") or ""

        if not username or role not in ("viewer", "judge", "admin"):
            flash(_("Invalid form data."), "warning")
            return render_template("user_edit.html", mode="edit", u=u, membership=membership)

        # unique username check
        if User.query.filter(User.username == username, User.id != u.id).first():
            flash(_("Another user already has that username."), "warning")
            return render_template("user_edit.html", mode="edit", u=u, membership=membership)

        u.username = username
        u.role = "public" if role == "viewer" else role
        if membership:
            membership.role = role

        # optional password reset
        if new_pw or new_pw2:
            if len(new_pw) < 8:
                flash(_("New password must be at least 8 characters."), "warning")
                return render_template("user_edit.html", mode="edit", u=u, membership=membership)
            if new_pw != new_pw2:
                flash(_("New passwords do not match."), "warning")
                return render_template("user_edit.html", mode="edit", u=u, membership=membership)
            if u.username.lower() in new_pw.lower():
                flash(_("Password should not contain the username."), "warning")
                return render_template("user_edit.html", mode="edit", u=u, membership=membership)
            u.set_password(new_pw)

        db.session.commit()
        flash(_("User updated."), "success")
        return redirect(url_for("users.list_users"))

    return render_template("user_edit.html", mode="edit", u=u, membership=membership)

@users_bp.route("/<int:user_id>/delete", methods=["POST"])
@roles_required("admin")
def delete_user(user_id: int):
    comp_id = get_current_competition_id()
    if not comp_id:
        flash(_("Select a competition first."), "warning")
        return redirect(url_for("main.select_competition"))
    membership = (
        CompetitionMember.query
        .filter(
            CompetitionMember.user_id == user_id,
            CompetitionMember.competition_id == comp_id,
        )
        .first()
    )
    if not membership:
        flash(_("User not found."), "warning")
        return redirect(url_for("users.list_users"))

    db.session.delete(membership)
    db.session.flush()
    remaining = CompetitionMember.query.filter(
        CompetitionMember.user_id == user_id
    ).count()
    if remaining == 0:
        u = User.query.get(user_id)
        if u:
            db.session.delete(u)
    db.session.commit()
    flash(_("User removed."), "success")
    return redirect(url_for("users.list_users"))
