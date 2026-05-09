"""codify columns/index that were being added at app startup as runtime DDL

Revision ID: d4e5f6a7b8c9
Revises: a3b1c2d4e5f6
Create Date: 2026-05-09 00:00:00.000000

These columns and the lora_messages dedup index were previously created
at app boot via inspect()/ALTER TABLE blocks in app/__init__.py because
they post-dated the initial Alembic schema and never got their own
revisions. Per the audit, that lets prod schema state drift silently —
fresh installs got everything via db.create_all(), upgrades relied on
the boot-time ALTER blocks, and Alembic had no record of any of it.

This revision encodes them into the migration chain so:
  - Fresh installs continue to work (db.create_all() picks them up
    from the SQLAlchemy models and Alembic stamps to head).
  - Existing prod DBs upgrade via `alembic upgrade head`.
  - The startup ALTER TABLE blocks in app/__init__.py become redundant
    and are removed in the same commit.

Each column is added if-not-exists semantics via op.add_column inside
batch_alter_table; if the column already exists (e.g. a DB that ran
the old startup DDL), the migration will fail and that's fine — that
DB is already at the target state and should `alembic stamp` to mark
it. The accompanying commit message documents the deploy step.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'a3b1c2d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('competitions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('ingest_password_hash', sa.String(length=255), nullable=True))

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.add_column(sa.Column('last_competition_id', sa.Integer(), nullable=True))
        batch_op.create_index(
            'ix_users_last_competition_id', ['last_competition_id'], unique=False
        )

    with op.batch_alter_table('checkins', schema=None) as batch_op:
        batch_op.add_column(sa.Column('created_by_user_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('created_by_device_id', sa.Integer(), nullable=True))
        batch_op.create_foreign_key(
            'fk_checkins_created_by_user',
            'users',
            ['created_by_user_id'],
            ['id'],
            ondelete='SET NULL',
        )
        batch_op.create_foreign_key(
            'fk_checkins_created_by_device',
            'lora_devices',
            ['created_by_device_id'],
            ['id'],
            ondelete='SET NULL',
        )
        batch_op.create_index(
            'ix_checkins_created_by_user_id', ['created_by_user_id'], unique=False
        )
        batch_op.create_index(
            'ix_checkins_created_by_device_id', ['created_by_device_id'], unique=False
        )

    # firmware_files.nvs_size — historical default was 20480 bytes; the
    # current model default is 0x3000 (12288), but existing rows should
    # keep whatever value they had. New rows pick up the model default.
    with op.batch_alter_table('firmware_files', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column('nvs_size', sa.Integer(), server_default='20480', nullable=False)
        )

    # Composite index on lora_messages used by the ingest dedup query.
    # db.create_all() only emits indexes for tables it creates fresh, so
    # existing DBs need this added explicitly.
    op.create_index(
        'ix_lora_messages_dedup',
        'lora_messages',
        ['competition_id', 'dev_id', 'received_at'],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index('ix_lora_messages_dedup', table_name='lora_messages')

    with op.batch_alter_table('firmware_files', schema=None) as batch_op:
        batch_op.drop_column('nvs_size')

    with op.batch_alter_table('checkins', schema=None) as batch_op:
        batch_op.drop_index('ix_checkins_created_by_device_id')
        batch_op.drop_index('ix_checkins_created_by_user_id')
        batch_op.drop_constraint('fk_checkins_created_by_device', type_='foreignkey')
        batch_op.drop_constraint('fk_checkins_created_by_user', type_='foreignkey')
        batch_op.drop_column('created_by_device_id')
        batch_op.drop_column('created_by_user_id')

    with op.batch_alter_table('users', schema=None) as batch_op:
        batch_op.drop_index('ix_users_last_competition_id')
        batch_op.drop_column('last_competition_id')

    with op.batch_alter_table('competitions', schema=None) as batch_op:
        batch_op.drop_column('ingest_password_hash')
