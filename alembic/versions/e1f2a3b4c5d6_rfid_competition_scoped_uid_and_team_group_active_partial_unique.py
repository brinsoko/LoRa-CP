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
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1f2a3b4c5d6'
down_revision: Union[str, None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- 1) RFID composite UID ---

    # Add competition_id nullable so the backfill can run.
    with op.batch_alter_table('rfid_cards', schema=None) as batch_op:
        batch_op.add_column(sa.Column('competition_id', sa.Integer(), nullable=True))

    # Backfill from the team's competition. SQLite doesn't support
    # UPDATE...FROM in older versions, but does support correlated
    # subqueries which work everywhere.
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

    # Now make competition_id NOT NULL, drop the old global unique on uid,
    # and add the composite unique + FK + index. SQLite ALTER TABLE has
    # severe limits, so batch_alter_table copies the table.
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
        # The auto-generated unique constraint on `uid` was named
        # `rfid_cards_uid_key` on Postgres; on SQLite it's an unnamed
        # UNIQUE column. batch_alter_table reissues the table without
        # the column-level UNIQUE if we redefine the column.
        batch_op.alter_column('uid', existing_type=sa.String(length=100), nullable=False)
        batch_op.create_unique_constraint('uq_rfid_competition_uid', ['competition_id', 'uid'])

    # SQLite preserves the column-level UNIQUE through batch alter unless
    # we explicitly reissue without it. Drop the auto-named index that
    # backed the old uid unique, in case it survived. Use IF EXISTS so
    # this is harmless if Alembic already rebuilt the table cleanly.
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX IF EXISTS ix_rfid_cards_uid")
        op.execute('DROP INDEX IF EXISTS "rfid_cards_uid_key"')

    # --- 2) TeamGroup: at most one active group per team ---
    # Best-effort cleanup: if a team has multiple active rows, keep the
    # most recent (highest id) and demote the rest to inactive. This
    # avoids the new index failing to build on a dirty DB.
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
    op.drop_index('uq_team_group_one_active', table_name='team_groups')

    with op.batch_alter_table('rfid_cards', schema=None) as batch_op:
        batch_op.drop_constraint('uq_rfid_competition_uid', type_='unique')
        batch_op.drop_constraint('fk_rfid_cards_competition_id', type_='foreignkey')
        batch_op.drop_index('ix_rfid_cards_competition_id')
        batch_op.drop_column('competition_id')
        batch_op.create_unique_constraint('rfid_cards_uid_key', ['uid'])
