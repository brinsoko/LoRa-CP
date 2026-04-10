from __future__ import annotations

from flask import Blueprint, jsonify, request
from flask_login import current_user, login_user, logout_user
from sqlalchemy.exc import IntegrityError

from app.api.helpers import json_ok
from app.extensions import db
from app.models import CompetitionMember, User
from app.utils.competition import require_current_competition_id
from app.utils.rest_auth import json_login_required, json_roles_required
from app.utils.validators import validate_email, validate_username

auth_api_bp = Blueprint("api_auth", __name__)


def _find_login_user(login_id: str) -> User | None:
    login_id = (login_id or "").strip()
    if not login_id:
        return None

    user = User.query.filter_by(username=login_id).first()
    if user:
        return user
    return User.query.filter_by(email=login_id.lower()).first()


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


def _json():
    if not request.is_json:
        return None, jsonify({"error": "Content-Type must be application/json"}), 415
    data = request.get_json(silent=True)
    if data is None:
        return None, jsonify({"error": "Malformed JSON"}), 400
    return data, None, None


@auth_api_bp.post("/api/auth/login")
def auth_login():
    data, err_resp, err_code = _json()
    if err_resp:
        return err_resp, err_code

    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if not username or not password:
        return jsonify({"error": "username and password required"}), 400

    user = _find_login_user(username)
    if not user or not user.check_password(password):
        return jsonify({"error": "Invalid credentials"}), 401

    login_user(user)
    return json_ok(
        {
            "ok": True,
            "user": {"id": user.id, "username": user.username, "role": user.role},
        }
    )


@auth_api_bp.post("/api/auth/logout")
@json_login_required
def auth_logout():
    logout_user()
    return json_ok({"ok": True})


@auth_api_bp.post("/api/auth/password")
@json_login_required
def auth_change_password():
    data, err_resp, err_code = _json()
    if err_resp:
        return err_resp, err_code

    if current_user.google_sub:
        return jsonify({"error": "Password changes are disabled for Google accounts"}), 403

    cur = data.get("current_password") or ""
    new = data.get("new_password") or ""
    new2 = data.get("confirm_password") or ""

    if not current_user.check_password(cur):
        return jsonify({"error": "Current password is incorrect"}), 400

    err = _validate_new_password(current_user.username, new, new2)
    if err:
        return jsonify({"error": err}), 400

    current_user.set_password(new)
    db.session.commit()
    return json_ok({"ok": True})


@auth_api_bp.get("/api/auth/me")
@json_login_required
def auth_me():
    u = current_user
    return json_ok({"id": u.id, "username": u.username, "role": u.role})


@auth_api_bp.get("/api/users")
@json_roles_required("admin")
def users_list():
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400

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
    return json_ok(
        {
            "users": [
                {
                    "id": u.id,
                    "username": u.username,
                    "role": u.role,
                    "membership_role": m.role,
                }
                for u, m in rows
            ]
        }
    )


@auth_api_bp.post("/api/users")
@json_roles_required("admin")
def users_create():
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    data, err_resp, err_code = _json()
    if err_resp:
        return err_resp, err_code

    username, username_error = validate_username(data.get("username"))
    password = data.get("password") or ""
    role = (data.get("role") or "viewer").strip()
    email, email_error = validate_email(data.get("email"))

    if username_error:
        return jsonify({"error": username_error}), 400
    if email_error:
        return jsonify({"error": email_error}), 400
    if not password or role not in ("viewer", "judge", "admin"):
        return jsonify({"error": "Invalid form data"}), 400
    if User.query.filter_by(username=username).first():
        return jsonify({"error": "Username already exists"}), 409
    if email and User.query.filter_by(email=email).first():
        return jsonify({"error": "Email already exists"}), 409

    err = _validate_new_password(username, password, password)
    if err:
        return jsonify({"error": err}), 400

    user_role = "public" if role == "viewer" else role
    u = User(username=username, role=user_role, email=email)
    u.set_password(password)
    db.session.add(u)
    db.session.flush()
    db.session.add(
        CompetitionMember(
            competition_id=comp_id,
            user_id=u.id,
            role=role,
            active=True,
        )
    )
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        return jsonify({"error": "Username already exists"}), 409

    return json_ok(
        {
            "ok": True,
            "user": {"id": u.id, "username": u.username, "role": u.role},
        },
        status=201,
    )


def _competition_user(comp_id: int, user_id: int) -> User | None:
    return (
        User.query.join(CompetitionMember, CompetitionMember.user_id == User.id)
        .filter(
            User.id == user_id,
            CompetitionMember.competition_id == comp_id,
        )
        .first()
    )


@auth_api_bp.get("/api/users/<int:user_id>")
@json_roles_required("admin")
def users_get(user_id: int):
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    u = _competition_user(comp_id, user_id)
    if not u:
        return jsonify({"error": "not_found"}), 404
    return json_ok({"id": u.id, "username": u.username, "role": u.role})


@auth_api_bp.patch("/api/users/<int:user_id>")
@json_roles_required("admin")
def users_patch(user_id: int):
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    u = _competition_user(comp_id, user_id)
    if not u:
        return jsonify({"error": "not_found"}), 404
    data, err_resp, err_code = _json()
    if err_resp:
        return err_resp, err_code

    new_username, username_error = validate_username(data.get("username") or u.username)
    membership = CompetitionMember.query.filter(
        CompetitionMember.user_id == u.id,
        CompetitionMember.competition_id == comp_id,
    ).first()
    current_role = membership.role if membership else "viewer"
    new_role = (data.get("role") or current_role).strip()

    if new_role not in ("viewer", "judge", "admin"):
        return jsonify({"error": "Invalid role"}), 400
    if username_error:
        return jsonify({"error": username_error}), 400
    if new_username != u.username and User.query.filter_by(username=new_username).first():
        return jsonify({"error": "Username already exists"}), 409

    u.username = new_username
    u.role = "public" if new_role == "viewer" else new_role
    if membership:
        membership.role = new_role

    npw = data.get("new_password")
    cpw = data.get("confirm_password")
    if npw or cpw:
        err = _validate_new_password(new_username, npw or "", cpw or "")
        if err:
            return jsonify({"error": err}), 400
        u.set_password(npw)

    db.session.commit()
    return json_ok({"ok": True, "user": {"id": u.id, "username": u.username, "role": u.role}})


@auth_api_bp.delete("/api/users/<int:user_id>")
@json_roles_required("admin")
def users_delete(user_id: int):
    comp_id = require_current_competition_id()
    if not comp_id:
        return jsonify({"error": "no_competition"}), 400
    membership = CompetitionMember.query.filter(
        CompetitionMember.user_id == user_id,
        CompetitionMember.competition_id == comp_id,
    ).first()
    if not membership:
        return jsonify({"error": "not_found"}), 404

    db.session.delete(membership)
    db.session.flush()
    remaining = CompetitionMember.query.filter(CompetitionMember.user_id == user_id).count()
    if remaining == 0:
        u = db.session.get(User, user_id)
        if u:
            db.session.delete(u)
    db.session.commit()
    return json_ok({"ok": True})
