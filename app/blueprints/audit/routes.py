from __future__ import annotations

import re
from datetime import datetime, timedelta

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _
from sqlalchemy.orm import joinedload

from app.models import AuditEvent
from app.utils.audit import actor_label_for
from app.utils.competition import get_current_competition_id
from app.utils.perms import roles_required

audit_bp = Blueprint("audit", __name__, template_folder="../../templates")

_KEY_RE = re.compile(r"^[a-z_]{1,64}$")


def _sanitize_key(raw_value: str | None) -> str | None:
    value = (raw_value or "").strip().lower()
    if not value:
        return None
    if not _KEY_RE.fullmatch(value):
        return None
    return value


def _parse_date_range(date_from_str: str | None, date_to_str: str | None) -> tuple[datetime | None, datetime | None]:
    start = end = None
    try:
        if date_from_str:
            start = datetime.fromisoformat(date_from_str)
        if date_to_str:
            end = datetime.fromisoformat(date_to_str)
            if "T" not in date_to_str and " " not in date_to_str:
                end = end + timedelta(days=1)
            else:
                end = end + timedelta(seconds=1)
    except ValueError:
        return None, None
    return start, end


def _render_summary(row: AuditEvent) -> str:
    details = row.details or {}
    team_name = details.get("team_name")
    checkpoint_name = details.get("checkpoint_name")
    username = details.get("username")
    name = details.get("name")
    email = details.get("email")

    if row.event_type == "checkin_created" and team_name and checkpoint_name:
        return _("Check-in recorded for team %(team)s at %(checkpoint)s.", team=team_name, checkpoint=checkpoint_name)
    if row.event_type == "checkin_updated":
        return _("Check-in updated.")
    if row.event_type == "checkin_deleted":
        return _("Check-in deleted.")
    if row.event_type == "score_submitted" and team_name and checkpoint_name:
        return _("Score submitted for team %(team)s at %(checkpoint)s.", team=team_name, checkpoint=checkpoint_name)
    if row.event_type == "user_created" and username:
        return _("User %(user)s created.", user=username)
    if row.event_type == "user_updated" and (details.get("after") or {}).get("username"):
        return _("User %(user)s updated.", user=details["after"]["username"])
    if row.event_type == "competition_member_attached" and username:
        return _("User %(user)s added to the competition.", user=username)
    if row.event_type == "competition_member_removed" and username:
        return _("User %(user)s removed from the competition.", user=username)
    if row.event_type == "competition_created" and name:
        return _("Competition %(name)s created.", name=name)
    if row.event_type == "competition_updated":
        return _("Competition settings updated.")
    if row.event_type == "competition_invite_saved" and email:
        return _("Invite saved for %(email)s.", email=email)
    if row.event_type == "competition_invite_revoked" and email:
        return _("Invite revoked for %(email)s.", email=email)
    if row.event_type == "team_created" and name:
        return _("Team %(name)s created.", name=name)
    if row.event_type == "team_updated":
        return _("Team updated.")
    if row.event_type == "team_deleted" and name:
        return _("Team %(name)s deleted.", name=name)
    if row.event_type == "team_group_updated":
        return _("Active group updated for the team.")
    if row.event_type == "team_numbers_randomized":
        return _("Team numbers randomized.")
    if row.event_type == "checkpoint_created" and name:
        return _("Checkpoint %(name)s created.", name=name)
    if row.event_type == "checkpoint_updated":
        return _("Checkpoint updated.")
    if row.event_type == "checkpoint_deleted" and name:
        return _("Checkpoint %(name)s deleted.", name=name)
    if row.event_type == "group_created" and name:
        return _("Group %(name)s created.", name=name)
    if row.event_type == "group_updated":
        return _("Group updated.")
    if row.event_type == "group_deleted" and name:
        return _("Group %(name)s deleted.", name=name)
    if row.event_type == "group_order_updated":
        return _("Group order updated.")
    if row.event_type == "device_created" and name:
        return _("Device %(name)s created.", name=name)
    if row.event_type == "device_updated":
        return _("Device updated.")
    if row.event_type == "device_deleted" and name:
        return _("Device %(name)s deleted.", name=name)
    return row.summary


@audit_bp.route("/", methods=["GET"])
@roles_required("admin")
def list_audit_events():
    comp_id = get_current_competition_id()
    if not comp_id:
        flash(_("Select a competition first."), "warning")
        return redirect(url_for("main.select_competition"))

    event_type = _sanitize_key(request.args.get("event_type"))
    entity_type = _sanitize_key(request.args.get("entity_type"))
    actor = (request.args.get("actor") or "").strip()
    if len(actor) > 100:
        actor = actor[:100]

    selected_date_from = request.args.get("date_from") or ""
    selected_date_to = request.args.get("date_to") or ""
    date_from, date_to = _parse_date_range(selected_date_from, selected_date_to)

    query = (
        AuditEvent.query
        .filter(AuditEvent.competition_id == comp_id)
        .options(
            joinedload(AuditEvent.actor_user),
            joinedload(AuditEvent.actor_device),
        )
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
    )
    if event_type:
        query = query.filter(AuditEvent.event_type == event_type)
    if entity_type:
        query = query.filter(AuditEvent.entity_type == entity_type)
    if actor:
        query = query.filter(AuditEvent.actor_label.ilike(f"%{actor}%"))
    if date_from:
        query = query.filter(AuditEvent.created_at >= date_from)
    if date_to:
        query = query.filter(AuditEvent.created_at < date_to)

    rows = query.limit(500).all()

    events = []
    for row in rows:
        events.append(
            {
                "id": row.id,
                "created_at": row.created_at,
                "event_type": row.event_type,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "actor": actor_label_for(
                    actor_type=row.actor_type,
                    actor_user=row.actor_user,
                    actor_device=row.actor_device,
                    actor_label=row.actor_label,
                ),
                "summary": _render_summary(row),
                "details": row.details,
            }
        )

    event_types = [
        value[0]
        for value in (
            AuditEvent.query
            .with_entities(AuditEvent.event_type)
            .filter(AuditEvent.competition_id == comp_id)
            .distinct()
            .order_by(AuditEvent.event_type.asc())
            .all()
        )
        if value and value[0]
    ]
    entity_types = [
        value[0]
        for value in (
            AuditEvent.query
            .with_entities(AuditEvent.entity_type)
            .filter(AuditEvent.competition_id == comp_id)
            .distinct()
            .order_by(AuditEvent.entity_type.asc())
            .all()
        )
        if value and value[0]
    ]

    return render_template(
        "audit_list.html",
        events=events,
        event_types=event_types,
        entity_types=entity_types,
        selected_event_type=event_type or "",
        selected_entity_type=entity_type or "",
        selected_actor=actor,
        selected_date_from=selected_date_from,
        selected_date_to=selected_date_to,
    )
