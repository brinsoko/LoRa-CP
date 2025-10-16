# app/blueprints/auth/routes.py
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_user, logout_user, login_required, current_user
from app.extensions import db
from app.models import User
from app.utils.perms import roles_required

auth_bp = Blueprint("auth", __name__)  # SINGLE blueprint definition

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

# -------- Login / Logout --------
@auth_bp.route("/login", methods=["GET", "POST"])
def login():  # endpoint: auth.login
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        user = User.query.filter_by(username=username).first()
        if user and user.check_password(password):
            login_user(user)
            flash("Signed in.", "success")
            return redirect(request.args.get("next") or url_for("main.index"))
        flash("Invalid username or password.", "warning")
    return render_template("login.html")

@auth_bp.route("/logout", methods=["POST"])
@login_required
def logout():  # endpoint: auth.logout
    logout_user()
    flash("Signed out.", "success")
    return redirect(url_for("main.index"))

# -------- Register (admin only) --------
@auth_bp.route("/register", methods=["GET","POST"])
@roles_required("admin")
def register():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        role = (request.form.get("role") or "public").strip()
        if not username or not password or role not in ("public", "judge", "admin"):
            flash("Invalid form data.", "warning")
            return render_template("register.html")
        if User.query.filter_by(username=username).first():
            flash("Username already exists.", "warning")
            return render_template("register.html")
        u = User(username=username, role=role)
        u.set_password(password)
        db.session.add(u)
        db.session.commit()
        flash(f"User '{username}' created with role '{role}'.", "success")
        return redirect(url_for("main.index"))
    return render_template("register.html")

# -------- Change password (self) --------
@auth_bp.route("/change_password", methods=["GET","POST"])
@login_required
def change_password():
    if request.method == "POST":
        cur = request.form.get("current_password") or ""
        new = request.form.get("new_password") or ""
        new2 = request.form.get("confirm_password") or ""
        if not current_user.check_password(cur):
            flash("Current password is incorrect.", "warning")
            return render_template("change_password.html")
        err = _validate_new_password(current_user.username, new, new2)
        if err:
            flash(err, "warning")
            return render_template("change_password.html")
        current_user.set_password(new)
        db.session.commit()
        flash("Password changed successfully.", "success")
        return redirect(url_for("main.index"))
    return render_template("change_password.html")

# -------- Create admin (admin-only normal path) --------
@auth_bp.route("/create_admin", methods=["GET", "POST"])
@roles_required("admin")  # use our roles helper; current_user.has_role() doesn't exist
def create_admin():
    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""
        if not username or not password:
            flash("Username and password are required.", "warning")
            return render_template("create_admin.html")
        if User.query.filter_by(username=username).first():
            flash("That username already exists.", "warning")
            return render_template("create_admin.html")
        admin = User(username=username, role="admin")
        admin.set_password(password)
        db.session.add(admin)
        db.session.commit()
        flash(f"Admin user '{username}' created.", "success")
        return redirect(url_for("auth.login"))
    return render_template("create_admin.html")