"""codify columns/index that were being added at app startup as runtime DDL

Revision ID: d4e5f6a7b8c9
Revises: a3b1c2d4e5f6
Create Date: 2026-05-09 00:00:00.000000

These columns and the lora_messages dedup index were previously created
at app boot via inspect()/ALTER TABLE blocks in app/__init__.py because
they post-dated the initial Alembic schema and never got their own
revisions. Per the audit, that let prod schema state drift silently —
fresh installs got everything via db.create_all(), upgrades relied on
the boot-time ALTER blocks, and Alembic had no record of any of it.

This revision encodes them into the migration chain. Every operation
is idempotent so it works against:
  - Fresh DBs where the initial revision (now non-empty) bootstrapped
    the full schema via db.metadata.create_all — every column already
    exists, so this revision becomes a no-op.
  - Legacy prod DBs that previously ran the boot-time ALTER blocks —
    some columns will already exist; this revision adds only what's
    actually missing.
  - Truly old DBs that have none of this — every column is added.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'a3b1c2d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _table_columns(insp, table: str) -> set[str]:
    return {c["name"] for c in insp.get_columns(table)}


def _table_indexes(insp, table: str) -> set[str]:
    return {i["name"] for i in insp.get_indexes(table)}


def _table_fks(insp, table: str) -> set[str]:
    return {fk["name"] for fk in insp.get_foreign_keys(table) if fk.get("name")}


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    comp_cols = _table_columns(insp, "competitions")
    if "ingest_password_hash" not in comp_cols:
        with op.batch_alter_table('competitions', schema=None) as batch_op:
            batch_op.add_column(sa.Column('ingest_password_hash', sa.String(length=255), nullable=True))

    user_cols = _table_columns(insp, "users")
    user_indexes = _table_indexes(insp, "users")
    if "last_competition_id" not in user_cols:
        with op.batch_alter_table('users', schema=None) as batch_op:
            batch_op.add_column(sa.Column('last_competition_id', sa.Integer(), nullable=True))
    if "ix_users_last_competition_id" not in user_indexes:
        with op.batch_alter_table('users', schema=None) as batch_op:
            batch_op.create_index(
                'ix_users_last_competition_id', ['last_competition_id'], unique=False
            )

    checkin_cols = _table_columns(insp, "checkins")
    checkin_indexes = _table_indexes(insp, "checkins")
    checkin_fks = _table_fks(insp, "checkins")

    columns_to_add = []
    if "created_by_user_id" not in checkin_cols:
        columns_to_add.append(sa.Column('created_by_user_id', sa.Integer(), nullable=True))
    if "created_by_device_id" not in checkin_cols:
        columns_to_add.append(sa.Column('created_by_device_id', sa.Integer(), nullable=True))
    if columns_to_add:
        with op.batch_alter_table('checkins', schema=None) as batch_op:
            for col in columns_to_add:
                batch_op.add_column(col)
        # Refresh inspector after schema change.
        insp = inspect(bind)
        checkin_indexes = _table_indexes(insp, "checkins")
        checkin_fks = _table_fks(insp, "checkins")

    # FKs and indexes are added in a single batch when at least one is
    # missing, but each operation is guarded so partial states don't
    # double-add.
    fk_ops_needed = (
        "fk_checkins_created_by_user" not in checkin_fks
        or "fk_checkins_created_by_device" not in checkin_fks
        or "ix_checkins_created_by_user_id" not in checkin_indexes
        or "ix_checkins_created_by_device_id" not in checkin_indexes
    )
    if fk_ops_needed and "created_by_user_id" in _table_columns(insp, "checkins"):
        with op.batch_alter_table('checkins', schema=None) as batch_op:
            if "fk_checkins_created_by_user" not in checkin_fks:
                batch_op.create_foreign_key(
                    'fk_checkins_created_by_user',
                    'users',
                    ['created_by_user_id'],
                    ['id'],
                    ondelete='SET NULL',
                )
            if "fk_checkins_created_by_device" not in checkin_fks:
                batch_op.create_foreign_key(
                    'fk_checkins_created_by_device',
                    'lora_devices',
                    ['created_by_device_id'],
                    ['id'],
                    ondelete='SET NULL',
                )
            if "ix_checkins_created_by_user_id" not in checkin_indexes:
                batch_op.create_index(
                    'ix_checkins_created_by_user_id', ['created_by_user_id'], unique=False
                )
            if "ix_checkins_created_by_device_id" not in checkin_indexes:
                batch_op.create_index(
                    'ix_checkins_created_by_device_id', ['created_by_device_id'], unique=False
                )

    fw_cols = _table_columns(insp, "firmware_files")
    if "nvs_size" not in fw_cols:
        # firmware_files.nvs_size — historical default was 20480 bytes;
        # the current model default is 0x3000 (12288), but existing
        # rows should keep whatever value they had. New rows pick up
        # the model default.
        with op.batch_alter_table('firmware_files', schema=None) as batch_op:
            batch_op.add_column(
                sa.Column('nvs_size', sa.Integer(), server_default='20480', nullable=False)
            )

    # Composite index on lora_messages used by the ingest dedup query.
    # db.create_all() only emits indexes for tables it creates fresh, so
    # legacy DBs may need this added explicitly. The IF NOT EXISTS form
    # handles every case in one statement.
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_lora_messages_dedup "
        "ON lora_messages (competition_id, dev_id, received_at)"
    )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    if "ix_lora_messages_dedup" in _table_indexes(insp, "lora_messages"):
        op.drop_index('ix_lora_messages_dedup', table_name='lora_messages')

    if "nvs_size" in _table_columns(insp, "firmware_files"):
        with op.batch_alter_table('firmware_files', schema=None) as batch_op:
            batch_op.drop_column('nvs_size')

    checkin_indexes = _table_indexes(insp, "checkins")
    checkin_fks = _table_fks(insp, "checkins")
    checkin_cols = _table_columns(insp, "checkins")

    with op.batch_alter_table('checkins', schema=None) as batch_op:
        if "ix_checkins_created_by_device_id" in checkin_indexes:
            batch_op.drop_index('ix_checkins_created_by_device_id')
        if "ix_checkins_created_by_user_id" in checkin_indexes:
            batch_op.drop_index('ix_checkins_created_by_user_id')
        if "fk_checkins_created_by_device" in checkin_fks:
            batch_op.drop_constraint('fk_checkins_created_by_device', type_='foreignkey')
        if "fk_checkins_created_by_user" in checkin_fks:
            batch_op.drop_constraint('fk_checkins_created_by_user', type_='foreignkey')
        if "created_by_device_id" in checkin_cols:
            batch_op.drop_column('created_by_device_id')
        if "created_by_user_id" in checkin_cols:
            batch_op.drop_column('created_by_user_id')

    user_indexes = _table_indexes(insp, "users")
    user_cols = _table_columns(insp, "users")
    with op.batch_alter_table('users', schema=None) as batch_op:
        if "ix_users_last_competition_id" in user_indexes:
            batch_op.drop_index('ix_users_last_competition_id')
        if "last_competition_id" in user_cols:
            batch_op.drop_column('last_competition_id')

    if "ingest_password_hash" in _table_columns(insp, "competitions"):
        with op.batch_alter_table('competitions', schema=None) as batch_op:
            batch_op.drop_column('ingest_password_hash')
