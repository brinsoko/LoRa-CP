"""checkpoints: bulk_entry_enabled flag for the judge shell's grid.

Stations like written tests are scored offline, sorted by team number,
and entered in one sitting; the flag opts a checkpoint into the bulk
entry grid (redesign plan 3.6). Guarded plain ADD COLUMN per house style.

Revision ID: a0b1c2d3e4f5
Revises: f9a0b1c2d3e4
Create Date: 2026-07-09
"""

from collections.abc import Sequence
from typing import Union

from alembic import op
from sqlalchemy import inspect

revision: str = "a0b1c2d3e4f5"
down_revision: Union[str, None] = "f9a0b1c2d3e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    insp = inspect(op.get_bind())
    if "checkpoints" not in set(insp.get_table_names()):
        return
    columns = {c["name"] for c in insp.get_columns("checkpoints")}
    if "bulk_entry_enabled" not in columns:
        op.execute("ALTER TABLE checkpoints ADD COLUMN bulk_entry_enabled BOOLEAN NOT NULL DEFAULT 0")


def downgrade() -> None:
    # Leave the column behind; nothing reads it on old code and dropping
    # would require a rebuild on old SQLite.
    pass
