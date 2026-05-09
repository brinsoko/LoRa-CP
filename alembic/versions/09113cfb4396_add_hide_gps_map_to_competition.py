"""add hide_gps_map to competition

Revision ID: 09113cfb4396
Revises: c8c7404f63fa
Create Date: 2026-04-12 16:23:43.490793

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = '09113cfb4396'
down_revision: Union[str, None] = 'c8c7404f63fa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Idempotent: the column is created by db.metadata.create_all in
    # the initial revision (which now bootstraps the full schema) and
    # may also be present on legacy DBs that ran the boot-time
    # ALTER TABLE blocks. Skip the ADD if it already exists.
    insp = inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("competitions")}
    if "hide_gps_map" in cols:
        return
    with op.batch_alter_table('competitions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('hide_gps_map', sa.Boolean(), server_default='0', nullable=False))


def downgrade() -> None:
    insp = inspect(op.get_bind())
    cols = {c["name"] for c in insp.get_columns("competitions")}
    if "hide_gps_map" not in cols:
        return
    with op.batch_alter_table('competitions', schema=None) as batch_op:
        batch_op.drop_column('hide_gps_map')
