"""checkpoints: hide_from_results flag

Revision ID: d7e8f9a0b1c2
Revises: c6d7e8f9a0b1
Create Date: 2026-05-24 22:00:00.000000

Adds Checkpoint.hide_from_results — when True, the CP doesn't render as a
column in the per-CP scoreboard or get its own per-CP Sheet column, but
check-ins are still recorded so we can track when a team reaches it
(typical use: the actual finish line, which is also a time-trial leg's
end_cp — we don't want a "Cilj: 0" column on the scoreboard but we do
need to know when teams crossed it).

Uses plain ALTER TABLE ADD COLUMN (not batch_alter_table) so SQLite
doesn't trigger CASCADE deletes on child tables — the c6d7e8f9a0b1
migration's batch_alter_table on this same table wiped team_groups +
global_score_rules in prod, and we're not repeating that.

Idempotent and guarded against missing tables.
"""
from collections.abc import Sequence
from typing import Union

from alembic import op
from sqlalchemy import inspect


revision: str = "d7e8f9a0b1c2"
down_revision: Union[str, None] = "c6d7e8f9a0b1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(insp, table: str) -> set[str]:
    return {c["name"] for c in insp.get_columns(table)}


def _table_exists(insp, table: str) -> bool:
    return table in set(insp.get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)
    if _table_exists(insp, "checkpoints") and "hide_from_results" not in _columns(insp, "checkpoints"):
        # Plain ADD COLUMN — does NOT rebuild the table, so no cascade
        # deletes on FK-referencing children. Safe to run online.
        op.execute(
            "ALTER TABLE checkpoints ADD COLUMN hide_from_results BOOLEAN NOT NULL DEFAULT 0"
        )


def downgrade() -> None:
    # SQLite can drop columns since 3.35, but the project supports older
    # SQLite via batch_alter_table. We don't expect to downgrade this in
    # practice; leave the column behind if downgrade is invoked.
    pass
