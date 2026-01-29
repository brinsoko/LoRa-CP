# app/utils/competition.py
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional
from types import SimpleNamespace
import secrets

from flask import session
from flask_login import current_user

from app.extensions import db
from app.models import Competition, CompetitionMember, CompetitionInvite, User, CheckpointGroup


DEFAULT_COMPETITION_NAME = "Default Competition"
INVITE_EXPIRY_DAYS = 7


def ensure_default_competition() -> Competition | None:
    if Competition.query.count():
        return Competition.query.order_by(Competition.created_at.asc()).first()

    admin_user = (
        User.query
        .filter(User.role.in_(["superadmin", "admin"]))
        .order_by(User.id.asc())
        .first()
    )
    competition = Competition(
        name=DEFAULT_COMPETITION_NAME,
        created_by_user_id=admin_user.id if admin_user else None,
    )
    db.session.add(competition)
    db.session.flush()

    if admin_user:
        db.session.add(
            CompetitionMember(
                competition_id=competition.id,
                user_id=admin_user.id,
                role="admin",
                active=True,
            )
        )

    db.session.commit()
    return competition


def get_user_memberships(user_id: int) -> list[CompetitionMember]:
    user = User.query.filter_by(id=user_id).first()
    if user and (user.role or "").strip().lower() == "superadmin":
        competitions = (
            Competition.query
            .order_by(Competition.name.asc())
            .all()
        )
        return [
            SimpleNamespace(competition=comp, role="admin", active=True, user_id=user_id)
            for comp in competitions
        ]
    return (
        CompetitionMember.query
        .filter(
            CompetitionMember.user_id == user_id,
            CompetitionMember.active.is_(True),
        )
        .join(Competition, Competition.id == CompetitionMember.competition_id)
        .order_by(Competition.name.asc())
        .all()
    )


def get_user_competitions(user_id: int) -> list[Competition]:
    return [m.competition for m in get_user_memberships(user_id) if m.competition]


def _is_member(user_id: int, competition_id: int) -> bool:
    if current_user.is_authenticated and (current_user.role or "").strip().lower() == "superadmin":
        return True
    return (
        CompetitionMember.query
        .filter(
            CompetitionMember.user_id == user_id,
            CompetitionMember.competition_id == competition_id,
            CompetitionMember.active.is_(True),
        )
        .first()
        is not None
    )


def get_current_competition_id() -> Optional[int]:
    if not current_user.is_authenticated:
        return None

    comp_id = session.get("competition_id")
    if comp_id is not None:
        try:
            comp_id = int(comp_id)
        except Exception:
            session.pop("competition_id", None)
            comp_id = None
    if comp_id and _is_member(current_user.id, comp_id):
        return comp_id
    if comp_id:
        session.pop("competition_id", None)

    last_id = getattr(current_user, "last_competition_id", None)
    if last_id and _is_member(current_user.id, last_id):
        session["competition_id"] = last_id
        session.modified = True
        return last_id

    if (current_user.role or "").strip().lower() == "superadmin":
        first = Competition.query.order_by(Competition.created_at.asc()).first()
        if first:
            session["competition_id"] = first.id
            return first.id

    membership = (
        CompetitionMember.query
        .filter(
            CompetitionMember.user_id == current_user.id,
            CompetitionMember.active.is_(True),
        )
        .order_by(CompetitionMember.created_at.asc())
        .first()
    )
    if membership:
        session["competition_id"] = membership.competition_id
        return membership.competition_id
    return None


def get_current_competition() -> Competition | None:
    comp_id = get_current_competition_id()
    if not comp_id:
        return None
    return Competition.query.get(comp_id)


def get_current_membership(user_id: int | None = None) -> CompetitionMember | None:
    if not current_user.is_authenticated:
        return None
    comp_id = get_current_competition_id()
    if not comp_id:
        return None
    user_id = user_id or current_user.id
    if (current_user.role or "").strip().lower() == "superadmin":
        return SimpleNamespace(
            competition_id=comp_id,
            user_id=user_id,
            role="admin",
            active=True,
        )
    return (
        CompetitionMember.query
        .filter(
            CompetitionMember.user_id == user_id,
            CompetitionMember.competition_id == comp_id,
            CompetitionMember.active.is_(True),
        )
        .first()
    )


def get_current_competition_role() -> Optional[str]:
    if current_user.is_authenticated and (current_user.role or "").strip().lower() == "superadmin":
        return "admin"
    membership = get_current_membership()
    if not membership:
        return None
    return (membership.role or "").strip().lower() or None


def require_current_competition_id() -> Optional[int]:
    comp_id = get_current_competition_id()
    return comp_id


def set_current_competition_id(competition_id: int) -> bool:
    if not current_user.is_authenticated:
        return False
    try:
        competition_id = int(competition_id)
    except Exception:
        return False
    if not _is_member(current_user.id, competition_id):
        return False
    session["competition_id"] = competition_id
    session.modified = True
    try:
        current_user.last_competition_id = competition_id
        db.session.commit()
    except Exception:
        db.session.rollback()
    return True


def create_invite(
    competition_id: int,
    created_by_user_id: int,
    role: str = "judge",
    invited_email: str | None = None,
) -> CompetitionInvite:
    token = secrets.token_hex(16)
    invite = CompetitionInvite(
        competition_id=competition_id,
        token=token,
        role=role,
        expires_at=datetime.utcnow() + timedelta(days=INVITE_EXPIRY_DAYS),
        created_by_user_id=created_by_user_id,
        invited_email=invited_email,
    )
    db.session.add(invite)
    db.session.flush()
    return invite


def get_competition_group_order(competition_id: int) -> list[str]:
    groups = (
        CheckpointGroup.query
        .filter(CheckpointGroup.competition_id == competition_id)
        .order_by(CheckpointGroup.position.asc().nulls_last(), CheckpointGroup.name.asc())
        .all()
    )
    return [g.name for g in groups if g.name]
