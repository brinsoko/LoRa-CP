# app/models.py
from __future__ import annotations
from datetime import datetime

from werkzeug.security import generate_password_hash, check_password_hash
from flask_login import UserMixin

from sqlalchemy import CheckConstraint, UniqueConstraint
from app.extensions import db


# ============================================================
# Association table: MANY-TO-MANY between checkpoints & groups
# (Must be defined BEFORE models that reference it)
# ============================================================
checkpoint_group_links = db.Table(
    "checkpoint_group_links",
    db.Column(
        "checkpoint_id",
        db.Integer,
        db.ForeignKey("checkpoints.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    db.Column(
        "group_id",
        db.Integer,
        db.ForeignKey("checkpoint_groups.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    db.UniqueConstraint("checkpoint_id", "group_id", name="uq_cp_group"),
)


# =================
# User (auth/roles)
# =================
class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(50), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="public")  # public|judge|admin

    def set_password(self, raw: str) -> None:
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password_hash, raw)


# =========
# Team
# =========
class Team(db.Model):
    __tablename__ = "teams"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    number = db.Column(db.Integer, nullable=True)

    # relationships
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
    name = db.Column(db.String(120), unique=True, nullable=False)
    description = db.Column(db.Text)

    # many-to-many backref defined on Checkpoint.groups
    checkpoints = db.relationship(
        "Checkpoint",
        secondary=checkpoint_group_links,
        back_populates="groups",
        order_by="Checkpoint.name.asc()",
        cascade="save-update",
        passive_deletes=True,
    )

    # If you prefer explicit:
    # team_assignments = db.relationship("TeamGroup", back_populates="group",
    #                                    cascade="all, delete-orphan",
    #                                    passive_deletes=True)


# ============
# Checkpoint
# ============
class Checkpoint(db.Model):
    __tablename__ = "checkpoints"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False, unique=True)
    location = db.Column(db.String(255))
    description = db.Column(db.Text)
    easting = db.Column(db.Float)
    northing = db.Column(db.Float)

    # LoRa device mapping (one device ↔ one checkpoint)
    lora_device_id = db.Column(
        db.Integer,
        db.ForeignKey("lora_devices.id", ondelete="SET NULL"),
        unique=True,        # a device cannot be assigned to two checkpoints
        nullable=True,
    )
    lora_device = db.relationship("LoRaDevice", back_populates="checkpoint")

    # one-to-many from Checkin
    checkins = db.relationship("Checkin", back_populates="checkpoint", lazy=True)

    # many-to-many groups
    groups = db.relationship(
        "CheckpointGroup",
        secondary=checkpoint_group_links,
        back_populates="checkpoints",
        order_by="CheckpointGroup.name.asc()",
        cascade="save-update",
        passive_deletes=True,
    )


# =========
# Checkin
# =========
class Checkin(db.Model):
    __tablename__ = "checkins"

    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(
        db.Integer, db.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False
    )
    checkpoint_id = db.Column(
        db.Integer, db.ForeignKey("checkpoints.id", ondelete="CASCADE"), nullable=False
    )
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    team = db.relationship("Team", back_populates="checkins")
    checkpoint = db.relationship("Checkpoint", back_populates="checkins")

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
# LoRa device
# =========================
class LoRaDevice(db.Model):
    __tablename__ = "lora_devices"

    id = db.Column(db.Integer, primary_key=True)
    dev_eui = db.Column(db.String(32), unique=True, nullable=True)  # e.g., 16-byte hex string
    dev_num = db.Column(db.Integer, unique=True, index=True, nullable=False)
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

class LoRaMessage(db.Model):
    __tablename__ = "lora_messages"
    id = db.Column(db.Integer, primary_key=True)
    dev_id = db.Column(db.String(64), index=True, nullable=False)
    payload = db.Column(db.Text, nullable=False)
    rssi = db.Column(db.Float)
    snr = db.Column(db.Float)
    received_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)