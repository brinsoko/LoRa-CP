#!/usr/bin/env python3
"""
Create a self-contained world for real-hardware testing.

Lays down:
  * a competition (created if missing, by name)
  * named checkpoints, one per LoRa device id you'll scan with
    (so the ingest auto-create doesn't leave you with "Device 2" /
    "Device 3" placeholder names)
  * one checkpoint group containing all those checkpoints
  * a handful of teams, all assigned to that group

Cards/UIDs are NOT created here - register them as you scan, either
via the team edit page in the web UI or whichever workflow you use.

Idempotent: re-running with the same args won't create duplicates.

Usage (from project root):
  venv/bin/python scripts/seed_test_teams.py
  venv/bin/python scripts/seed_test_teams.py --name "Spring test"
  venv/bin/python scripts/seed_test_teams.py --dev-ids 2,3,4 --teams 6

In production, run inside the web container so it hits the real DB:
  docker compose exec web python scripts/seed_test_teams.py
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from app import create_app
from app.extensions import db
from app.models import (
    Checkpoint,
    CheckpointGroup,
    Competition,
    LoRaDevice,
    Path,
    PathStop,
)
from scripts.seed_db import (
    assign_team_to_groups,
    get_or_create_checkpoint,
    get_or_create_group,
    get_or_create_team,
)

DEFAULT_COMPETITION = "Test event"
DEFAULT_GROUP = "Test route"
DEFAULT_DEV_IDS = (2, 3)
DEFAULT_TEAMS = [
    ("Wolves", 301),
    ("Eagles", 302),
    ("Foxes", 303),
    ("Hawks", 304),
    ("Badgers", 401),
    ("Otters", 402),
    ("Ravens", 403),
    ("Lynxes", 404),
]
# Friendly checkpoint name per dev id. Anything past this falls back to
# a generic "Device N" so the script never fails on a new dev id.
CHECKPOINT_NAMES = {
    1: "Start line",
    2: "Mid checkpoint",
    3: "Finish line",
    4: "Bonus checkpoint",
}


def parse_dev_ids(raw: str) -> list[int]:
    out: list[int] = []
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        out.append(int(chunk))  # argparse will surface a ValueError if bad
    if not out:
        raise ValueError("--dev-ids must contain at least one integer")
    if any(d <= 0 for d in out):
        raise ValueError("--dev-ids must all be positive")
    return out


def get_or_create_competition(name: str) -> Competition:
    comp = Competition.query.filter(db.func.lower(Competition.name) == name.lower()).first()
    if comp:
        return comp
    comp = Competition(name=name)
    db.session.add(comp)
    db.session.flush()
    return comp


def link_device_to_checkpoint(competition_id: int, dev_id: int, checkpoint: Checkpoint) -> LoRaDevice:
    """Idempotently bind a LoRaDevice (dev_num) to a real checkpoint so the
    ingest endpoint resolves to that checkpoint instead of auto-creating a
    'Device N' one."""
    device = LoRaDevice.query.filter_by(competition_id=competition_id, dev_num=dev_id).first()
    if not device:
        device = LoRaDevice(
            competition_id=competition_id,
            dev_num=dev_id,
            name=f"DEV-{dev_id}",
            active=True,
        )
        db.session.add(device)
        db.session.flush()
    # Wire the FK both directions: the checkpoint references the device,
    # and the resolver in app.resources.ingest.resolve_checkpoint_for_dev
    # walks via Checkpoint.lora_device_id.
    if checkpoint.lora_device_id != device.id:
        checkpoint.lora_device_id = device.id
    return device


def set_group_checkpoints_idempotent(group: CheckpointGroup, checkpoints: list[Checkpoint]) -> None:
    """Same intent as seed_db.set_group_checkpoints but safe to re-run:
    only appends missing stops to the group's path, preserves existing
    order, and never wipes unrelated checkpoints from the route."""
    path = group.path
    if path is None:
        path = Path(competition_id=group.competition_id, name=f"{group.name} path")
        db.session.add(path)
        db.session.flush()
        group.path = path
        group.direction = "forward"
    existing_ids = {stop.checkpoint_id for stop in path.stops}
    next_pos = max((stop.position for stop in path.stops), default=-1) + 1
    for cp in checkpoints:
        if cp.id in existing_ids:
            continue
        db.session.add(PathStop(path_id=path.id, checkpoint_id=cp.id, position=next_pos))
        next_pos += 1


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--name", default=DEFAULT_COMPETITION, help=f"Competition name (default: {DEFAULT_COMPETITION!r}).")
    p.add_argument("--group", default=DEFAULT_GROUP, help=f"Checkpoint group name (default: {DEFAULT_GROUP!r}).")
    p.add_argument(
        "--dev-ids",
        type=parse_dev_ids,
        default=list(DEFAULT_DEV_IDS),
        help=(
            "Comma-separated LoRa device ids to wire to named checkpoints "
            f"(default: {','.join(map(str, DEFAULT_DEV_IDS))})."
        ),
    )
    p.add_argument(
        "--teams",
        type=int,
        default=4,
        help=f"How many teams to create (max {len(DEFAULT_TEAMS)}, default 4).",
    )
    args = p.parse_args()

    if not 1 <= args.teams <= len(DEFAULT_TEAMS):
        sys.exit(f"--teams must be between 1 and {len(DEFAULT_TEAMS)}")

    app = create_app()
    with app.app_context():
        competition = get_or_create_competition(args.name)
        print(f"competition: id={competition.id} name={competition.name!r}")

        # One named checkpoint per dev id, each wired to its LoRaDevice.
        checkpoints: list[Checkpoint] = []
        for dev_id in args.dev_ids:
            cp_name = CHECKPOINT_NAMES.get(dev_id, f"Device {dev_id}")
            cp = get_or_create_checkpoint(competition.id, cp_name, desc=f"Linked to LoRa dev_id={dev_id}")
            link_device_to_checkpoint(competition.id, dev_id, cp)
            checkpoints.append(cp)
            print(f"  checkpoint: dev_id={dev_id} -> {cp.name!r} (id={cp.id})")

        # Group + group-checkpoint links so the leaderboard has something to show.
        group = get_or_create_group(competition.id, args.group, desc="Auto-created for testing")
        set_group_checkpoints_idempotent(group, checkpoints)
        print(f"group: id={group.id} name={group.name!r} contains {len(checkpoints)} checkpoint(s)")

        # Teams, all assigned to the test group so their check-ins count.
        for name, number in DEFAULT_TEAMS[: args.teams]:
            team = get_or_create_team(competition.id, name, number)
            assign_team_to_groups(team, [group.id])
            print(f"  team: {name!r} #{number} -> id={team.id} (group={group.name!r})")

        db.session.commit()
        # Capture before the session closes - bare ORM attribute access on
        # `competition` after the `with` block detaches the instance.
        comp_id = competition.id

    print()
    print("Done. Next steps for live hardware testing:")
    print(f"  1. POST to /api/ingest with competition_id={comp_id} (or set COMPETITION_ID in the reader).")
    print("  2. Scan a card - the message logs and `uid_seen` will be False the first time.")
    print("  3. Open the team's edit page, paste the UID from the response into the RFID UID field, save.")
    print("  4. Scan again - `checkin_created: True` and the team shows up on the checkpoint.")


if __name__ == "__main__":
    main()
