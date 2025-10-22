from app import create_app, db
from app.models import User
from werkzeug.security import generate_password_hash

app = create_app()
with app.app_context():
    if not User.query.filter_by(username="admin").first():
        admin = User(
            username="admin",
            password_hash=generate_password_hash("admin123"),
            is_admin=True
        )
        db.session.add(admin)
        db.session.commit()
        print("✅ Admin user created.")
    else:
        print("⚠️  Admin already exists.")