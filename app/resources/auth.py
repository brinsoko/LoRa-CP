# app/resources/auth.py
from __future__ import annotations
from flask import request, jsonify
from flask_restful import Resource
from flask_login import login_user, logout_user, login_required, current_user
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import User
from app.utils.perms import roles_required  # your existing decorator

# ---------- helpers ----------
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


# ---------- resources ----------
class AuthLogin(Resource):
    def post(self):
        data, err_resp, err_code = _json()
        if err_resp:
            return err_resp, err_code

        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        if not username or not password:
            return {"error": "username and password required"}, 400

        user = User.query.filter_by(username=username).first()
        if not user or not user.check_password(password):
            return {"error": "Invalid credentials"}, 401

        login_user(user)
        return {
            "ok": True,
            "user": {"id": user.id, "username": user.username, "role": user.role}
        }, 200


class AuthLogout(Resource):
    method_decorators = [login_required]

    def post(self):
        logout_user()
        return {"ok": True}, 200


class AuthChangePassword(Resource):
    method_decorators = [login_required]

    def post(self):
        data, err_resp, err_code = _json()
        if err_resp:
            return err_resp, err_code

        cur = data.get("current_password") or ""
        new = data.get("new_password") or ""
        new2 = data.get("confirm_password") or ""

        if not current_user.check_password(cur):
            return {"error": "Current password is incorrect"}, 400

        err = _validate_new_password(current_user.username, new, new2)
        if err:
            return {"error": err}, 400

        current_user.set_password(new)
        db.session.commit()
        return {"ok": True}, 200


class UserList(Resource):
    # admin-only for creating/listing users
    method_decorators = [roles_required("admin")]

    def get(self):
        users = User.query.order_by(User.username.asc()).all()
        return {
            "users": [
                {"id": u.id, "username": u.username, "role": u.role}
                for u in users
            ]
        }, 200

    def post(self):
        """
        Create a user (admin).
        Body: { "username": "...", "password": "...", "role": "public|judge|admin" }
        """
        data, err_resp, err_code = _json()
        if err_resp:
            return err_resp, err_code

        username = (data.get("username") or "").strip()
        password = data.get("password") or ""
        role = (data.get("role") or "public").strip()

        if not username or not password or role not in ("public", "judge", "admin"):
            return {"error": "Invalid form data"}, 400
        if User.query.filter_by(username=username).first():
            return {"error": "Username already exists"}, 409

        # reuse same password rules
        err = _validate_new_password(username, password, password)
        if err:
            return {"error": err}, 400

        u = User(username=username, role=role)
        u.set_password(password)
        db.session.add(u)
        try:
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
            return {"error": "Username already exists"}, 409

        return {
            "ok": True,
            "user": {"id": u.id, "username": u.username, "role": u.role}
        }, 201


class UserItem(Resource):
    # admin-only: get/patch/delete a specific user
    method_decorators = [roles_required("admin")]

    def get(self, user_id: int):
        u = User.query.get_or_404(user_id)
        return {"id": u.id, "username": u.username, "role": u.role}, 200

    def patch(self, user_id: int):
        """
        Update username/role and/or reset password (optional).
        Body can include: username, role (public|judge|admin),
        new_password, confirm_password
        """
        u = User.query.get_or_404(user_id)
        data, err_resp, err_code = _json()
        if err_resp:
            return err_resp, err_code

        new_username = (data.get("username") or u.username).strip()
        new_role = (data.get("role") or u.role).strip()

        if new_role not in ("public", "judge", "admin"):
            return {"error": "Invalid role"}, 400

        # enforce unique username if changed
        if new_username != u.username and User.query.filter_by(username=new_username).first():
            return {"error": "Username already exists"}, 409

        u.username = new_username
        u.role = new_role

        npw = data.get("new_password")
        cpw = data.get("confirm_password")
        if npw or cpw:
            err = _validate_new_password(new_username, npw or "", cpw or "")
            if err:
                return {"error": err}, 400
            u.set_password(npw)

        db.session.commit()
        return {"ok": True, "user": {"id": u.id, "username": u.username, "role": u.role}}, 200

    def delete(self, user_id: int):
        u = User.query.get_or_404(user_id)
        db.session.delete(u)
        db.session.commit()
        return {"ok": True}, 200


# optional: self-service fetch/update for the current user
class Me(Resource):
    method_decorators = [login_required]

    def get(self):
        u = current_user
        return {"id": u.id, "username": u.username, "role": u.role}, 200