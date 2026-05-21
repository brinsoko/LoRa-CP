"""Superadmin console: cross-competition admin views.

Distinct from the per-competition admin role - this is gated on
User.role == "superadmin" (the system-level role) and is meant for the
operator running the LoRa-CP installation, not race-day organizers.

Views:
  /superadmin/                       landing page with all-users table +
                                      live Sheets quota indicator
  /superadmin/sheets-status.json     JSON snapshot of the SheetsClient
                                      throttle window for the JS poller
  /superadmin/users/bulk-add         bulk-create users (auto-generated
                                      passwords, displayed once)
  /superadmin/users/<id>/delete      hard-delete a user (own account
                                      blocked: the only superadmin
                                      cannot be deleted by themselves)

Competition deletion lives on the per-competition settings page's
Danger Zone (main.delete_competition) — the destructive action stays
co-located with the thing being destroyed.

Future views can hang off this blueprint without re-doing the role-check
plumbing.
"""

from __future__ import annotations

import re
import secrets
from functools import wraps

from flask import (
    Blueprint,
    abort,
    current_app,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    url_for,
)
from flask_babel import gettext as _
from flask_login import current_user, login_required

from app.extensions import db
from app.models import Competition, CompetitionMember, User

superadmin_bp = Blueprint(
    "superadmin",
    __name__,
    template_folder="../../templates",
)

# Mirrors the regex used in users.routes for consistency. Per CLAUDE.md,
# User.role is reserved for "superadmin" / "public" (system-level only);
# per-competition roles (judge/admin/viewer) live on CompetitionMember,
# so bulk-add creates the User with role="public" and writes the chosen
# per-comp role onto the CompetitionMember row.
_USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,50}$")
_VALID_MEMBERSHIP_ROLES = ("viewer", "judge", "admin")


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


def _generate_password() -> str:
    # ~16 chars of url-safe base64 randomness. Plenty for a one-shot
    # operator-issued password; users are expected to change it on first
    # login if we ever add that flow.
    return secrets.token_urlsafe(12)


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


@superadmin_bp.route("/users/bulk-add", methods=["GET", "POST"])
@_superadmin_only
def bulk_add_users():
    if request.method == "GET":
        competitions = Competition.query.order_by(Competition.name.asc()).all()
        return render_template("superadmin_bulk_add.html", competitions=competitions)

    raw = request.form.get("usernames") or ""
    role = (request.form.get("role") or "").strip().lower()
    if role not in _VALID_MEMBERSHIP_ROLES:
        flash(_("Invalid role."), "warning")
        return redirect(url_for("superadmin.bulk_add_users"))

    try:
        competition_id = int(request.form.get("competition_id") or 0)
    except (TypeError, ValueError):
        competition_id = 0
    competition = db.session.get(Competition, competition_id) if competition_id else None
    if not competition:
        flash(_("Pick a competition to attach the new users to."), "warning")
        return redirect(url_for("superadmin.bulk_add_users"))

    seen: set[str] = set()
    usernames: list[str] = []
    invalid: list[str] = []
    dupes: list[str] = []
    for line in raw.splitlines():
        name = line.strip()
        if not name:
            continue
        if not _USERNAME_RE.fullmatch(name):
            invalid.append(name)
            continue
        if name in seen:
            dupes.append(name)
            continue
        seen.add(name)
        usernames.append(name)

    if invalid:
        flash(
            _("Invalid usernames (skipped): %(list)s", list=", ".join(invalid[:20])),
            "warning",
        )
    if dupes:
        flash(
            _("Duplicate usernames in input (skipped): %(list)s", list=", ".join(dupes[:20])),
            "warning",
        )

    if not usernames:
        flash(_("Provide at least one valid username."), "warning")
        return redirect(url_for("superadmin.bulk_add_users"))

    existing_users = {u.username: u for u in User.query.filter(User.username.in_(usernames)).all()}

    # For users that already exist, attach them to the chosen competition
    # if they aren't already a member (and report which were re-used vs
    # created fresh).
    created: list[tuple[str, str]] = []
    attached_existing: list[str] = []
    already_in_comp: list[str] = []

    for username in usernames:
        u = existing_users.get(username)
        if u is None:
            pw = _generate_password()
            # System-level role stays public — per CLAUDE.md, per-comp
            # roles never leak onto User.role.
            u = User(username=username, role="public")
            u.set_password(pw)
            db.session.add(u)
            db.session.flush()
            created.append((username, pw))
        else:
            # Check whether they're already in the target competition.
            existing_member = CompetitionMember.query.filter(
                CompetitionMember.user_id == u.id,
                CompetitionMember.competition_id == competition.id,
            ).first()
            if existing_member and existing_member.active:
                already_in_comp.append(username)
                continue
            attached_existing.append(username)
            if existing_member:
                existing_member.active = True
                existing_member.role = role
                continue

        db.session.add(
            CompetitionMember(
                competition_id=competition.id,
                user_id=u.id,
                role=role,
                active=True,
            )
        )

    db.session.commit()

    if already_in_comp:
        flash(
            _(
                "Already in %(comp)s (skipped): %(list)s",
                comp=competition.name,
                list=", ".join(already_in_comp[:20]),
            ),
            "info",
        )
    if attached_existing:
        flash(
            _(
                "Existing user(s) attached to %(comp)s as %(role)s: %(list)s",
                comp=competition.name,
                role=role,
                list=", ".join(attached_existing[:20]),
            ),
            "info",
        )

    current_app.logger.info(
        "superadmin %s bulk-created %d new user(s) and attached %d existing user(s) "
        "to competition %s as %s",
        getattr(current_user, "username", "?"),
        len(created),
        len(attached_existing),
        competition.name,
        role,
    )

    if not created:
        return redirect(url_for("superadmin.bulk_add_users"))

    return render_template(
        "superadmin_bulk_results.html",
        created=created,
        role=role,
        competition=competition,
    )


@superadmin_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@_superadmin_only
def delete_user(user_id: int):
    # Self-delete guard: a superadmin can delete anyone else, including
    # other superadmins, but cannot remove themselves. In a single-
    # superadmin install this also serves as the last-superadmin guard.
    if user_id == current_user.id:
        flash(_("You cannot delete your own account."), "warning")
        return redirect(url_for("superadmin.index"))

    user = db.session.get(User, user_id)
    if not user:
        flash(_("User not found."), "warning")
        return redirect(url_for("superadmin.index"))

    username = user.username
    role = user.role
    db.session.delete(user)
    db.session.commit()

    current_app.logger.info(
        "superadmin %s deleted user id=%d username=%s role=%s",
        getattr(current_user, "username", "?"),
        user_id,
        username,
        role,
    )
    flash(_("Deleted user '%(user)s'.", user=username), "success")
    return redirect(url_for("superadmin.index"))
