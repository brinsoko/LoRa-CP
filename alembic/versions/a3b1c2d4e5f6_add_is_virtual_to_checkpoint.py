"""add is_virtual to checkpoint, make score_entry.checkin_id nullable

Revision ID: a3b1c2d4e5f6
Revises: 09113cfb4396
Create Date: 2026-04-12 20:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a3b1c2d4e5f6'
down_revision: Union[str, None] = '09113cfb4396'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('checkpoints', schema=None) as batch_op:
        batch_op.add_column(sa.Column('is_virtual', sa.Boolean(), server_default='0', nullable=False))

    with op.batch_alter_table('score_entries', schema=None) as batch_op:
        batch_op.alter_column('checkin_id', existing_type=sa.Integer(), nullable=True)


def downgrade() -> None:
    with op.batch_alter_table('score_entries', schema=None) as batch_op:
        batch_op.alter_column('checkin_id', existing_type=sa.Integer(), nullable=False)

    with op.batch_alter_table('checkpoints', schema=None) as batch_op:
        batch_op.drop_column('is_virtual')
