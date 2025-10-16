# app/models.py
from __future__ import annotations
from datetime import datetime
from typing import List, Optional
from enum import Enum

from app.extensions import db
from sqlalchemy import String, Integer, Text, DateTime, ForeignKey, CheckConstraint, UniqueConstraint, Index, Float
from sqlalchemy.orm import Mapped, mapped_column, relationship
from flask_login import UserMixin
from werkzeug.security import generate_password_hash, check_password_hash


# ----------------------------
# Users / Auth
# ----------------------------
class User(UserMixin, db.Model):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[str] = mapped_column(String(20), nullable=False, default="public")

    def set_password(self, raw: str) -> None:
        self.password_hash = generate_password_hash(raw)

    def check_password(self, raw: str) -> bool:
        return check_password_hash(self.password_hash, raw)


# ----------------------------
# Core domain
# ----------------------------
class Team(db.Model):
    __tablename__ = "teams"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # 1:1 RFID
    rfid_card: Mapped[Optional["RFIDCard"]] = relationship(
        back_populates="team", uselist=False, cascade="all, delete-orphan", passive_deletes=True
    )

    # 1:N Checkins
    checkins: Mapped[List["Checkin"]] = relationship(
        back_populates="team", cascade="all, delete-orphan", passive_deletes=True
    )

    # Group assignments
    group_assignments: Mapped[List["TeamGroup"]] = relationship(
        back_populates="team", cascade="all, delete-orphan"
    )

    __table_args__ = (
        CheckConstraint("number IS NULL OR number > 0", name="ck_team_number_positive"),
    )


class RFIDCard(db.Model):
    __tablename__ = "rfid_cards"
    id: Mapped[int] = mapped_column(primary_key=True)
    uid: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    team_id: Mapped[int] = mapped_column(
        ForeignKey("teams.id", ondelete="CASCADE"),
        unique=True, nullable=False
    )
    # Optional human-friendly label; must be positive if set
    number: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    team: Mapped["Team"] = relationship(back_populates="rfid_card")

    __table_args__ = (
        CheckConstraint("number IS NULL OR number > 0", name="ck_rfid_number_positive"),
    )


class Checkpoint(db.Model):
    __tablename__ = "checkpoints"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    location: Mapped[Optional[str]] = mapped_column(String(255))
    description: Mapped[Optional[str]] = mapped_column(Text)

    # numeric coords from JSON
    easting: Mapped[Optional[float]] = mapped_column(Float)
    northing: Mapped[Optional[float]] = mapped_column(Float)

    # 1:N Checkins (mirror of Checkin.checkpoint)
    checkins: Mapped[List["Checkin"]] = relationship(back_populates="checkpoint")


class Checkin(db.Model):
    __tablename__ = "checkins"
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    checkpoint_id: Mapped[int] = mapped_column(ForeignKey("checkpoints.id", ondelete="CASCADE"), nullable=False)
    timestamp: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, nullable=False)

    team: Mapped["Team"] = relationship(back_populates="checkins")
    checkpoint: Mapped["Checkpoint"] = relationship(back_populates="checkins")


# (Optional enum if you need it later; not used by schema below)
class TargetStatus(str, Enum):
    NOT_FOUND = "not_found"
    NEXT = "next"
    FOUND = "found"


# ----------------------------
# Groups for checkpoints
# ----------------------------
class CheckpointGroup(db.Model):
    __tablename__ = "checkpoint_groups"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text)

    checkpoints: Mapped[List["GroupCheckpoint"]] = relationship(
        back_populates="group", cascade="all, delete-orphan", order_by="GroupCheckpoint.seq_index.asc()"
    )
    team_assignments: Mapped[List["TeamGroup"]] = relationship(
        back_populates="group", cascade="all, delete-orphan"
    )


class GroupCheckpoint(db.Model):
    __tablename__ = "group_checkpoints"
    id: Mapped[int] = mapped_column(primary_key=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("checkpoint_groups.id"), nullable=False, index=True)
    checkpoint_id: Mapped[int] = mapped_column(ForeignKey("checkpoints.id"), nullable=False, index=True)
    seq_index: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    group: Mapped["CheckpointGroup"] = relationship(back_populates="checkpoints")
    checkpoint: Mapped["Checkpoint"] = relationship(backref="group_links")

    __table_args__ = (
        UniqueConstraint("group_id", "checkpoint_id", name="uq_group_checkpoint"),
        Index("ix_group_seq", "group_id", "seq_index"),
    )


class TeamGroup(db.Model):
    __tablename__ = "team_groups"
    id: Mapped[int] = mapped_column(primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), nullable=False, index=True)
    group_id: Mapped[int] = mapped_column(ForeignKey("checkpoint_groups.id"), nullable=False, index=True)
    active: Mapped[bool] = mapped_column(nullable=False, default=True)

    team: Mapped["Team"] = relationship(back_populates="group_assignments")
    group: Mapped["CheckpointGroup"] = relationship(back_populates="team_assignments")

    __table_args__ = (
        UniqueConstraint("team_id", "group_id", name="uq_team_group"),
    )