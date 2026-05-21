"""Superadmin console: cross-competition admin views.

Distinct from the per-competition admin role - this is gated on
User.role == "superadmin" (the system-level role) and is meant for the
operator running the LoRa-CP installation, not race-day organizers.

Initial views:
  /superadmin/                       landing page with all-users table +
                                      live Sheets quota indicator
  /superadmin/sheets-status.json     JSON snapshot of the SheetsClient
                                      throttle window for the JS poller

Future views can hang off this blueprint without re-doing the role-check
plumbing.
"""

from __future__ import annotations

from functools import wraps

from flask import Blueprint, abort, current_app, jsonify, render_template
from flask_login import current_user, login_required

from app.models import User

superadmin_bp = Blueprint(
    "superadmin",
    __name__,
    template_folder="../../templates",
)


def _superadmin_only(view):
    """Reject anyone without User.role == 'superadmin'.

    Anonymous users get redirected by login_required first; authenticated
    non-superadmins get a 403 rather than a redirect so the failure is
    explicit (a redirect would silently send them to /login again).
    """

    @wraps(view)
    @login_required
    def wrapper(*args, **kwargs):
        role = getattr(current_user, "role", None)
        if role != "superadmin":
            abort(403)
        return view(*args, **kwargs)

    return wrapper


@superadmin_bp.route("/", methods=["GET"])
@_superadmin_only
def index():
    users = User.query.order_by(User.role.desc(), User.username.asc()).all()
    return render_template("superadmin_index.html", users=users)


@superadmin_bp.route("/sheets-status.json", methods=["GET"])
@_superadmin_only
def sheets_status():
    """Return the current SheetsClient throttle-window snapshot.

    Polled every ~2 seconds by the quota indicator widget on the console
    landing page. If no Sheets client has been initialized yet (because
    no Sheets call has happened in this process), we report a zeroed
    "idle" state instead of failing - the widget renders 0/40.
    """
    client = current_app.extensions.get("sheets_client")
    if client is None:
        return jsonify(
            {
                "used": 0,
                "limit": 40,
                "window_seconds": 60,
                "elapsed_seconds": 0.0,
                "remaining_seconds": 60.0,
                "client_initialized": False,
            }
        )
    status = client.get_window_status()
    status["client_initialized"] = True
    return jsonify(status)
