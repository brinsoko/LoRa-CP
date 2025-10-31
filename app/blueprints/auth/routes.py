# app/blueprints/auth/routes.py
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user, login_user, logout_user

from app.models import User
from app.utils.frontend_api import api_json
from app.utils.perms import roles_required


auth_bp = Blueprint("auth", __name__)


def _validate_new_password(username: str, pw1: str, pw2: str) -> str | None:
    if not pw1 or not pw2:
        return "Please fill in all fields."
    if pw1 != pw2:
        return "New passwords do not match."
    if len(pw1) < 8:
        return "New password must be at least 8 characters."
    if username.lower() in pw1.lower():
        return "Password should not contain your username."
    return None


@auth_bp.route("/login", methods=["GET", "POST"])
def login():  # endpoint: auth.login
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        resp, payload = api_json(
            "POST",
            "/api/auth/login",
            json={"username": username, "password": password},
        )

        if resp.status_code == 200:
            user_info = payload.get("user") or {}
            user_obj = User.query.get(user_info.get("id")) if user_info.get("id") else None
            if user_obj:
                login_user(user_obj)
            flash("Signed in.", "success")
            return redirect(request.args.get("next") or url_for("main.index"))

        flash(payload.get("error") or "Invalid username or password.", "warning")

    return render_template("login.html")


@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():  # endpoint: auth.logout
    api_json("POST", "/api/auth/logout")
    logout_user()
    flash("Signed out.", "success")
    return redirect(url_for("main.index"))


@auth_bp.route("/register", methods=["GET", "POST"])
@roles_required("admin")
def register():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        role = (request.form.get("role") or "public").strip()

        if not username or not password or role not in ("public", "judge", "admin"):
            flash("Invalid form data.", "warning")
            return render_template("register.html")

        resp, payload = api_json(
            "POST",
            "/api/users",
            json={"username": username, "password": password, "role": role},
        )

        if resp.status_code == 201:
            flash(f"User '{username}' created with role '{role}'.", "success")
            return redirect(url_for("main.index"))

        flash(payload.get("error") or "Could not create user.", "warning")

    return render_template("register.html")


@auth_bp.route("/change_password", methods=["GET", "POST"])
@login_required
def change_password():
    if request.method == "POST":
        cur = request.form.get("current_password") or ""
        new = request.form.get("new_password") or ""
        new2 = request.form.get("confirm_password") or ""

        err = _validate_new_password(current_user.username, new, new2)
        if err:
            flash(err, "warning")
            return render_template("change_password.html")

        resp, payload = api_json(
            "POST",
            "/api/auth/password",
            json={
                "current_password": cur,
                "new_password": new,
                "confirm_password": new2,
            },
        )

        if resp.status_code == 200:
            flash("Password changed successfully.", "success")
            return redirect(url_for("main.index"))

        flash(payload.get("error") or "Could not change password.", "warning")

    return render_template("change_password.html")


@auth_bp.route("/create_admin", methods=["GET", "POST"])
@roles_required("admin")
def create_admin():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        if not username or not password:
            flash("Username and password are required.", "warning")
            return render_template("create_admin.html")

        resp, payload = api_json(
            "POST",
            "/api/users",
            json={"username": username, "password": password, "role": "admin"},
        )

        if resp.status_code == 201:
            flash(f"Admin user '{username}' created.", "success")
            return redirect(url_for("auth.login"))

        flash(payload.get("error") or "Could not create admin user.", "warning")

    return render_template("create_admin.html")
