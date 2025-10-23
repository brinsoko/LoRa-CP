# app/blueprints/users/routes.py
from __future__ import annotations
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required
from app.extensions import db
from app.models import User
from app.utils.perms import roles_required  # already in your project

users_bp = Blueprint("users", __name__, template_folder="../../templates")

@users_bp.route("/", methods=["GET"])
@roles_required("admin")
def list_users():
    users = User.query.order_by(User.username.asc()).all()
    return render_template("users_list.html", users=users)

@users_bp.route("/add", methods=["GET", "POST"])
@roles_required("admin")
def add_user():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        role = (request.form.get("role") or "public").strip()

        if not username or not password or role not in ("public", "judge", "admin"):
            flash("Please fill all fields. Role must be public/judge/admin.", "warning")
            return render_template("user_edit.html", mode="add")

        if User.query.filter_by(username=username).first():
            flash("Username already exists.", "warning")
            return render_template("user_edit.html", mode="add")

        u = User(username=username, role=role)
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        flash(f"User '{username}' created.", "success")
        return redirect(url_for("users.list_users"))

    return render_template("user_edit.html", mode="add")

@users_bp.route("/<int:user_id>/edit", methods=["GET", "POST"])
@roles_required("admin")
def edit_user(user_id: int):
    u = User.query.get_or_404(user_id)

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        role = (request.form.get("role") or "public").strip()
        new_pw = request.form.get("new_password") or ""
        new_pw2 = request.form.get("confirm_password") or ""

        if not username or role not in ("public", "judge", "admin"):
            flash("Invalid form data.", "warning")
            return render_template("user_edit.html", mode="edit", u=u)

        # unique username check
        if User.query.filter(User.username == username, User.id != u.id).first():
            flash("Another user already has that username.", "warning")
            return render_template("user_edit.html", mode="edit", u=u)

        u.username = username
        u.role = role

        # optional password reset
        if new_pw or new_pw2:
            if len(new_pw) < 8:
                flash("New password must be at least 8 characters.", "warning")
                return render_template("user_edit.html", mode="edit", u=u)
            if new_pw != new_pw2:
                flash("New passwords do not match.", "warning")
                return render_template("user_edit.html", mode="edit", u=u)
            if u.username.lower() in new_pw.lower():
                flash("Password should not contain the username.", "warning")
                return render_template("user_edit.html", mode="edit", u=u)
            u.set_password(new_pw)

        db.session.commit()
        flash("User updated.", "success")
        return redirect(url_for("users.list_users"))

    return render_template("user_edit.html", mode="edit", u=u)

@users_bp.route("/<int:user_id>/delete", methods=["POST"])
@roles_required("admin")
def delete_user(user_id: int):
    u = User.query.get_or_404(user_id)
    db.session.delete(u)
    db.session.commit()
    flash("User deleted.", "success")
    return redirect(url_for("users.list_users"))