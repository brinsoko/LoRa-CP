# app/models.py
from __future__ import annotations

from flask_login import UserMixin
from sqlalchemy import CheckConstraint, Index, UniqueConstraint, event
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db
from app.utils.time import utcnow_naive


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
    last_competition_id = db.Column(db.Integer, nullable=True, index=True)

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

    def __repr__(self) -> str:
        return f"<User id={self.id} username={self.username!r} role={self.role!r}>"


# =================
# Competition
# =================
class Competition(db.Model):
    __tablename__ = "competitions"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False, unique=True)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False)
    public_results = db.Column(db.Boolean, nullable=False, default=False)
    hide_gps_map = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    hide_dev_map = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    hide_audit_messages = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    hide_score_submissions = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    ingest_password_hash = db.Column(db.String(255), nullable=True)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

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
    paths = db.relationship(
        "Path",
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
    firmware_files = db.relationship(
        "FirmwareFile",
        back_populates="competition",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def set_ingest_password(self, raw: str | None) -> None:
        raw = (raw or "").strip()
        if not raw:
            self.ingest_password_hash = None
        else:
            self.ingest_password_hash = generate_password_hash(raw)

    def check_ingest_password(self, raw: str | None) -> bool:
        if not self.ingest_password_hash:
            return False
        return check_password_hash(self.ingest_password_hash, (raw or "").strip())

    def __repr__(self) -> str:
        return f"<Competition id={self.id} name={self.name!r}>"


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
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False)

    competition = db.relationship("Competition", back_populates="members")
    user = db.relationship("User", back_populates="competition_memberships")

    __table_args__ = (UniqueConstraint("competition_id", "user_id", name="uq_competition_member"),)

    def __repr__(self) -> str:
        return (
            f"<CompetitionMember id={self.id} competition_id={self.competition_id} "
            f"user_id={self.user_id} role={self.role!r} active={self.active}>"
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
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False)
    created_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    invited_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    invited_email = db.Column(db.String(255), nullable=True, index=True)

    competition = db.relationship("Competition", back_populates="invites")
    created_by_user = db.relationship("User", foreign_keys=[created_by_user_id])
    invited_user = db.relationship("User", foreign_keys=[invited_user_id], back_populates="competition_invites")

    def __repr__(self) -> str:
        return (
            f"<CompetitionInvite id={self.id} competition_id={self.competition_id} "
            f"role={self.role!r} invited_email={self.invited_email!r}>"
        )


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
    # competition_id scopes the assignment so editing a judge's assignments
    # in one competition can't wipe their assignments in another. Always
    # equal to checkpoint.competition_id for valid rows.
    competition_id = db.Column(
        db.Integer,
        db.ForeignKey("competitions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    is_default = db.Column(db.Boolean, nullable=False, default=False)

    user = db.relationship("User")
    checkpoint = db.relationship("Checkpoint")
    competition = db.relationship("Competition")

    __table_args__ = (UniqueConstraint("user_id", "checkpoint_id", name="uq_judge_checkpoint"),)

    def __repr__(self) -> str:
        return (
            f"<JudgeCheckpoint id={self.id} user_id={self.user_id} "
            f"checkpoint_id={self.checkpoint_id} default={self.is_default}>"
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
    # Free-text notes for judges to record special events (lost child,
    # bike trouble, etc.). Display-only; does not affect scoring.
    notes = db.Column(db.Text, nullable=True)
    # Team-level dead-time minutes not bound to any single checkpoint
    # (e.g. organizer-caused delay at the start). _get_team_dead_time_total
    # adds this to the per-CP sum before the time-trial penalty applies.
    bonus_dead_time = db.Column(db.Float, nullable=False, default=0.0, server_default="0.0")

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

    members = db.relationship(
        "TeamMember",
        back_populates="team",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="TeamMember.position",
    )

    __table_args__ = (CheckConstraint("number IS NULL OR number > 0", name="ck_team_number_positive"),)

    def __repr__(self) -> str:
        return f"<Team id={self.id} comp={self.competition_id} number={self.number} name={self.name!r}>"


# ==============
# TeamMember
# ==============
class TeamMember(db.Model):
    """One scout in a team. Free-text name + optional role (e.g. 'kapetan').

    position pins the display order so reorderings persist across reloads;
    the uq_team_member_position constraint blocks two rows colliding on
    the same slot.
    """

    __tablename__ = "team_members"

    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(
        db.Integer,
        db.ForeignKey("teams.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = db.Column(db.String(160), nullable=False)
    role = db.Column(db.String(80), nullable=True)
    position = db.Column(db.Integer, nullable=False, default=0)

    team = db.relationship("Team", back_populates="members")

    __table_args__ = (UniqueConstraint("team_id", "position", name="uq_team_member_position"),)

    def __repr__(self) -> str:
        return f"<TeamMember id={self.id} team={self.team_id} name={self.name!r}>"


# ==============
# RFIDCard
# ==============
class RFIDCard(db.Model):
    __tablename__ = "rfid_cards"

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(
        db.Integer,
        db.ForeignKey("competitions.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # UID is unique only within a competition — the same physical card
    # can be reused across years/events. Globally-unique UID would have
    # blocked re-issuance of the same scout card to a new team.
    uid = db.Column(db.String(100), nullable=False)
    team_id = db.Column(
        db.Integer,
        db.ForeignKey("teams.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    # Optional human-friendly identifier, must be positive if set
    number = db.Column(db.Integer, nullable=True)

    competition = db.relationship("Competition")
    team = db.relationship("Team", back_populates="rfid_card")

    __table_args__ = (
        UniqueConstraint("competition_id", "uid", name="uq_rfid_competition_uid"),
        CheckConstraint("number IS NULL OR number > 0", name="ck_rfid_number_positive"),
    )

    def __repr__(self) -> str:
        return f"<RFIDCard id={self.id} comp={self.competition_id} uid={self.uid!r} team_id={self.team_id}>"


# ====================
# Path (ordered course through checkpoints)
# ====================
class Path(db.Model):
    """An ordered course through checkpoints, shared between categories.

    A CheckpointGroup references a Path plus a direction, so two groups
    running the same course opposite ways share one Path row and can never
    disagree about the stop order. Route resolution (direction applied)
    lives in app/utils/paths.py; nothing else may derive start/finish.
    """

    __tablename__ = "paths"

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(
        db.Integer, db.ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = db.Column(db.String(120), nullable=False)
    notes = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False)

    competition = db.relationship("Competition", back_populates="paths")
    stops = db.relationship(
        "PathStop",
        back_populates="path",
        cascade="all, delete-orphan",
        passive_deletes=True,
        order_by="PathStop.position.asc()",
    )
    groups = db.relationship("CheckpointGroup", back_populates="path")

    __table_args__ = (UniqueConstraint("competition_id", "name", name="uq_path_competition_name"),)

    def __repr__(self) -> str:
        return f"<Path id={self.id} comp={self.competition_id} name={self.name!r}>"


class PathStop(db.Model):
    """One ordered stop on a Path.

    Unique on (path_id, position) only; the same checkpoint may appear
    twice on a path (out-and-back courses). Checkin recording for revisits
    is a separate, still-open feature; the model just doesn't block it.
    """

    __tablename__ = "path_stops"

    id = db.Column(db.Integer, primary_key=True)
    path_id = db.Column(
        db.Integer, db.ForeignKey("paths.id", ondelete="CASCADE"), nullable=False, index=True
    )
    checkpoint_id = db.Column(
        db.Integer, db.ForeignKey("checkpoints.id", ondelete="CASCADE"), nullable=False, index=True
    )
    position = db.Column(db.Integer, nullable=False)
    # Expected duration of the leg (previous stop -> this stop) in minutes,
    # undirected. ETA fallback for the judge "who is still coming" view
    # until enough observed leg times exist (redesign plan 3.1/3.5).
    # Null on the first stop or when unknown.
    expected_leg_minutes = db.Column(db.Float, nullable=True)

    path = db.relationship("Path", back_populates="stops")
    checkpoint = db.relationship("Checkpoint", back_populates="path_stops")

    __table_args__ = (UniqueConstraint("path_id", "position", name="uq_path_stop_position"),)

    def __repr__(self) -> str:
        return (
            f"<PathStop id={self.id} path_id={self.path_id} "
            f"checkpoint_id={self.checkpoint_id} position={self.position}>"
        )


# ====================
# CheckpointGroup
# ====================
class CheckpointGroup(db.Model):
    """A category of teams: identity (name/prefix) + team assignment +
    (path, direction). The ordered checkpoint list lives on the Path."""

    __tablename__ = "checkpoint_groups"

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(
        db.Integer, db.ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = db.Column(db.String(120), nullable=False)
    prefix = db.Column(db.String(20), nullable=True)
    description = db.Column(db.Text)
    position = db.Column(db.Integer, nullable=False, default=0)
    # The course this category runs, and in which direction. SET NULL on
    # path delete keeps the category (teams, scoring scope) alive; the API
    # additionally refuses to delete a path that groups still reference.
    path_id = db.Column(
        db.Integer, db.ForeignKey("paths.id", ondelete="SET NULL"), nullable=True, index=True
    )
    direction = db.Column(db.String(10), nullable=False, default="forward", server_default="forward")

    competition = db.relationship("Competition", back_populates="checkpoint_groups")
    path = db.relationship("Path", back_populates="groups")

    __table_args__ = (
        UniqueConstraint("competition_id", "name", name="uq_checkpoint_group_competition_name"),
        CheckConstraint("direction IN ('forward','reverse')", name="ck_group_direction"),
    )

    def __repr__(self) -> str:
        return f"<CheckpointGroup id={self.id} comp={self.competition_id} name={self.name!r}>"


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
    scoring_text = db.Column(db.Text)
    judges_note = db.Column(db.Text)
    easting = db.Column(db.Float)
    northing = db.Column(db.Float)
    is_virtual = db.Column(db.Boolean, nullable=False, default=False, server_default="0")
    # Manual display order. Nullable so newly imported checkpoints sort
    # after positioned ones; existing rows were backfilled by migration
    # c6d7e8f9a0b1 with row-number-by-name to preserve current ordering.
    position = db.Column(db.Integer, nullable=True)
    # When True, this CP is excluded from per-CP scoreboard columns and
    # from per-CP Google Sheet output. Check-ins are still recorded so
    # arrival tracking + time-trial leg end detection still work — the
    # CP just doesn't get its own column on every team row. Typical use:
    # a finish line that's also a time-trial leg's end_cp; we don't want
    # it cluttering the leaderboard as "Cilj: 0" since the leg cell
    # already reports the leg result.
    hide_from_results = db.Column(db.Boolean, nullable=False, default=False, server_default="0")

    # Device mapping (one device ↔ one checkpoint)
    lora_device_id = db.Column(
        db.Integer,
        db.ForeignKey("lora_devices.id", ondelete="SET NULL"),
        unique=True,  # a device cannot be assigned to two checkpoints
        nullable=True,
    )
    lora_device = db.relationship("LoRaDevice", back_populates="checkpoint")

    # one-to-many from Checkin
    checkins = db.relationship("Checkin", back_populates="checkpoint", lazy=True)

    competition = db.relationship("Competition", back_populates="checkpoints")
    path_stops = db.relationship(
        "PathStop",
        back_populates="checkpoint",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (UniqueConstraint("competition_id", "name", name="uq_checkpoint_competition_name"),)

    def __repr__(self) -> str:
        return f"<Checkpoint id={self.id} comp={self.competition_id} name={self.name!r}>"


# =========
# Checkin
# =========
class Checkin(db.Model):
    __tablename__ = "checkins"

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(
        db.Integer, db.ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False)
    checkpoint_id = db.Column(db.Integer, db.ForeignKey("checkpoints.id", ondelete="CASCADE"), nullable=False)
    timestamp = db.Column(db.DateTime, default=utcnow_naive, nullable=False)
    created_by_user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    created_by_device_id = db.Column(
        db.Integer, db.ForeignKey("lora_devices.id", ondelete="SET NULL"), nullable=True, index=True
    )

    team = db.relationship("Team", back_populates="checkins")
    checkpoint = db.relationship("Checkpoint", back_populates="checkins")
    competition = db.relationship("Competition")
    created_by_user = db.relationship("User", foreign_keys=[created_by_user_id])
    created_by_device = db.relationship("LoRaDevice", foreign_keys=[created_by_device_id])
    scores = db.relationship(
        "ScoreEntry",
        back_populates="checkin",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    __table_args__ = (db.UniqueConstraint("team_id", "checkpoint_id", name="uq_team_checkpoint"),)

    def __repr__(self) -> str:
        return (
            f"<Checkin id={self.id} comp={self.competition_id} team_id={self.team_id} "
            f"checkpoint_id={self.checkpoint_id}>"
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
        # A team cannot have the same group twice.
        UniqueConstraint("team_id", "group_id", name="uq_team_group"),
        # Enforce the docstring above: at most one active group per team.
        # Live arrivals picks the first active group via list[0], which is
        # nondeterministic when multiple actives exist; this index makes
        # that state unrepresentable at the DB level.
        Index(
            "uq_team_group_one_active",
            "team_id",
            unique=True,
            sqlite_where=db.text("active = 1"),
        ),
    )

    def __repr__(self) -> str:
        return f"<TeamGroup id={self.id} team_id={self.team_id} group_id={self.group_id} active={self.active}>"


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
    name = db.Column(db.String(120), nullable=True)  # friendly label
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

    __table_args__ = (UniqueConstraint("competition_id", "dev_num", name="uq_device_competition_devnum"),)

    def __repr__(self) -> str:
        return f"<LoRaDevice id={self.id} comp={self.competition_id} dev_num={self.dev_num} name={self.name!r}>"


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
    received_at = db.Column(db.DateTime, default=utcnow_naive, index=True)

    competition = db.relationship("Competition")

    # The 10-second dedup query in /api/ingest filters on
    # (competition_id, dev_id, received_at >= cutoff) on every packet.
    # A composite index makes that lookup an O(log n) seek instead of
    # touching every recent row per dev_id.
    __table_args__ = (
        Index(
            "ix_lora_messages_dedup",
            "competition_id",
            "dev_id",
            "received_at",
        ),
    )

    def __repr__(self) -> str:
        return f"<LoRaMessage id={self.id} competition_id={self.competition_id} dev_id={self.dev_id!r}>"


# =========================
# Firmware files (for web flasher)
# =========================
class FirmwareFile(db.Model):
    __tablename__ = "firmware_files"

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(
        db.Integer, db.ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = db.Column(db.String(120), nullable=False)
    device_type = db.Column(db.String(20), nullable=False)  # "receiver" | "sender"
    version = db.Column(db.String(40), nullable=True)
    filename = db.Column(db.String(255), nullable=False)  # {uuid}_{secure_original}.bin on disk
    nvs_offset = db.Column(db.Integer, nullable=False, default=0xD000)  # sec_nvs (encrypted)
    nvs_size = db.Column(db.Integer, nullable=False, default=0x3000)
    nvs_keys_offset = db.Column(db.Integer, nullable=False, default=0xC000)  # nvs_keys partition
    app_offset = db.Column(db.Integer, nullable=False, default=0x10000)
    uploaded_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False)
    uploaded_by_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True)

    competition = db.relationship("Competition", back_populates="firmware_files")
    uploaded_by = db.relationship("User", foreign_keys=[uploaded_by_user_id])

    __table_args__ = (
        UniqueConstraint("competition_id", "filename", name="uq_firmware_comp_filename"),
        CheckConstraint("device_type IN ('receiver','sender')", name="ck_firmware_device_type"),
    )

    def __repr__(self) -> str:
        return f"<FirmwareFile id={self.id} comp={self.competition_id} name={self.name!r} type={self.device_type!r}>"


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
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False)

    competition = db.relationship("Competition", back_populates="sheets")
    checkpoint = db.relationship("Checkpoint")

    __table_args__ = (UniqueConstraint("spreadsheet_id", "tab_name", name="uq_sheet_tab"),)

    def __repr__(self) -> str:
        return (
            f"<SheetConfig id={self.id} competition_id={self.competition_id} "
            f"tab_type={self.tab_type!r} tab_name={self.tab_name!r}>"
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
    checkin_id = db.Column(db.Integer, db.ForeignKey("checkins.id", ondelete="CASCADE"), nullable=True, index=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True)
    checkpoint_id = db.Column(
        db.Integer, db.ForeignKey("checkpoints.id", ondelete="CASCADE"), nullable=False, index=True
    )
    judge_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    raw_fields = db.Column(db.JSON, nullable=False, default=dict)
    total = db.Column(db.Float, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False, index=True)

    checkin = db.relationship("Checkin", back_populates="scores")
    team = db.relationship("Team")
    checkpoint = db.relationship("Checkpoint")
    judge_user = db.relationship("User")

    def __repr__(self) -> str:
        return (
            f"<ScoreEntry id={self.id} competition_id={self.competition_id} "
            f"team_id={self.team_id} checkpoint_id={self.checkpoint_id}>"
        )


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
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False, index=True)

    checkpoint = db.relationship("Checkpoint")
    group = db.relationship("CheckpointGroup")

    __table_args__ = (UniqueConstraint("competition_id", "checkpoint_id", "group_id", name="uq_score_rule_scope"),)

    def __repr__(self) -> str:
        return (
            f"<ScoreRule id={self.id} competition_id={self.competition_id} "
            f"checkpoint_id={self.checkpoint_id} group_id={self.group_id}>"
        )


# =========================
# AuditEvent (append-only audit trail)
# =========================
class AuditEvent(db.Model):
    __tablename__ = "audit_events"

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(
        db.Integer, db.ForeignKey("competitions.id", ondelete="CASCADE"), nullable=False, index=True
    )
    event_type = db.Column(db.String(64), nullable=False, index=True)
    entity_type = db.Column(db.String(64), nullable=False, index=True)
    entity_id = db.Column(db.Integer, nullable=True, index=True)
    actor_type = db.Column(db.String(20), nullable=False, default="system", index=True)
    actor_user_id = db.Column(db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    actor_device_id = db.Column(
        db.Integer, db.ForeignKey("lora_devices.id", ondelete="SET NULL"), nullable=True, index=True
    )
    actor_label = db.Column(db.String(255), nullable=True)
    summary = db.Column(db.String(255), nullable=False)
    details = db.Column(db.JSON, nullable=True)
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False, index=True)

    competition = db.relationship("Competition")
    actor_user = db.relationship("User", foreign_keys=[actor_user_id])
    actor_device = db.relationship("LoRaDevice", foreign_keys=[actor_device_id])

    def __repr__(self) -> str:
        return (
            f"<AuditEvent id={self.id} competition_id={self.competition_id} "
            f"event_type={self.event_type!r} entity_type={self.entity_type!r}>"
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
    created_at = db.Column(db.DateTime, default=utcnow_naive, nullable=False, index=True)

    group = db.relationship("CheckpointGroup")

    __table_args__ = (UniqueConstraint("competition_id", "group_id", name="uq_global_score_rule_scope"),)

    def __repr__(self) -> str:
        return f"<GlobalScoreRule id={self.id} competition_id={self.competition_id} group_id={self.group_id}>"


@event.listens_for(Path.stops, "append")
def _on_path_stop_append(path: Path, stop: PathStop, *_):
    """Assign the next position to stops appended via path.stops."""
    if stop.position is not None:
        return
    existing = [s.position for s in path.stops if s is not stop and s.position is not None]
    stop.position = (max(existing) + 1) if existing else 0
