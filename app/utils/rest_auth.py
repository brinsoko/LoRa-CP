# app/utils/rest_auth.py
from flask_login import current_user
from functools import wraps
from app.utils.competition import get_current_competition_role, require_current_competition_id

def json_login_required(fn):
    """Like @login_required but returns JSON 401 instead of redirect."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return {"error": "unauthorized"}, 401
        return fn(*args, **kwargs)
    return wrapper

def json_roles_required(*roles):
    """Role gate for REST: JSON 403 on failure."""
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            if not current_user.is_authenticated:
                return {"error": "unauthorized"}, 401
            comp_id = require_current_competition_id()
            if not comp_id:
                return {"error": "no_competition"}, 400
            role = get_current_competition_role()
            if role not in roles:
                return {"error": "forbidden", "required": roles}, 403
            return fn(*args, **kwargs)
        return wrapper
    return deco
