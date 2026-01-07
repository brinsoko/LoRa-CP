# app/utils/perms.py
from functools import wraps
from flask import request, redirect, url_for, abort, current_app
from flask_login import current_user
from app.utils.competition import get_current_competition_role

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

            comp_role = get_current_competition_role()
            user_role = (comp_role or "").strip().lower()
            if allowed and not comp_role:
                return redirect(url_for("main.select_competition"))
            ok = (not allowed) or (user_role in allowed)

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
        user_role = (get_current_competition_role() or "").strip().lower()
        allowed = {(r or "").strip().lower() for r in roles}
        return (not allowed) or (user_role in allowed)
    return dict(has_role=has_role)
