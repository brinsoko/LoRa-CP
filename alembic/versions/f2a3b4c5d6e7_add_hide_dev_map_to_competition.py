"""add hide_dev_map to competition

Revision ID: f2a3b4c5d6e7
Revises: e1f2a3b4c5d6
Create Date: 2026-05-21 11:00:00.000000

Splits the single hide_gps_map flag into two so admins can hide just the
device (LoRa) map while keeping the public GPS map visible (or vice
versa). hide_gps_map keeps its existing meaning (hides the public Map
button); hide_dev_map is new and hides the Device Map button.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


revision: str = 'f2a3b4c5d6e7'
down_revision: Union[str, None] = 'e1f2a3b4c5d6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    insp = inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("competitions")}
    if "hide_dev_map" in cols:
        return
    with op.batch_alter_table('competitions', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('hide_dev_map', sa.Boolean(), server_default='0', nullable=False)
        )


def downgrade() -> None:
    insp = inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("competitions")}
    if "hide_dev_map" not in cols:
        return
    with op.batch_alter_table('competitions', schema=None) as batch_op:
        batch_op.drop_column('hide_dev_map')
