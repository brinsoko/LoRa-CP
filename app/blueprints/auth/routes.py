# app/blueprints/auth/routes.py
from flask import Blueprint, render_template, request, redirect, url_for, flash, session, current_app
from flask_login import login_required, current_user, login_user, logout_user
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
import requests
import secrets
import re
from datetime import datetime

from app.models import User, CompetitionInvite, CompetitionMember
from app.extensions import db
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


def _normalize_username(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "", (value or "").strip())
    return cleaned or "user"


def _accept_pending_invites(user: User) -> None:
    if not user.email:
        return
    now = datetime.utcnow()
    invites = (
        CompetitionInvite.query
        .filter(
            CompetitionInvite.invited_email.ilike(user.email),
            CompetitionInvite.used_at.is_(None),
            CompetitionInvite.expires_at > now,
        )
        .all()
    )
    if not invites:
        return

    existing_memberships = {
        m.competition_id
        for m in CompetitionMember.query
        .filter(CompetitionMember.user_id == user.id)
        .all()
    }
    for invite in invites:
        if invite.competition_id not in existing_memberships:
            db.session.add(
                CompetitionMember(
                    competition_id=invite.competition_id,
                    user_id=user.id,
                    role=invite.role,
                    active=True,
                )
            )
        invite.invited_user_id = user.id
        invite.used_at = now
    db.session.commit()


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
                _accept_pending_invites(user_obj)
                login_user(user_obj)
            flash("Signed in.", "success")
            return redirect(request.args.get("next") or url_for("main.select_competition"))

        flash(payload.get("error") or "Invalid username or password.", "warning")

    google_client_id = current_app.config.get("GOOGLE_OAUTH_CLIENT_ID")
    google_enabled = bool(google_client_id)
    return render_template("login.html", google_enabled=google_enabled)


@auth_bp.route("/login/google")
def login_google():
    client_id = current_app.config.get("GOOGLE_OAUTH_CLIENT_ID")
    if not client_id:
        flash("Google OAuth is not configured.", "warning")
        return redirect(url_for("auth.login"))
    state = secrets.token_urlsafe(16)
    session["google_oauth_state"] = state
    session["google_oauth_next"] = request.args.get("next") or ""
    redirect_uri = url_for("auth.login_google_callback", _external=True)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth"
    return redirect(f"{auth_url}?{requests.compat.urlencode(params)}")


@auth_bp.route("/login/google/callback")
def login_google_callback():
    client_id = current_app.config.get("GOOGLE_OAUTH_CLIENT_ID")
    client_secret = current_app.config.get("GOOGLE_OAUTH_CLIENT_SECRET")
    if not client_id or not client_secret:
        flash("Google OAuth is not configured.", "warning")
        return redirect(url_for("auth.login"))

    state = request.args.get("state")
    if not state or state != session.get("google_oauth_state"):
        flash("Invalid OAuth state.", "warning")
        return redirect(url_for("auth.login"))

    code = request.args.get("code")
    if not code:
        flash("Google OAuth failed to return a code.", "warning")
        return redirect(url_for("auth.login"))

    token_resp = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": url_for("auth.login_google_callback", _external=True),
            "grant_type": "authorization_code",
        },
        timeout=10,
    )
    if token_resp.status_code != 200:
        flash("Google OAuth token exchange failed.", "warning")
        return redirect(url_for("auth.login"))

    tokens = token_resp.json() or {}
    raw_id_token = tokens.get("id_token")
    if not raw_id_token:
        flash("Google OAuth did not return an id_token.", "warning")
        return redirect(url_for("auth.login"))

    try:
        id_info = id_token.verify_oauth2_token(
            raw_id_token,
            google_requests.Request(),
            client_id,
        )
    except Exception as exc:
        current_app.logger.exception("Google OAuth token verification failed: %s", exc)
        if current_app.debug:
            flash(f"Google OAuth token verification failed: {exc}", "warning")
        else:
            flash("Google OAuth token verification failed.", "warning")
        return redirect(url_for("auth.login"))

    sub = id_info.get("sub")
    email = (id_info.get("email") or "").strip().lower()
    if not sub:
        flash("Google OAuth user info missing.", "warning")
        return redirect(url_for("auth.login"))

    user = User.query.filter_by(google_sub=sub).first()
    if not user and email:
        user = User.query.filter_by(email=email).first()
        if user and not user.google_sub:
            user.google_sub = sub
            if not user.email:
                user.email = email
    if not user:
        if not email:
            flash("Google account did not provide an email.", "warning")
            return redirect(url_for("auth.login"))
        base_username = _normalize_username(email.split("@")[0])
        username = base_username
        suffix = 1
        while User.query.filter_by(username=username).first():
            suffix += 1
            username = f"{base_username}{suffix}"
        user = User(username=username, role="public", google_sub=sub, email=email)
        user.set_password(secrets.token_urlsafe(32))
        db.session.add(user)
        db.session.commit()
        flash("Account created via Google.", "success")

    if not user.email and email:
        user.email = email

    _accept_pending_invites(user)
    try:
        login_user(user)
    finally:
        session.pop("google_oauth_state", None)
        next_url = session.pop("google_oauth_next", None) or ""

    flash("Signed in.", "success")
    return redirect(next_url or url_for("main.select_competition"))


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
        email = (request.form.get("email") or "").strip().lower() or None
        password = request.form.get("password") or ""
        role = (request.form.get("role") or "viewer").strip()

        if not username or not password or role not in ("viewer", "judge", "admin"):
            flash("Invalid form data.", "warning")
            return render_template("register.html")

        resp, payload = api_json(
            "POST",
            "/api/users",
            json={"username": username, "password": password, "role": role, "email": email},
        )

        if resp.status_code == 201:
            flash(f"User '{username}' created with role '{role}'.", "success")
            return redirect(url_for("main.select_competition"))

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
        return redirect(url_for("main.select_competition"))

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
