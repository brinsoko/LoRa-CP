# seed_admin.py (project root, next to run.py)
from app import create_app
from app.extensions import db
from app.models import User

app = create_app()
with app.app_context():
    if not User.query.filter_by(username="admin").first():
        u = User(username="admin", role="admin")
        u.set_password("change-me-now")
        db.session.add(u); db.session.commit()
        print("Admin created: admin / change-me-now")
    else:
        print("Admin already exists.")