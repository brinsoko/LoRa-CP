# app/models.py
from __future__ import annotations
from datetime import datetime

from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin

from sqlalchemy import CheckConstraint, UniqueConstraint, event
from sqlalchemy.ext.associationproxy import association_proxy
from app.extensions import db


# =================
# User (auth/roles)
# =================
class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="public")  # public|judge|admin|superadmin
    google_sub = db.Column(db.String(255), unique=True, nullable=True)
    email = db.Column(db.String(255), unique=True, nullable=True)

    competition_memberships = db.relationship(
        "CompetitionMember",
        back_populates="user",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    competition_invites = db.relationship(
        "CompetitionInvite",
        back_populates="invited_user",
        foreign_keys="CompetitionInvite.invited_user_id",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def set_password(self, raw: str) -> None:
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password_hash, raw)


# =================
# Competition
# =================
class Competition(db.Model):
    __tablename__ = "competitions"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False, unique=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    public_results = db.Column(db.Boolean, nullable=False, default=False)
    created_by_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    created_by_user = db.relationship("User")
    members = db.relationship(
        "CompetitionMember",
        back_populates="competition",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    invites = db.relationship(
        "CompetitionInvite",
        back_populates="competition",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    teams = db.relationship(
        "Team",
        back_populates="competition",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    checkpoints = db.relationship(
        "Checkpoint",
        back_populates="competition",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    checkpoint_groups = db.relationship(
        "CheckpointGroup",
        back_populates="competition",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    devices = db.relationship(
        "LoRaDevice",
        back_populates="competition",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    sheets = db.relationship(
        "SheetConfig",
        back_populates="competition",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class CompetitionMember(db.Model):
    __tablename__ = "competition_members"

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(
        db.Integer,
        db.ForeignKey("competitions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role = db.Column(db.String(20), nullable=False, default="judge")  # admin|judge|viewer
    active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    competition = db.relationship("Competition", back_populates="members")
    user = db.relationship("User", back_populates="competition_memberships")

    __table_args__ = (
        UniqueConstraint("competition_id", "user_id", name="uq_competition_member"),
    )


class CompetitionInvite(db.Model):
    __tablename__ = "competition_invites"

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(
        db.Integer,
        db.ForeignKey("competitions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token = db.Column(db.String(64), unique=True, nullable=False, index=True)
    role = db.Column(db.String(20), nullable=False, default="judge")  # admin|judge|viewer
    expires_at = db.Column(db.DateTime, nullable=False)
    used_at = db.Column(db.DateTime, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    created_by_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    invited_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    invited_email = db.Column(db.String(255), nullable=True, index=True)

    competition = db.relationship("Competition", back_populates="invites")
    created_by_user = db.relationship("User", foreign_keys=[created_by_user_id])
    invited_user = db.relationship("User", foreign_keys=[invited_user_id], back_populates="competition_invites")


class JudgeCheckpoint(db.Model):
    __tablename__ = "judge_checkpoints"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    checkpoint_id = db.Column(
        db.Integer,
        db.ForeignKey("checkpoints.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    is_default = db.Column(db.Boolean, nullable=False, default=False)

    user = db.relationship("User")
    checkpoint = db.relationship("Checkpoint")

    __table_args__ = (
        UniqueConstraint("user_id", "checkpoint_id", name="uq_judge_checkpoint"),
    )

# =========
# Team
# =========
class Team(db.Model):
    __tablename__ = "teams"

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(
        db.Integer, db.ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = db.Column(db.String(100), nullable=False)
    number = db.Column(db.Integer, nullable=True)
    organization = db.Column(db.String(120), nullable=True, index=True)
    dnf = db.Column(db.Boolean, nullable=False, default=False)

    # relationships
    competition = db.relationship("Competition", back_populates="teams")
    rfid_card = db.relationship(
        "RFIDCard",
        back_populates="team",
        uselist=False,
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    checkins = db.relationship(
        "Checkin",
        back_populates="team",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    # optional many-assignments with 'active' flag via TeamGroup
    group_assignments = db.relationship(
        "TeamGroup",
        back_populates="team",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        CheckConstraint("number IS NULL OR number > 0", name="ck_team_number_positive"),
    )


# ==============
# RFIDCard
# ==============
class RFIDCard(db.Model):
    __tablename__ = "rfid_cards"

    id = db.Column(db.Integer, primary_key=True)
    uid = db.Column(db.String(100), unique=True, nullable=False)
    team_id = db.Column(
        db.Integer,
        db.ForeignKey("teams.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    # Optional human-friendly identifier, must be positive if set
    number = db.Column(db.Integer, nullable=True)

    team = db.relationship("Team", back_populates="rfid_card")

    __table_args__ = (
        CheckConstraint("number IS NULL OR number > 0", name="ck_rfid_number_positive"),
    )


# ====================
# CheckpointGroup
# ====================
class CheckpointGroup(db.Model):
    __tablename__ = "checkpoint_groups"

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(
        db.Integer, db.ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = db.Column(db.String(120), nullable=False)
    prefix = db.Column(db.String(20), nullable=True)
    description = db.Column(db.Text)
    position = db.Column(db.Integer, nullable=False, default=0)

    competition = db.relationship("Competition", back_populates="checkpoint_groups")
    checkpoint_links = db.relationship(
        "CheckpointGroupLink",
        back_populates="group",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="CheckpointGroupLink.position.asc()",
    )
    checkpoints = association_proxy(
        "checkpoint_links",
        "checkpoint",
        creator=lambda checkpoint: CheckpointGroupLink(checkpoint=checkpoint),
    )

    # If you prefer explicit:
    # team_assignments = db.relationship("TeamGroup", back_populates="group",
    #                                    cascade="all, delete-orphan",
    #                                    passive_deletes=True)

    __table_args__ = (
        UniqueConstraint("competition_id", "name", name="uq_checkpoint_group_competition_name"),
    )


# ============
# Checkpoint
# ============
class Checkpoint(db.Model):
    __tablename__ = "checkpoints"

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(
        db.Integer, db.ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = db.Column(db.String(120), nullable=False)
    location = db.Column(db.String(255))
    description = db.Column(db.Text)
    easting = db.Column(db.Float)
    northing = db.Column(db.Float)

    # Device mapping (one device ↔ one checkpoint)
    lora_device_id = db.Column(
        db.Integer,
        db.ForeignKey("lora_devices.id", ondelete="SET NULL"),
        unique=True,        # a device cannot be assigned to two checkpoints
        nullable=True,
    )
    lora_device = db.relationship("LoRaDevice", back_populates="checkpoint")

    # one-to-many from Checkin
    checkins = db.relationship("Checkin", back_populates="checkpoint", lazy=True)

    competition = db.relationship("Competition", back_populates="checkpoints")
    group_links = db.relationship(
        "CheckpointGroupLink",
        back_populates="checkpoint",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    groups = association_proxy(
        "group_links",
        "group",
        creator=lambda group: CheckpointGroupLink(group=group),
    )

    __table_args__ = (
        UniqueConstraint("competition_id", "name", name="uq_checkpoint_competition_name"),
    )

class CheckpointGroupLink(db.Model):
    __tablename__ = "checkpoint_group_links"

    group_id = db.Column(
        db.Integer,
        db.ForeignKey("checkpoint_groups.id", ondelete="CASCADE"),
        primary_key=True,
    )
    checkpoint_id = db.Column(
        db.Integer,
        db.ForeignKey("checkpoints.id", ondelete="CASCADE"),
        primary_key=True,
    )
    position = db.Column(db.Integer, nullable=False)

    group = db.relationship("CheckpointGroup", back_populates="checkpoint_links")
    checkpoint = db.relationship("Checkpoint", back_populates="group_links")

    __table_args__ = (
        UniqueConstraint("checkpoint_id", "group_id", name="uq_cp_group"),
    )


# =========
# Checkin
# =========
class Checkin(db.Model):
    __tablename__ = "checkins"

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(
        db.Integer, db.ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    team_id = db.Column(
        db.Integer, db.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    checkpoint_id = db.Column(
        db.Integer, db.ForeignKey("checkpoints.id", ondelete="CASCADE"), nullable=False
    )
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    team = db.relationship("Team", back_populates="checkins")
    checkpoint = db.relationship("Checkpoint", back_populates="checkins")
    competition = db.relationship("Competition")
    scores = db.relationship(
        "ScoreEntry",
        back_populates="checkin",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (
        db.UniqueConstraint("team_id", "checkpoint_id", name="uq_team_checkpoint"),
    )


# =========================
# TeamGroup (team ↔ group with 'active' flag)
# =========================
class TeamGroup(db.Model):
    __tablename__ = "team_groups"

    id = db.Column(db.Integer, primary_key=True)

    team_id = db.Column(
        db.Integer,
        db.ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    group_id = db.Column(
        db.Integer,
        db.ForeignKey("checkpoint_groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # Current active assignment? (only one should be active at a time per team)
    active = db.Column(db.Boolean, nullable=False, default=True)

    # Relationships
    team = db.relationship("Team", back_populates="group_assignments")
    group = db.relationship("CheckpointGroup", backref="team_assignments")
    # If you used explicit back_populates in CheckpointGroup, swap to:
    # group = db.relationship("CheckpointGroup", back_populates="team_assignments")

    __table_args__ = (
        # A team cannot have the same group twice
        UniqueConstraint("team_id", "group_id", name="uq_team_group"),
    )


# =========================
# Device (LoRa gateway or phone)
# =========================
class LoRaDevice(db.Model):
    __tablename__ = "lora_devices"

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(
        db.Integer, db.ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dev_num = db.Column(db.Integer, index=True, nullable=False)
    name = db.Column(db.String(120), nullable=True)                  # friendly label
    note = db.Column(db.Text, nullable=True)
    model = db.Column(db.String(64), nullable=True)
    active = db.Column(db.Boolean, nullable=False, default=True)
    

    # optional telemetry-ish fields
    last_seen = db.Column(db.DateTime)
    last_rssi = db.Column(db.Float)
    battery = db.Column(db.Float)


    # inverse: which checkpoint references this device (0 or 1)
    checkpoint = db.relationship("Checkpoint", back_populates="lora_device", uselist=False)
    competition = db.relationship("Competition", back_populates="devices")

    __table_args__ = (
        UniqueConstraint("competition_id", "dev_num", name="uq_device_competition_devnum"),
    )

class LoRaMessage(db.Model):
    __tablename__ = "lora_messages"
    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(
        db.Integer, db.ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    dev_id = db.Column(db.String(64), index=True, nullable=False)
    payload = db.Column(db.Text, nullable=False)
    rssi = db.Column(db.Float)
    snr = db.Column(db.Float)
    received_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)

    competition = db.relationship("Competition")


# =========================
# Google Sheets config
# =========================
class SheetConfig(db.Model):
    __tablename__ = "sheet_configs"

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(
        db.Integer, db.ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    spreadsheet_id = db.Column(db.String(128), nullable=False, index=True)
    spreadsheet_name = db.Column(db.String(200), nullable=False)
    tab_name = db.Column(db.String(200), nullable=False)
    tab_type = db.Column(db.String(50), nullable=False, default="checkpoint")  # root|teams|arrivals|total|checkpoint
    checkpoint_id = db.Column(db.Integer, db.ForeignKey("checkpoints.id", ondelete="SET NULL"), nullable=True)
    config = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    competition = db.relationship("Competition", back_populates="sheets")
    checkpoint = db.relationship("Checkpoint")

    __table_args__ = (
        UniqueConstraint("spreadsheet_id", "tab_name", name="uq_sheet_tab"),
    )


# =========================
# ScoreEntry (judge scoring)
# =========================
class ScoreEntry(db.Model):
    __tablename__ = "score_entries"

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(
        db.Integer, db.ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    checkin_id = db.Column(
        db.Integer, db.ForeignKey("checkins.id", ondelete="CASCADE"), nullable=False, index=True
    )
    team_id = db.Column(
        db.Integer, db.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    checkpoint_id = db.Column(
        db.Integer, db.ForeignKey("checkpoints.id", ondelete="CASCADE"), nullable=False, index=True
    )
    judge_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    raw_fields = db.Column(db.JSON, nullable=False, default=dict)
    total = db.Column(db.Float, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    checkin = db.relationship("Checkin", back_populates="scores")
    team = db.relationship("Team")
    checkpoint = db.relationship("Checkpoint")
    judge_user = db.relationship("User")


# =========================
# ScoreRule (scoring logic)
# =========================
class ScoreRule(db.Model):
    __tablename__ = "score_rules"

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(
        db.Integer, db.ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    checkpoint_id = db.Column(
        db.Integer, db.ForeignKey("checkpoints.id", ondelete="CASCADE"), nullable=False, index=True
    )
    group_id = db.Column(
        db.Integer, db.ForeignKey("checkpoint_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rules = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    checkpoint = db.relationship("Checkpoint")
    group = db.relationship("CheckpointGroup")

    __table_args__ = (
        UniqueConstraint("competition_id", "checkpoint_id", "group_id", name="uq_score_rule_scope"),
    )


# =========================
# GlobalScoreRule (group-wide scoring logic)
# =========================
class GlobalScoreRule(db.Model):
    __tablename__ = "global_score_rules"

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(
        db.Integer, db.ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    group_id = db.Column(
        db.Integer, db.ForeignKey("checkpoint_groups.id", ondelete="CASCADE"), nullable=False, index=True
    )
    rules = db.Column(db.JSON, nullable=False, default=dict)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    group = db.relationship("CheckpointGroup")

    __table_args__ = (
        UniqueConstraint("competition_id", "group_id", name="uq_global_score_rule_scope"),
    )


def _assign_checkpoint_link_position(link: CheckpointGroupLink) -> None:
    if link.position is not None:
        return
    group = link.group
    if not group:
        return

    existing_positions = [
        l.position
        for l in group.checkpoint_links
        if l is not link and l.position is not None
    ]
    link.position = (max(existing_positions) + 1) if existing_positions else 0


@event.listens_for(CheckpointGroup.checkpoint_links, "append")
def _on_group_link_append(group: CheckpointGroup, link: CheckpointGroupLink, *_):
    """Ensure new links created via group.checkpoints get a position."""
    _assign_checkpoint_link_position(link)


@event.listens_for(Checkpoint.group_links, "append")
def _on_checkpoint_link_append(checkpoint: Checkpoint, link: CheckpointGroupLink, *_):
    """Ensure new links created via checkpoint.groups get a position."""
    _assign_checkpoint_link_position(link)
