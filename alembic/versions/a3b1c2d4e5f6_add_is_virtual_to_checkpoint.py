"""add is_virtual to checkpoint, make score_entry.checkin_id nullable

Revision ID: a3b1c2d4e5f6
Revises: 09113cfb4396
Create Date: 2026-04-12 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = 'a3b1c2d4e5f6'
down_revision: Union[str, None] = '09113cfb4396'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent: the initial revision bootstraps the full schema via
    # db.metadata.create_all, so is_virtual is already present on
    # fresh DBs. Legacy DBs that ran the boot-time ALTER TABLE blocks
    # may also already have it. Only ALTER what's actually missing.
    insp = inspect(op.get_bind())

    cp_cols = {c["name"] for c in insp.get_columns("checkpoints")}
    if "is_virtual" not in cp_cols:
        with op.batch_alter_table('checkpoints', schema=None) as batch_op:
            batch_op.add_column(sa.Column('is_virtual', sa.Boolean(), server_default='0', nullable=False))

    score_cols = {c["name"]: c for c in insp.get_columns("score_entries")}
    checkin_col = score_cols.get("checkin_id")
    if checkin_col is not None and not checkin_col.get("nullable", True):
        with op.batch_alter_table('score_entries', schema=None) as batch_op:
            batch_op.alter_column('checkin_id', existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    insp = inspect(op.get_bind())

    score_cols = {c["name"]: c for c in insp.get_columns("score_entries")}
    if score_cols.get("checkin_id", {}).get("nullable", True):
        with op.batch_alter_table('score_entries', schema=None) as batch_op:
            batch_op.alter_column('checkin_id', existing_type=sa.Integer(), nullable=False)

    cp_cols = {c["name"] for c in insp.get_columns("checkpoints")}
    if "is_virtual" in cp_cols:
        with op.batch_alter_table('checkpoints', schema=None) as batch_op:
            batch_op.drop_column('is_virtual')
