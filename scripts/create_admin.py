#!/usr/bin/env python3
from __future__ import annotations
import os, sys
from app import create_app
from app.extensions import db
from app.models import User

sys.path.append(os.path.dirname(os.path.dirname(__file__)))

def main() -> None:
    username = os.environ.get("ADMIN_USER", "admin").strip()
    password = os.environ.get("ADMIN_PASS", "admin123")
    role     = (os.environ.get("ADMIN_ROLE", "admin") or "admin").strip()

    if role not in ("public", "judge", "admin", "superadmin"):
        raise SystemExit(f"Invalid ADMIN_ROLE={role!r}; must be one of public|judge|admin|superadmin")

    app = create_app()
    with app.app_context():
        u = User.query.filter_by(username=username).first()
        if u:
            changed = False
            # update role if different
            if u.role != role:
                u.role = role
                changed = True
            # update password only if explicitly provided (non-empty env)
            if os.environ.get("ADMIN_PASS") is not None and password:
                u.set_password(password)
                changed = True

            if changed:
                db.session.commit()
                print(f"User '{username}' updated (role={u.role}).")
            else:
                print(f"User '{username}' already exists (role={u.role}); no changes.")
        else:
            u = User(username=username, role=role)
            u.set_password(password)
            db.session.add(u)
            db.session.commit()
            print(f"User '{username}' created with role '{role}'. Password: {password}")

if __name__ == "__main__":
    main()
