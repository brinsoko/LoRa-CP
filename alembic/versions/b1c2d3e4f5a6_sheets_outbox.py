"""sheets_sync_jobs: durable outbox for Google Sheets writes.

Replaces the in-memory per-process queue (redesign plan 3.4). Guarded
create for the create_all() bootstrap path.

Revision ID: b1c2d3e4f5a6
Revises: a0b1c2d3e4f5
Create Date: 2026-07-09
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "b1c2d3e4f5a6"
down_revision: Union[str, None] = "a0b1c2d3e4f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    insp = inspect(op.get_bind())
    if "sheets_sync_jobs" not in set(insp.get_table_names()):
        op.create_table(
            "sheets_sync_jobs",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "competition_id",
                sa.Integer(),
                sa.ForeignKey("competitions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("kind", sa.String(length=40), nullable=False),
            sa.Column("payload", sa.JSON(), nullable=True),
            sa.Column("dedup_key", sa.String(length=200), nullable=False),
            sa.Column("status", sa.String(length=10), nullable=False, server_default="pending"),
            sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("next_attempt_at", sa.DateTime(), nullable=True),
            sa.Column("last_error", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("updated_at", sa.DateTime(), nullable=False),
            sa.CheckConstraint(
                "status IN ('pending','running','done','failed')", name="ck_sheets_job_status"
            ),
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_sheets_sync_jobs_competition_id ON sheets_sync_jobs (competition_id)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_sheets_sync_jobs_kind ON sheets_sync_jobs (kind)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_sheets_sync_jobs_dedup_key ON sheets_sync_jobs (dedup_key)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_sheets_sync_jobs_status ON sheets_sync_jobs (status)")


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS sheets_sync_jobs")
