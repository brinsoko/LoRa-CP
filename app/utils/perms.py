# app/utils/perms.py
from functools import wraps
from flask import request, redirect, url_for, abort, current_app
from flask_login import current_user
from app.utils.competition import get_current_competition_role

def _current_role_set():
    roles = set()
    comp_role = (get_current_competition_role() or "").strip().lower()
    global_role = (getattr(current_user, "role", None) or "").strip().lower()
    if comp_role:
        roles.add(comp_role)
    if global_role:
        roles.add(global_role)
    return roles

def roles_required(*roles):
    """
    If NOT authenticated -> redirect to login (?next=...).
    If authenticated but role not allowed -> 403.
    Case-insensitive compare; trims whitespace. Emits debug logs.
    """
    allowed = {(r or "").strip().lower() for r in roles}

    def decorator(view):
        @wraps(view)
        def wrapped(*args, **kwargs):
            if not current_user.is_authenticated:
                current_app.logger.debug(
                    "[roles_required] redirect → login: endpoint=%s next=%s",
                    request.endpoint, request.url
                )
                return redirect(url_for("auth.login", next=request.url))

            role_set = _current_role_set()
            user_role = ", ".join(sorted(role_set))
            if allowed and not role_set:
                return redirect(url_for("main.select_competition"))
            ok = (not allowed) or bool(role_set & allowed)

            current_app.logger.debug(
                "[roles_required] user=%r role=%r allowed=%r ok=%s endpoint=%s path=%s",
                getattr(current_user, "username", None),
                user_role, allowed, ok, request.endpoint, request.path
            )

            if not ok:
                abort(403)
            return view(*args, **kwargs)
        return wrapped
    return decorator

def inject_perms():
    """In templates: {{ has_role('admin') }} (case-insensitive)."""
    def has_role(*roles):
        if not current_user.is_authenticated:
            return False
        allowed = {(r or "").strip().lower() for r in roles}
        return (not allowed) or bool(_current_role_set() & allowed)
    return dict(has_role=has_role)
