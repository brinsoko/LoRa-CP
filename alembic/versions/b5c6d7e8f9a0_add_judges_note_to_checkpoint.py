"""add judges_note to checkpoint

Revision ID: b5c6d7e8f9a0
Revises: a4b5c6d7e8f9
Create Date: 2026-05-21 12:00:00.000000

Free-text per-CP "judges / notes" field so admins can record who will be
at a checkpoint even when those people don't have app logins (sub-judges,
parents, volunteers). The structured JudgeCheckpoint table still holds
real assignments; this column is purely informational.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = 'b5c6d7e8f9a0'
down_revision: Union[str, None] = 'a4b5c6d7e8f9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    insp = inspect(op.get_bind())
    existing_tables = set(insp.get_table_names())
    if "checkpoints" not in existing_tables:
        # Legacy rfid-only upgrade path - checkpoints will arrive via
        # db.create_all() on first boot. Skip silently.
        return
    cp_cols = {c["name"] for c in insp.get_columns("checkpoints")}
    if "judges_note" in cp_cols:
        return
    with op.batch_alter_table('checkpoints', schema=None) as batch_op:
        batch_op.add_column(sa.Column('judges_note', sa.Text(), nullable=True))


def downgrade() -> None:
    insp = inspect(op.get_bind())
    existing_tables = set(insp.get_table_names())
    if "checkpoints" not in existing_tables:
        return
    cp_cols = {c["name"] for c in insp.get_columns("checkpoints")}
    if "judges_note" not in cp_cols:
        return
    with op.batch_alter_table('checkpoints', schema=None) as batch_op:
        batch_op.drop_column('judges_note')
