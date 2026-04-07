# app/extensions.py
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_babel import Babel
from sqlalchemy import event
from sqlalchemy.engine import Engine

db = SQLAlchemy()           
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message_category = "warning"

babel = Babel()

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
    finally:
        cur.close()
