# app/extensions.py
from flask_babel import Babel
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address
from flask_login import LoginManager
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine

db = SQLAlchemy()
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message_category = "warning"

babel = Babel()

# In-memory storage is fine for a single-process deployment; resets on
# restart, which is acceptable for the small operator footprint here.
# If we ever scale beyond one worker, swap storage_uri to redis://...
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[],
    storage_uri="memory://",
)


@login_manager.user_loader
def load_user(user_id: str):
    # Local import prevents circular dependency at import time
    from app.models import User

    try:
        return db.session.get(User, int(user_id))
    except Exception:
        return None


@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = getattr(dbapi_connection, "cursor", None)
    module_name = type(dbapi_connection).__module__
    if cursor is None or "sqlite" not in module_name:
        return
    cur = dbapi_connection.cursor()
    try:
        cur.execute("PRAGMA foreign_keys=ON")
        # WAL lets the two gunicorn workers read while one writes;
        # busy_timeout makes the losing writer wait for the lock instead
        # of raising "database is locked". In-memory test DBs report
        # journal_mode=memory here, which is harmless.
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA busy_timeout=15000")
    finally:
        cur.close()
