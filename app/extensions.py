# app/extensions.py
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager

db = SQLAlchemy()           
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message_category = "warning"

@login_manager.user_loader
def load_user(user_id: str):
    # Local import prevents circular dependency at import time
    from app.models import User
    try:
        return User.query.get(int(user_id))
    except Exception:
        return None
    

    