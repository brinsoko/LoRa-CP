# app/utils/rest_auth.py
from functools import wraps

from flask import jsonify
from flask_login import current_user

from app.utils.competition import get_current_competition_role, require_current_competition_id


def _current_role_set():
    """See app/utils/perms.py:_current_role_set for rationale.
    Only the per-competition role counts; the global User.role is
    used solely for the "superadmin" system bypass."""
    roles = set()
    comp_role = (get_current_competition_role() or "").strip().lower()
    if comp_role:
        roles.add(comp_role)
    global_role = (getattr(current_user, "role", None) or "").strip().lower()
    if global_role == "superadmin":
        roles.update({"superadmin", "admin", "judge", "viewer"})
    return roles


def json_login_required(fn):
    """Like @login_required but returns JSON 401 instead of redirect."""

    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({"error": "unauthorized", "code": 401}), 401
        return fn(*args, **kwargs)

    return wrapper


def json_roles_required(*roles):
    """Role gate for REST: JSON 403 on failure."""
    allowed = {(r or "").strip().lower() for r in roles}

    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                return jsonify({"error": "unauthorized", "code": 401}), 401
            comp_id = require_current_competition_id()
            if not comp_id:
                return jsonify({"error": "no_competition", "code": 400}), 400
            role_set = _current_role_set()
            if not (role_set & allowed):
                return jsonify({"error": "forbidden", "code": 403, "required": list(roles)}), 403
            return fn(*args, **kwargs)

        return wrapper

    return deco
