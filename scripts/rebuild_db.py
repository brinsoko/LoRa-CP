#!/usr/bin/env python3
"""
Rebuild the local SQLite DB: drop all tables and recreate them.
Run from project root:
    python scripts/rebuild_db.py
"""
import os, sys

# Ensure project root (where 'app/' lives) is on sys.path
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import create_app
from app.extensions import db

def main():
    app = create_app()
    with app.app_context():
        confirm = input("⚠️  This will ERASE all data. Continue? (y/N): ").strip().lower()
        if confirm != "y":
            print("Cancelled.")
            return
        print("Dropping all tables...")
        db.drop_all()
        print("Creating tables...")
        db.create_all()
        print("✅ Database rebuilt successfully.")

if __name__ == "__main__":
    main()