import os
import sys
from logging.config import fileConfig

from sqlalchemy import engine_from_config, pool

from alembic import context

# Ensure the project root is on sys.path so we can import app modules
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app import create_app
from app.extensions import db
from app.models import *  # noqa: F401,F403 — ensure all models are loaded

# Alembic Config object
config = context.config

# Set up Python logging from the .ini file
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Create the Flask app *without* the boot-time DDL so Alembic can
# apply migrations against a real empty DB. Otherwise create_all() runs
# first and the migrations try to ADD columns that already exist, which
# breaks `alembic upgrade head` on fresh installs.
flask_app = create_app({"SKIP_DB_BOOTSTRAP": True})

# Override sqlalchemy.url from the Flask app config
config.set_main_option(
    "sqlalchemy.url",
    flask_app.config["SQLALCHEMY_DATABASE_URI"],
)

# Point Alembic at the same MetaData that SQLAlchemy uses
target_metadata = db.Model.metadata


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL without a live connection)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,  # required for SQLite ALTER TABLE support
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode (with a live DB connection)."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # required for SQLite ALTER TABLE support
        )

        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
