"""rfid: scope UID per competition; team_groups: enforce at-most-one active per team

Revision ID: e1f2a3b4c5d6
Revises: d4e5f6a7b8c9
Create Date: 2026-05-09 00:30:00.000000

Two model invariants that were declared in code but not enforced by the
schema:

1. RFIDCard.uid was globally unique. The operational reality is that
   the same physical scout card is reused across competitions, so a
   global unique constraint blocks re-issuing the card to a new team
   in the next event. Migration adds rfid_cards.competition_id, backfills
   it from teams.competition_id, drops the global unique on uid, and
   replaces it with UNIQUE(competition_id, uid).

2. TeamGroup carried the comment "only one active assignment per team"
   but the DB only had UNIQUE(team_id, group_id). Live arrivals picks
   the first active group via list[0], which is nondeterministic when
   the invariant is violated. Migration adds a partial unique index on
   team_groups(team_id) WHERE active = 1.

Idempotency: the migration is now safe to run against three DB states:
  - Fresh schema produced by db.metadata.create_all() in the initial
    revision — already has competition_id and the composite unique.
    This migration becomes a near no-op (only orphan-active cleanup
    and the partial index need running, both guarded).
  - Legacy DB at d4e5f6a7b8c9 with the old global UNIQUE on uid — the
    full upgrade runs.
  - Partially-migrated DB — each step checks state before applying.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns(insp, table: str) -> set[str]:
    return {c["name"] for c in insp.get_columns(table)}


def _indexes(insp, table: str) -> set[str]:
    return {i["name"] for i in insp.get_indexes(table)}


def _unique_constraints(insp, table: str) -> set[str]:
    return {uc.get("name") for uc in insp.get_unique_constraints(table) if uc.get("name")}


def _fks(insp, table: str) -> set[str]:
    return {fk.get("name") for fk in insp.get_foreign_keys(table) if fk.get("name")}


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    # --- 1) RFID composite UID ---

    rfid_cols = _columns(insp, "rfid_cards")
    rfid_uniques = _unique_constraints(insp, "rfid_cards")
    rfid_fks = _fks(insp, "rfid_cards")
    rfid_indexes = _indexes(insp, "rfid_cards")

    if "competition_id" not in rfid_cols:
        # Add competition_id nullable so the backfill can run.
        with op.batch_alter_table('rfid_cards', schema=None) as batch_op:
            batch_op.add_column(sa.Column('competition_id', sa.Integer(), nullable=True))

        # Backfill from the team's competition. SQLite doesn't support
        # UPDATE...FROM in older versions, but does support correlated
        # subqueries which work everywhere. Orphaned rows (team deleted
        # out from under the FK) get NULL, which the NOT NULL alter
        # below would reject — drop them defensively.
        op.execute(
            """
            UPDATE rfid_cards
            SET competition_id = (
                SELECT teams.competition_id
                FROM teams
                WHERE teams.id = rfid_cards.team_id
            )
            """
        )
        op.execute(
            "DELETE FROM rfid_cards WHERE competition_id IS NULL"
        )

        # Now make competition_id NOT NULL, drop the old global unique
        # on uid, and add the composite unique + FK + index. SQLite
        # ALTER TABLE has severe limits, so batch_alter_table copies
        # the table.
        with op.batch_alter_table('rfid_cards', schema=None) as batch_op:
            batch_op.alter_column('competition_id', existing_type=sa.Integer(), nullable=False)
            batch_op.create_index('ix_rfid_cards_competition_id', ['competition_id'], unique=False)
            batch_op.create_foreign_key(
                'fk_rfid_cards_competition_id',
                'competitions',
                ['competition_id'],
                ['id'],
                ondelete='CASCADE',
            )
            # batch_alter_table reissues the table without the old
            # column-level UNIQUE on uid when we redefine the column.
            batch_op.alter_column('uid', existing_type=sa.String(length=100), nullable=False)
            batch_op.create_unique_constraint('uq_rfid_competition_uid', ['competition_id', 'uid'])

        # Drop the old auto-named uid unique index in case SQLite kept
        # it. IF EXISTS so this is harmless if batch_alter_table
        # already rebuilt the table cleanly.
        with op.get_context().autocommit_block():
            op.execute("DROP INDEX IF EXISTS ix_rfid_cards_uid")
            op.execute('DROP INDEX IF EXISTS "rfid_cards_uid_key"')
    else:
        # Schema came from db.metadata.create_all (fresh install) —
        # competition_id and the composite unique are already in
        # place. Add only what's missing.
        if "ix_rfid_cards_competition_id" not in rfid_indexes:
            with op.batch_alter_table('rfid_cards', schema=None) as batch_op:
                batch_op.create_index('ix_rfid_cards_competition_id', ['competition_id'], unique=False)
        if "fk_rfid_cards_competition_id" not in rfid_fks:
            with op.batch_alter_table('rfid_cards', schema=None) as batch_op:
                batch_op.create_foreign_key(
                    'fk_rfid_cards_competition_id',
                    'competitions',
                    ['competition_id'],
                    ['id'],
                    ondelete='CASCADE',
                )
        if "uq_rfid_competition_uid" not in rfid_uniques:
            with op.batch_alter_table('rfid_cards', schema=None) as batch_op:
                batch_op.create_unique_constraint('uq_rfid_competition_uid', ['competition_id', 'uid'])

    # --- 2) TeamGroup: at most one active group per team ---

    tg_indexes = _indexes(insp, "team_groups")
    if "uq_team_group_one_active" not in tg_indexes:
        # Best-effort cleanup: if a team has multiple active rows, keep
        # the most recent (highest id) and demote the rest to inactive.
        # This avoids the new index failing to build on a dirty DB.
        op.execute(
            """
            UPDATE team_groups
            SET active = 0
            WHERE active = 1
              AND id NOT IN (
                  SELECT MAX(id) FROM team_groups
                  WHERE active = 1
                  GROUP BY team_id
              )
            """
        )

        op.create_index(
            'uq_team_group_one_active',
            'team_groups',
            ['team_id'],
            unique=True,
            sqlite_where=sa.text('active = 1'),
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    if "uq_team_group_one_active" in _indexes(insp, "team_groups"):
        op.drop_index('uq_team_group_one_active', table_name='team_groups')

    rfid_uniques = _unique_constraints(insp, "rfid_cards")
    rfid_fks = _fks(insp, "rfid_cards")
    rfid_indexes = _indexes(insp, "rfid_cards")
    rfid_cols = _columns(insp, "rfid_cards")

    with op.batch_alter_table('rfid_cards', schema=None) as batch_op:
        if "uq_rfid_competition_uid" in rfid_uniques:
            batch_op.drop_constraint('uq_rfid_competition_uid', type_='unique')
        if "fk_rfid_cards_competition_id" in rfid_fks:
            batch_op.drop_constraint('fk_rfid_cards_competition_id', type_='foreignkey')
        if "ix_rfid_cards_competition_id" in rfid_indexes:
            batch_op.drop_index('ix_rfid_cards_competition_id')
        if "competition_id" in rfid_cols:
            batch_op.drop_column('competition_id')
        # Restore the global unique on uid.
        batch_op.create_unique_constraint('rfid_cards_uid_key', ['uid'])
