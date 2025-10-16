# app/utils/status.py
from app.models import Checkin, GroupCheckpoint, TeamGroup

def compute_team_statuses(team_id: int):
    """
    Returns (ordered_checkpoints, status_by_checkpoint_id) for the team's active groups.
    Status: 'found', 'next', 'not_found'
    """
    # active groups for team (you can allow multiple; we’ll union them by order)
    active_groups = TeamGroup.query.filter_by(team_id=team_id, active=True).all()
    if not active_groups:
        return [], {}

    group_ids = [tg.group_id for tg in active_groups]

    # ordered checkpoints across groups (group_id then seq_index)
    rows = (GroupCheckpoint.query
            .filter(GroupCheckpoint.group_id.in_(group_ids))
            .order_by(GroupCheckpoint.group_id.asc(),
                      GroupCheckpoint.seq_index.asc())
            .all())

    ordered = [r.checkpoint for r in rows]

    # found set via Checkin
    found_ids = {
        cid for (cid,) in Checkin.query.with_entities(Checkin.checkpoint_id)
        .filter(Checkin.team_id == team_id).all()
    }

    status = {}
    # mark found
    for cp in ordered:
        if cp.id in found_ids:
            status[cp.id] = "found"

    # mark first not-found as next (per group independently)
    # We need the first not-found in EACH group:
    by_group = {}
    for r in rows:
        by_group.setdefault(r.group_id, []).append(r.checkpoint)

    for gid, cps in by_group.items():
        for cp in cps:
            if cp.id not in status:
                status[cp.id] = "next"
                break

    # remaining become not_found
    for cp in ordered:
        if cp.id not in status:
            status[cp.id] = "not_found"

    return ordered, status