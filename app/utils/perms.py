# app/utils/perms.py
from functools import wraps

from flask import abort, current_app, redirect, request, url_for
from flask_login import current_user

from app.utils.competition import get_current_competition_role


def _current_role_set():
    """Roles in scope for the *currently selected* competition.

    Only the user's CompetitionMember.role for the active competition counts.
    The global User.role field is intentionally NOT unioned in here — that
    field is reserved for the system-level "superadmin" role, which is
    handled as an explicit bypass below. Doing the union allowed roles to
    leak across competitions (e.g. an admin in one comp passing admin
    gates in another)."""
    roles = set()
    comp_role = (get_current_competition_role() or "").strip().lower()
    if comp_role:
        roles.add(comp_role)
    # Superadmin is a system-level bypass: it satisfies any per-competition
    # role gate without requiring a CompetitionMember row.
    global_role = (getattr(current_user, "role", None) or "").strip().lower()
    if global_role == "superadmin":
        roles.update({"superadmin", "admin", "judge", "viewer"})
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
                    "[roles_required] redirect → login: endpoint=%s next=%s", request.endpoint, request.url
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
                user_role,
                allowed,
                ok,
                request.endpoint,
                request.path,
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
