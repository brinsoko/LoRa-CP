from functools import wraps
from flask import abort, redirect, request, url_for
from flask_login import current_user

def has_role(*roles: str) -> bool:
    if not current_user.is_authenticated:
        return False
    # normalize stored role defensively
    user_role = (current_user.role or "public").strip().lower()
    return user_role in {r.strip().lower() for r in roles}

def roles_required(*roles: str):
    def wrapper(view):
        @wraps(view)
        def inner(*args, **kwargs):
            if not current_user.is_authenticated:
                return redirect(url_for("auth.login", next=request.url))
            if not has_role(*roles):
                abort(403)
            return view(*args, **kwargs)
        return inner
    return wrapper

def inject_perms():
    # exposes has_role() to Jinja: {% if has_role('judge','admin') %}...
    return dict(has_role=has_role)