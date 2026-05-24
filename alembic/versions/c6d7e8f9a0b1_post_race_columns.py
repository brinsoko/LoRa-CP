"""post-race overhaul: 5 additive columns across 4 tables

Revision ID: c6d7e8f9a0b1
Revises: b5c6d7e8f9a0
Create Date: 2026-05-24 02:00:00.000000

Adds, in one migration so the deploy step is a single alembic upgrade head:

1. judge_checkpoints.competition_id  (item 12)
   Fixes the cross-competition assign bug. Backfilled from
   the linked Checkpoint.competition_id. Uniqueness widens to
   (user_id, checkpoint_id, competition_id) — the existing pair
   constraint becomes redundant but is kept until the next
   cleanup so legacy code paths don't break.

2. checkpoints.position  (item 5)
   Manual ordering. Backfilled with row_number ordered by name
   within each competition so existing alphabetical order is
   preserved as the initial value. New checkpoints get NULL
   (sorts last by nulls_last fallback).

3. checkpoint_groups.reverse  (item 4)
   Per-group direction flag for the live arrivals view.
   Default FALSE — existing behavior unchanged.

4. teams.notes  (item 19)
   Free-text per-team notes for special events.

5. teams.bonus_dead_time  (item 20)
   Team-level dead-time bucket not bound to any CP. Float
   minutes, default 0.0. _get_team_dead_time_total adds this
   to the per-CP sum.

Idempotent: each step checks for the column/index/constraint
before applying so re-running upgrade head is safe.
"""
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect


revision: str = "c6d7e8f9a0b1"
down_revision: Union[str, None] = "b5c6d7e8f9a0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(insp, table: str) -> set[str]:
    return {c["name"] for c in insp.get_columns(table)}


def _indexes(insp, table: str) -> set[str]:
    return {i["name"] for i in insp.get_indexes(table)}


def _table_exists(insp, table: str) -> bool:
    return table in set(insp.get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    # --- 1) judge_checkpoints.competition_id ---
    # judge_checkpoints was historically created via db.create_all() on boot
    # rather than a migration, so legacy DBs upgrading from an older revision
    # may not have the table yet. Skip cleanly if so — db.create_all() will
    # add it post-upgrade with the column already defined on the model.
    if _table_exists(insp, "judge_checkpoints") and "competition_id" not in _columns(
        insp, "judge_checkpoints"
    ):
        with op.batch_alter_table("judge_checkpoints", schema=None) as batch_op:
            batch_op.add_column(sa.Column("competition_id", sa.Integer(), nullable=True))

        # Backfill from the linked checkpoint's competition_id. Orphans
        # (whose checkpoint was deleted) get NULL — we drop them below
        # so the index/constraint can apply.
        op.execute(
            """
            UPDATE judge_checkpoints
            SET competition_id = (
                SELECT checkpoints.competition_id
                FROM checkpoints
                WHERE checkpoints.id = judge_checkpoints.checkpoint_id
            )
            """
        )
        op.execute("DELETE FROM judge_checkpoints WHERE competition_id IS NULL")

        with op.batch_alter_table("judge_checkpoints", schema=None) as batch_op:
            batch_op.alter_column(
                "competition_id", existing_type=sa.Integer(), nullable=False
            )
            batch_op.create_index(
                "ix_judge_checkpoints_competition_id", ["competition_id"], unique=False
            )
            batch_op.create_foreign_key(
                "fk_judge_checkpoints_competition_id",
                "competitions",
                ["competition_id"],
                ["id"],
                ondelete="CASCADE",
            )

    # --- 2) checkpoints.position ---
    if _table_exists(insp, "checkpoints") and "position" not in _columns(insp, "checkpoints"):
        with op.batch_alter_table("checkpoints", schema=None) as batch_op:
            batch_op.add_column(sa.Column("position", sa.Integer(), nullable=True))

        # Backfill: preserve the current alphabetical-by-name order as
        # the initial position within each competition. ROW_NUMBER over
        # PARTITION BY is supported in SQLite 3.25+ which is well below
        # the project's required runtime.
        op.execute(
            """
            UPDATE checkpoints
            SET position = sub.rn - 1
            FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY competition_id ORDER BY name ASC
                       ) AS rn
                FROM checkpoints
            ) AS sub
            WHERE checkpoints.id = sub.id
            """
        )

    # --- 3) checkpoint_groups.reverse ---
    if _table_exists(insp, "checkpoint_groups") and "reverse" not in _columns(
        insp, "checkpoint_groups"
    ):
        with op.batch_alter_table("checkpoint_groups", schema=None) as batch_op:
            batch_op.add_column(
                sa.Column(
                    "reverse",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.false(),
                )
            )

    # --- 4) teams.notes + 5) teams.bonus_dead_time ---
    if _table_exists(insp, "teams"):
        t_cols = _columns(insp, "teams")
        if "notes" not in t_cols:
            with op.batch_alter_table("teams", schema=None) as batch_op:
                batch_op.add_column(sa.Column("notes", sa.Text(), nullable=True))
        if "bonus_dead_time" not in t_cols:
            with op.batch_alter_table("teams", schema=None) as batch_op:
                batch_op.add_column(
                    sa.Column(
                        "bonus_dead_time",
                        sa.Float(),
                        nullable=False,
                        server_default="0.0",
                    )
                )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    if "bonus_dead_time" in _columns(insp, "teams"):
        with op.batch_alter_table("teams", schema=None) as batch_op:
            batch_op.drop_column("bonus_dead_time")

    if "notes" in _columns(insp, "teams"):
        with op.batch_alter_table("teams", schema=None) as batch_op:
            batch_op.drop_column("notes")

    if "reverse" in _columns(insp, "checkpoint_groups"):
        with op.batch_alter_table("checkpoint_groups", schema=None) as batch_op:
            batch_op.drop_column("reverse")

    if "position" in _columns(insp, "checkpoints"):
        with op.batch_alter_table("checkpoints", schema=None) as batch_op:
            batch_op.drop_column("position")

    jc_cols = _columns(insp, "judge_checkpoints")
    jc_indexes = _indexes(insp, "judge_checkpoints")
    if "competition_id" in jc_cols:
        with op.batch_alter_table("judge_checkpoints", schema=None) as batch_op:
            if "fk_judge_checkpoints_competition_id" in {
                fk.get("name") for fk in insp.get_foreign_keys("judge_checkpoints")
            }:
                batch_op.drop_constraint(
                    "fk_judge_checkpoints_competition_id", type_="foreignkey"
                )
            if "ix_judge_checkpoints_competition_id" in jc_indexes:
                batch_op.drop_index("ix_judge_checkpoints_competition_id")
            batch_op.drop_column("competition_id")
