"""initial schema

Revision ID: c8c7404f63fa
Revises: 
Create Date: 2026-04-12 16:21:26.472463

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8c7404f63fa'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The original "initial" revision was generated empty because the
    # project bootstrapped its schema via db.create_all() at app start
    # and Alembic was bolted on later. That made `alembic upgrade head`
    # against an empty DB impossible: the chain assumed tables already
    # existed and only described diffs.
    #
    # Build the base schema from the SQLAlchemy MetaData using
    # checkfirst=True so this stays idempotent when the DB already has
    # tables (e.g. fresh installs that ran db.create_all() + stamp).
    # Subsequent migrations are made idempotent in their own files.
    bind = op.get_bind()
    from app import models  # noqa: F401  — loads every Model into MetaData
    from app.extensions import db

    db.metadata.create_all(bind=bind, checkfirst=True)


def downgrade() -> None:
    bind = op.get_bind()
    from app import models  # noqa: F401
    from app.extensions import db

    db.metadata.drop_all(bind=bind, checkfirst=True)
