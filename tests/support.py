from __future__ import annotations

from datetime import datetime
import itertools

from app.extensions import db
from app.models import (
    Checkin,
    Checkpoint,
    CheckpointGroup,
    Competition,
    CompetitionMember,
    JudgeCheckpoint,
    LoRaDevice,
    RFIDCard,
    Team,
    TeamGroup,
    User,
)


_COUNTER = itertools.count(1)


def unique_name(prefix: str) -> str:
    return f"{prefix}-{next(_COUNTER)}"


def create_user(*, username: str | None = None, password: str = "password123", email: str | None = None, role: str = "public") -> User:
    username = username or unique_name("user")
    if email is None:
        email = f"{username}@example.com"
    user = User(username=username, email=email, role=role)
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    return user


def create_competition(*, name: str | None = None, created_by_user: User | None = None, public_results: bool = False, ingest_password: str | None = None) -> Competition:
    competition = Competition(
        name=name or unique_name("competition"),
        created_by_user_id=created_by_user.id if created_by_user else None,
        public_results=public_results,
    )
    if ingest_password:
        competition.set_ingest_password(ingest_password)
    db.session.add(competition)
    db.session.commit()
    return competition


def add_membership(user: User, competition: Competition, *, role: str = "admin", active: bool = True) -> CompetitionMember:
    membership = CompetitionMember(
        user_id=user.id,
        competition_id=competition.id,
        role=role,
        active=active,
    )
    db.session.add(membership)
    db.session.commit()
    return membership


def create_team(competition: Competition, *, name: str | None = None, number: int | None = None, organization: str | None = None) -> Team:
    team = Team(
        competition_id=competition.id,
        name=name or unique_name("team"),
        number=number,
        organization=organization,
    )
    db.session.add(team)
    db.session.commit()
    return team


def create_group(competition: Competition, *, name: str | None = None, prefix: str | None = None, description: str | None = None) -> CheckpointGroup:
    position = db.session.query(db.func.max(CheckpointGroup.position)).filter(
        CheckpointGroup.competition_id == competition.id
    ).scalar()
    group = CheckpointGroup(
        competition_id=competition.id,
        name=name or unique_name("group"),
        prefix=prefix,
        description=description,
        position=(position if position is not None else -1) + 1,
    )
    db.session.add(group)
    db.session.commit()
    return group


def assign_team_group(team: Team, group: CheckpointGroup, *, active: bool = True) -> TeamGroup:
    link = TeamGroup(team_id=team.id, group_id=group.id, active=active)
    db.session.add(link)
    db.session.commit()
    return link


def create_device(competition: Competition, *, dev_num: int, name: str | None = None, note: str | None = None, model: str | None = None, active: bool = True) -> LoRaDevice:
    device = LoRaDevice(
        competition_id=competition.id,
        dev_num=dev_num,
        name=name,
        note=note,
        model=model,
        active=active,
    )
    db.session.add(device)
    db.session.commit()
    return device


def create_checkpoint(
    competition: Competition,
    *,
    name: str | None = None,
    easting: float | None = None,
    northing: float | None = None,
    location: str | None = None,
    description: str | None = None,
    lora_device: LoRaDevice | None = None,
) -> Checkpoint:
    checkpoint = Checkpoint(
        competition_id=competition.id,
        name=name or unique_name("checkpoint"),
        easting=easting,
        northing=northing,
        location=location,
        description=description,
        lora_device_id=lora_device.id if lora_device else None,
    )
    db.session.add(checkpoint)
    db.session.commit()
    return checkpoint


def attach_device_to_checkpoint(device: LoRaDevice, checkpoint: Checkpoint) -> None:
    checkpoint.lora_device_id = device.id
    db.session.commit()


def create_rfid_card(team: Team, *, uid: str | None = None, number: int | None = None) -> RFIDCard:
    card = RFIDCard(uid=uid or unique_name("UID").upper(), team_id=team.id, number=number)
    db.session.add(card)
    db.session.commit()
    return card


def create_checkin(
    competition: Competition,
    team: Team,
    checkpoint: Checkpoint,
    *,
    timestamp: datetime | None = None,
    created_by_user: User | None = None,
    created_by_device: LoRaDevice | None = None,
) -> Checkin:
    checkin = Checkin(
        competition_id=competition.id,
        team_id=team.id,
        checkpoint_id=checkpoint.id,
        timestamp=timestamp or datetime.utcnow(),
        created_by_user_id=created_by_user.id if created_by_user else None,
        created_by_device_id=created_by_device.id if created_by_device else None,
    )
    db.session.add(checkin)
    db.session.commit()
    return checkin


def assign_judge_checkpoint(user: User, checkpoint: Checkpoint, *, is_default: bool = False) -> JudgeCheckpoint:
    if is_default:
        (
            JudgeCheckpoint.query
            .filter(JudgeCheckpoint.user_id == user.id)
            .update({JudgeCheckpoint.is_default: False}, synchronize_session=False)
        )
    assignment = JudgeCheckpoint(user_id=user.id, checkpoint_id=checkpoint.id, is_default=is_default)
    db.session.add(assignment)
    db.session.commit()
    return assignment


def login_as(client, user: User, competition: Competition | None = None) -> None:
    with client.session_transaction() as sess:
        sess["_user_id"] = str(user.id)
        if competition is None:
            sess.pop("competition_id", None)
        else:
            sess["competition_id"] = competition.id

