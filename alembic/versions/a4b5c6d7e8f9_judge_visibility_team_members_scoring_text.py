"""judge visibility flags, team_members table, checkpoint scoring_text

Revision ID: a4b5c6d7e8f9
Revises: f2a3b4c5d6e7
Create Date: 2026-05-21 11:30:00.000000

Bundles three race-day prep additions into one migration so the prod
deploy needs only one DB upgrade:

  1. Two new boolean flags on Competition so admins can hide the audit
     log / messages link and the score-submissions log from judges
     (current default: visible).
  2. Free-text scoring_text column on Checkpoint - the human-readable
     scoring rules the judge sees on the score form. NULL means
     "fall back to the auto-generated description from score rules".
  3. team_members table - one row per scout in a team, with name and
     optional role/note. Created with a unique (team_id, position)
     index so display order is stable across reloads.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = 'a4b5c6d7e8f9'
down_revision: Union[str, None] = 'f2a3b4c5d6e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    insp = inspect(op.get_bind())
    existing_tables = set(insp.get_table_names())

    # Competition: two new booleans for the visibility dropdown. Some
    # legacy upgrade paths don't even have a competitions table yet (they
    # arrive here via the very first migration that codifies the schema),
    # so guard the inspection as well.
    if "competitions" in existing_tables:
        comp_cols = {c["name"] for c in insp.get_columns("competitions")}
        new_comp_cols = [
            ("hide_audit_messages", sa.Boolean(), "0"),
            ("hide_score_submissions", sa.Boolean(), "0"),
        ]
        pending_comp = [(n, t, d) for (n, t, d) in new_comp_cols if n not in comp_cols]
        if pending_comp:
            with op.batch_alter_table('competitions', schema=None) as batch_op:
                for name, type_, default in pending_comp:
                    batch_op.add_column(sa.Column(name, type_, server_default=default, nullable=False))

    # Checkpoint: scoring_text free-text override. Legacy test DBs stamped
    # at earlier revisions may not have created the checkpoints table yet
    # (the test exercises an rfid-only upgrade path), so skip silently
    # when missing - a fresh install creates it via db.create_all().
    if "checkpoints" in existing_tables:
        cp_cols = {c["name"] for c in insp.get_columns("checkpoints")}
        if "scoring_text" not in cp_cols:
            with op.batch_alter_table('checkpoints', schema=None) as batch_op:
                batch_op.add_column(sa.Column('scoring_text', sa.Text(), nullable=True))

    # team_members table. Only create when the teams parent table exists,
    # otherwise the FK declaration would fail on legacy upgrade paths.
    if "team_members" not in existing_tables and "teams" in existing_tables:
        op.create_table(
            'team_members',
            sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
            sa.Column(
                'team_id', sa.Integer(),
                sa.ForeignKey('teams.id', ondelete='CASCADE'),
                nullable=False, index=True,
            ),
            sa.Column('name', sa.String(160), nullable=False),
            sa.Column('role', sa.String(80), nullable=True),
            sa.Column('position', sa.Integer(), nullable=False, server_default='0'),
            sa.UniqueConstraint('team_id', 'position', name='uq_team_member_position'),
        )


def downgrade() -> None:
    insp = inspect(op.get_bind())
    existing_tables = set(insp.get_table_names())
    if "team_members" in existing_tables:
        op.drop_table('team_members')
    cp_cols = {c["name"] for c in insp.get_columns("checkpoints")}
    if "scoring_text" in cp_cols:
        with op.batch_alter_table('checkpoints', schema=None) as batch_op:
            batch_op.drop_column('scoring_text')
    comp_cols = {c["name"] for c in insp.get_columns("competitions")}
    with op.batch_alter_table('competitions', schema=None) as batch_op:
        for name in ("hide_audit_messages", "hide_score_submissions"):
            if name in comp_cols:
                batch_op.drop_column(name)
