#!/usr/bin/env python3
"""
Seed the database with demo/test data.

Usage (from project root):
  python scripts/seed_demo.py           # add/merge demo data
  python scripts/seed_demo.py --fresh   # DROP & CREATE tables, then seed

The script is idempotent where possible: it checks for existing rows by unique
fields (e.g., username, team name, checkpoint name, UID) before inserting.
"""

from __future__ import annotations

import os
import sys
import random
from datetime import datetime, timedelta

# Ensure project root (where 'app/' lives) is importable
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import create_app
from app.extensions import db
from app.models import (
    User,
    Team,
    RFIDCard,
    Checkpoint,
    Checkin,
    CheckpointGroup,
    TeamGroup,
)

# ----------------------------- helpers -----------------------------

def get_or_create_user(username: str, role: str, password: str) -> User:
    u = User.query.filter_by(username=username).first()
    if not u:
        u = User(username=username, role=role)
        u.set_password(password)
        db.session.add(u)
        db.session.flush()
    return u

def get_or_create_team(name: str, number: int | None) -> Team:
    q = Team.query.filter(db.func.lower(Team.name) == name.lower())
    if number is None:
        q = q.filter(Team.number.is_(None))
    else:
        q = q.filter(Team.number == number)
    t = q.first()
    if not t:
        t = Team(name=name, number=number)
        db.session.add(t)
        db.session.flush()
    return t

def get_or_create_checkpoint(name: str, e=None, n=None, location=None, desc=None) -> Checkpoint:
    cp = Checkpoint.query.filter(db.func.lower(Checkpoint.name) == name.lower()).first()
    if not cp:
        cp = Checkpoint(
            name=name,
            easting=e,
            northing=n,
            location=location,
            description=desc,
        )
        db.session.add(cp)
        db.session.flush()
    else:
        # lightly update missing coords/fields (non-destructive)
        if cp.easting is None and e is not None:
            cp.easting = e
        if cp.northing is None and n is not None:
            cp.northing = n
        if not cp.location and location:
            cp.location = location
        if not cp.description and desc:
            cp.description = desc
    return cp

def get_or_create_group(name: str, desc: str | None = None) -> CheckpointGroup:
    g = CheckpointGroup.query.filter(db.func.lower(CheckpointGroup.name) == name.lower()).first()
    if not g:
        g = CheckpointGroup(name=name, description=desc or None)
        db.session.add(g)
        db.session.flush()
    else:
        if desc and not g.description:
            g.description = desc
    return g

def assign_team_to_groups(team: Team, group_ids: list[int]):
    """Ensure a team has TeamGroup rows for the given groups (active=True),
    and remove TeamGroup rows for groups not in list."""
    current = {tg.group_id for tg in team.group_assignments}
    desired = set(group_ids)

    # Remove any not desired
    if current - desired:
        (db.session.query(TeamGroup)
         .filter(TeamGroup.team_id == team.id)
         .filter(~TeamGroup.group_id.in_(list(desired) if desired else [-1]))
         .delete(synchronize_session=False))

    # Add newly desired
    for gid in desired - current:
        db.session.add(TeamGroup(team_id=team.id, group_id=gid, active=True))

def ensure_rfid(team: Team, uid: str, number: int | None):
    uid_norm = uid.strip().upper()
    # If team already has a card, update it; else create (respecting UID uniqueness)
    existing_team_card = RFIDCard.query.filter_by(team_id=team.id).first()
    if existing_team_card:
        # if UID is used by another card, skip change to avoid IntegrityError
        used = RFIDCard.query.filter(RFIDCard.uid == uid_norm, RFIDCard.team_id != team.id).first()
        if not used:
            existing_team_card.uid = uid_norm
            existing_team_card.number = number
        return existing_team_card

    # Otherwise, ensure no one else has this UID
    if RFIDCard.query.filter_by(uid=uid_norm).first():
        # generate a unique-ish UID variant
        uid_norm += f"-{team.id}"
    card = RFIDCard(uid=uid_norm, team_id=team.id, number=number)
    db.session.add(card)
    db.session.flush()
    return card

def add_checkin(team: Team, cp: Checkpoint, when: datetime):
    # Respect unique check-in per (team, checkpoint) per your earlier rule
    exists = Checkin.query.filter_by(team_id=team.id, checkpoint_id=cp.id).first()
    if exists:
        return exists
    c = Checkin(team_id=team.id, checkpoint_id=cp.id, timestamp=when)
    db.session.add(c)
    return c

# ----------------------------- main seeding -----------------------------

def seed(fresh: bool = False):
    app = create_app()
    with app.app_context():
        if fresh:
            ans = input("⚠️  This will DROP & CREATE all tables. Continue? (y/N): ").strip().lower()
            if ans != "y":
                print("Cancelled.")
                return
            print("Dropping tables...")
            db.drop_all()
            print("Creating tables...")
            db.create_all()

        print("Seeding users...")
        admin = get_or_create_user("admin", "admin", "change-me-now")
        judge = get_or_create_user("judge", "judge", "judge-pass")

        print("Seeding groups...")
        g_alpha = get_or_create_group("Alpha", "Alpha route")
        g_bravo = get_or_create_group("Bravo", "Bravo route")
        g_charlie = get_or_create_group("Charlie", "Optional challenge")

        print("Seeding teams...")
        t1 = get_or_create_team("Wolves", 11)
        t2 = get_or_create_team("Eagles", 21)
        t3 = get_or_create_team("Foxes", 31)
        t4 = get_or_create_team("Badgers", 41)
        t5 = get_or_create_team("Otters", 51)
        t6 = get_or_create_team("Hawks", 61)

        print("Assigning teams to groups...")
        assign_team_to_groups(t1, [g_alpha.id])
        assign_team_to_groups(t2, [g_alpha.id])
        assign_team_to_groups(t3, [g_bravo.id])
        assign_team_to_groups(t4, [g_bravo.id])
        assign_team_to_groups(t5, [g_alpha.id, g_charlie.id])  # multi-group team
        assign_team_to_groups(t6, [g_bravo.id, g_charlie.id])

        print("Seeding checkpoints...")
        # Simple grid around a base point; replace with real coords later
        base_e, base_n = 10000.0, 5000.0
        cps = []
        for i in range(1, 11):
            name = f"CP-{i:02d}"
            e = base_e + (i * 10.0)
            n = base_n + (i * 7.0)
            cp = get_or_create_checkpoint(name, e=e, n=n, location=f"Sector {i}", desc=f"Scenic point {i}")
            cps.append(cp)

        # Group composition (many-to-many)
        # Alpha: CP-01..CP-05
        g_alpha.checkpoints = [c for c in cps if 1 <= int(c.name.split("-")[1]) <= 5]
        # Bravo: CP-06..CP-10
        g_bravo.checkpoints = [c for c in cps if 6 <= int(c.name.split("-")[1]) <= 10]
        # Charlie overlaps a few special points
        g_charlie.checkpoints = [cps[1], cps[4], cps[7]]  # CP-02, CP-05, CP-08

        print("Seeding RFID cards...")
        ensure_rfid(t1, "A1B2C3D4", 101)
        ensure_rfid(t2, "A1B2C3D5", 102)
        ensure_rfid(t3, "A1B2C3D6", 103)
        ensure_rfid(t4, "A1B2C3D7", 104)
        ensure_rfid(t5, "A1B2C3D8", 105)
        ensure_rfid(t6, "A1B2C3D9", 106)

        print("Seeding check-ins...")
        now = datetime.utcnow()
        # Helper: only pick checkpoints that belong to any of a team's groups
        def checkpoints_for_team(team: Team) -> list[Checkpoint]:
            group_ids = [tg.group_id for tg in team.group_assignments]
            if not group_ids:
                return []
            # Union of checkpoints in those groups
            q = (Checkpoint.query
                 .join(Checkpoint.groups)
                 .filter(CheckpointGroup.id.in_(group_ids))
                 .distinct()
                 .order_by(Checkpoint.name.asc()))
            return q.all()

        for t in [t1, t2, t3, t4, t5, t6]:
            pool = checkpoints_for_team(t)
            if not pool:
                continue
            sample = random.sample(pool, k=min(3, len(pool)))
            # Spread timestamps over last 24h
            for k, cp in enumerate(sample):
                ts = now - timedelta(hours=(24 - (k * 3 + random.randint(0, 2))))
                add_checkin(t, cp, ts)

        db.session.commit()
        print("\n✅ Seed complete!")
        print_counts()

def print_counts():
    print("Counts:")
    print(f"  Users:       {User.query.count()}")
    print(f"  Teams:       {Team.query.count()}")
    print(f"  Groups:      {CheckpointGroup.query.count()}")
    print(f"  Checkpoints: {Checkpoint.query.count()}")
    print(f"  RFID Cards:  {RFIDCard.query.count()}")
    print(f"  Check-ins:   {Checkin.query.count()}")

# ----------------------------- entrypoint -----------------------------

if __name__ == "__main__":
    import argparse
    p = argparse.ArgumentParser(description="Seed demo data.")
    p.add_argument("--fresh", action="store_true", help="Drop & recreate tables before seeding.")
    args = p.parse_args()
    seed(fresh=args.fresh)