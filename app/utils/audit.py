from __future__ import annotations

from datetime import datetime
from typing import Any

from flask_babel import gettext as _
from flask_login import current_user

from app.extensions import db
from app.models import AuditEvent, LoRaDevice, User


def format_device_label(device: LoRaDevice | None) -> str:
    if not device:
        return _("Unknown device")
    name = (device.name or "").strip()
    if name:
        return name
    if device.dev_num is not None:
        return f"DEV-{device.dev_num}"
    if device.id is not None:
        return _("Device ID %(id)s", id=device.id)
    return _("Unknown device")


def format_user_label(user: User | None) -> str:
    if not user:
        return _("Unknown user")
    username = (user.username or "").strip()
    if username:
        return username
    email = (user.email or "").strip()
    if email:
        return email
    return _("User %(id)s", id=user.id)


def actor_payload(
    *,
    user: User | None = None,
    device: LoRaDevice | None = None,
    actor_type: str | None = None,
    label: str | None = None,
) -> dict[str, Any]:
    if user:
        return {
            "actor_type": "user",
            "actor_user": user,
            "actor_device": None,
            "actor_label": label or format_user_label(user),
        }
    if device:
        return {
            "actor_type": "device",
            "actor_user": None,
            "actor_device": device,
            "actor_label": label or format_device_label(device),
        }
    return {
        "actor_type": actor_type or "system",
        "actor_user": None,
        "actor_device": None,
        "actor_label": label or _("System"),
    }


def current_user_actor() -> dict[str, Any]:
    if getattr(current_user, "is_authenticated", False):
        return actor_payload(user=current_user)
    return actor_payload(actor_type="system")


def actor_label_for(
    *,
    actor_type: str | None = None,
    actor_user: User | None = None,
    actor_device: LoRaDevice | None = None,
    actor_label: str | None = None,
) -> str:
    if actor_label:
        return actor_label
    if actor_user:
        return format_user_label(actor_user)
    if actor_device:
        return format_device_label(actor_device)
    if actor_type == "device":
        return _("Unknown device")
    if actor_type == "user":
        return _("Unknown user")
    return _("System")


def record_audit_event(
    *,
    competition_id: int,
    event_type: str,
    entity_type: str,
    entity_id: int | None,
    summary: str,
    details: dict[str, Any] | None = None,
    actor_type: str | None = None,
    actor_user: User | None = None,
    actor_device: LoRaDevice | None = None,
    actor_label: str | None = None,
    created_at: datetime | None = None,
) -> AuditEvent:
    event = AuditEvent(
        competition_id=competition_id,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id,
        actor_type=actor_type or ("user" if actor_user else "device" if actor_device else "system"),
        actor_user=actor_user,
        actor_device=actor_device,
        actor_label=actor_label_for(
            actor_type=actor_type,
            actor_user=actor_user,
            actor_device=actor_device,
            actor_label=actor_label,
        ),
        summary=summary,
        details=details or None,
        created_at=created_at or datetime.utcnow(),
    )
    db.session.add(event)
    return event
