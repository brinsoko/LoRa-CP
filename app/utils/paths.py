# app/utils/paths.py
"""Route resolution: the single authority for directed checkpoint order.

A CheckpointGroup (category) references a Path plus a direction. Every
consumer that needs "the checkpoints of this group, in traversal order"
(live arrivals, scoring, sheets, stats, ingest) must go through these
helpers so start/finish and direction can never diverge between
subsystems, which was the core defect of the old reverse-flag model.
"""

from __future__ import annotations

from app.extensions import db
from app.models import CheckpointGroup, Path, PathStop

DIRECTION_FORWARD = "forward"
DIRECTION_REVERSE = "reverse"


def path_checkpoint_ids(path: Path | None) -> list[int]:
    """Checkpoint ids of a path in stored (forward) stop order."""
    if not path:
        return []
    return [stop.checkpoint_id for stop in path.stops]


def resolve_route_ids(group: CheckpointGroup | None) -> list[int]:
    """Directed checkpoint ids for a group (direction applied)."""
    if not group:
        return []
    ids = path_checkpoint_ids(group.path)
    if group.direction == DIRECTION_REVERSE:
        ids = list(reversed(ids))
    return ids


def route_start(group: CheckpointGroup | None) -> int | None:
    ids = resolve_route_ids(group)
    return ids[0] if ids else None


def route_finish(group: CheckpointGroup | None) -> int | None:
    ids = resolve_route_ids(group)
    return ids[-1] if ids else None


def resolve_route_ids_bulk(comp_id: int) -> dict[int, list[int]]:
    """Directed checkpoint ids for every group of a competition.

    One query for the stops, one for the groups, for the read paths that
    resolve all routes at once (live arrivals, leaderboard build, stats).
    """
    stops = (
        db.session.query(PathStop.path_id, PathStop.checkpoint_id)
        .join(Path, PathStop.path_id == Path.id)
        .filter(Path.competition_id == comp_id)
        .order_by(PathStop.path_id.asc(), PathStop.position.asc())
        .all()
    )
    order_by_path: dict[int, list[int]] = {}
    for path_id, checkpoint_id in stops:
        order_by_path.setdefault(path_id, []).append(checkpoint_id)

    groups = (
        db.session.query(CheckpointGroup.id, CheckpointGroup.path_id, CheckpointGroup.direction)
        .filter(CheckpointGroup.competition_id == comp_id)
        .all()
    )
    routes: dict[int, list[int]] = {}
    for group_id, path_id, direction in groups:
        ids = list(order_by_path.get(path_id, [])) if path_id else []
        if direction == DIRECTION_REVERSE:
            ids.reverse()
        routes[group_id] = ids
    return routes


def group_ids_containing_checkpoint(comp_id: int, checkpoint_id: int) -> set[int]:
    """Groups whose route includes the checkpoint (direction irrelevant)."""
    rows = (
        db.session.query(CheckpointGroup.id)
        .join(Path, CheckpointGroup.path_id == Path.id)
        .join(PathStop, PathStop.path_id == Path.id)
        .filter(
            CheckpointGroup.competition_id == comp_id,
            PathStop.checkpoint_id == checkpoint_id,
        )
        .distinct()
        .all()
    )
    return {row[0] for row in rows}


def replace_path_stops(
    path: Path,
    ordered_checkpoint_ids: list[int],
    expected_minutes: list[float | None] | None = None,
) -> None:
    """Rewrite a path's stops to the given checkpoint order.

    Positions are reassigned densely from 0. expected_minutes, when given,
    is aligned with the checkpoint list (ETA fallback per leg); otherwise
    existing values are kept for positions whose checkpoint did not
    change, so a pure reorder of later stops doesn't wipe leg estimates.
    """
    previous = {stop.position: stop for stop in path.stops}
    path.stops = []
    db.session.flush()
    for position, checkpoint_id in enumerate(ordered_checkpoint_ids):
        if expected_minutes is not None:
            minutes = expected_minutes[position] if position < len(expected_minutes) else None
        else:
            old = previous.get(position)
            minutes = old.expected_leg_minutes if (old and old.checkpoint_id == checkpoint_id) else None
        path.stops.append(
            PathStop(
                checkpoint_id=checkpoint_id,
                position=position,
                expected_leg_minutes=minutes,
            )
        )
