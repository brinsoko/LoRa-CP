# app/utils/status.py
from __future__ import annotations

from app.extensions import db
from app.models import Checkin, Checkpoint, CheckpointGroup, TeamGroup


def get_active_group_for_team(team_id: int, competition_id: int) -> CheckpointGroup | None:
    """Return the currently-active CheckpointGroup for a team (if any)."""
    tg = (
        db.session.query(TeamGroup)
        .filter(
            TeamGroup.team_id == team_id,
            TeamGroup.active.is_(True),
        )
        .first()
    )
    if not tg or not tg.group:
        return None
    if tg.group.competition_id != competition_id:
        return None
    return tg.group


def get_group_checkpoints(group: CheckpointGroup) -> list[Checkpoint]:
    """Return the group's checkpoints in directed route order.

    Uses the path resolver, so a reversed group gets its map "next
    checkpoint" from the direction it actually travels.
    """
    from app.utils.paths import resolve_route_ids

    route = resolve_route_ids(group)
    if not route:
        return []
    by_id = {
        cp.id: cp
        for cp in db.session.query(Checkpoint).filter(Checkpoint.id.in_(set(route))).all()
    }
    return [by_id[cp_id] for cp_id in route if cp_id in by_id]


def get_found_checkpoint_ids(team_id: int, competition_id: int) -> list[int]:
    """Return checkpoint IDs that the team has already checked in at."""
    rows = (
        db.session.query(Checkin.checkpoint_id)
        .filter(Checkin.team_id == team_id, Checkin.competition_id == competition_id)
        .all()
    )
    return [cp_id for (cp_id,) in rows]


def compute_team_statuses(team_id: int, competition_id: int) -> dict:
    """
    Compute per-checkpoint status for the team's ACTIVE group.
    Statuses: 'found' | 'not_found' and pick one 'next' (first not found).
    Returns a structure usable by your map API and UI.
    """
    group = get_active_group_for_team(team_id, competition_id)
    if not group:
        return {
            "team_id": team_id,
            "group": None,
            "found_ids": [],
            "next_id": None,
            "checkpoints": [],
        }

    cps = get_group_checkpoints(group)
    # Exclude virtual checkpoints from map display
    cps = [cp for cp in cps if not cp.is_virtual]
    found_ids = set(get_found_checkpoint_ids(team_id, competition_id))

    # Decide "next" as the first checkpoint in group order that is not found.
    next_id = None
    items = []
    for order_index, cp in enumerate(cps):
        is_found = cp.id in found_ids
        if is_found:
            status = "found"
        else:
            if next_id is None:
                next_id = cp.id
                status = "next"
            else:
                status = "not_found"
        items.append(
            {
                "id": cp.id,
                "name": cp.name,
                "easting": cp.easting,
                "northing": cp.northing,
                "status": status,
                "order": order_index,
            }
        )

    return {
        "team_id": team_id,
        "group": {"id": group.id, "name": group.name},
        "found_ids": sorted(list(found_ids)),
        "next_id": next_id,
        "checkpoints": items,
    }


def all_checkpoints_for_map(competition_id: int) -> list[dict]:
    """Return all checkpoints with coords for the public map layer.

    Virtual checkpoints are excluded; they have no physical location.
    """
    cps = (
        db.session.query(Checkpoint)
        .filter(
            Checkpoint.competition_id == competition_id,
            Checkpoint.is_virtual.is_(False),
        )
        .order_by(Checkpoint.position.asc().nulls_last(), Checkpoint.name.asc())
        .all()
    )
    return [
        {
            "id": cp.id,
            "name": cp.name,
            "easting": cp.easting,
            "northing": cp.northing,
            "location": cp.location,
        }
        for cp in cps
    ]
