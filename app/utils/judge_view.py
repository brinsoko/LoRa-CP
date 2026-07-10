# app/utils/judge_view.py
"""Data for the judge shell's checkpoint-scoped screens.

The waiting list implements the decisions-log rules (redesign plan 3.5):
a team drops off as soon as it arrived here, checked in at any LATER stop
of its directed route (surfaced separately as "missed you"), is DNF, or
has finished. The ETA uses the mean of observed leg durations once at
least MIN_ETA_SAMPLES teams completed the leg, before that the manual
PathStop.expected_leg_minutes, otherwise only "last seen" is shown.
"""

from __future__ import annotations

from app.extensions import db
from app.models import (
    Checkin,
    Checkpoint,
    CheckpointGroup,
    ScoreEntry,
    Team,
    TeamGroup,
)
from app.utils.paths import resolve_route_ids
from app.utils.time import format_datetime_display, format_time_display, utcnow_naive

MIN_ETA_SAMPLES = 3


def _expected_leg_minutes(group: CheckpointGroup, prev_cp_id: int, cp_id: int) -> float | None:
    """Manual expected duration for the leg between two adjacent stops.

    Stored undirected on the later (higher forward position) stop of the
    pair, so both traversal directions read the same value.
    """
    if not group.path:
        return None
    stops = list(group.path.stops)
    for earlier, later in zip(stops, stops[1:], strict=False):
        if {earlier.checkpoint_id, later.checkpoint_id} == {prev_cp_id, cp_id}:
            return later.expected_leg_minutes
    return None


def build_judge_checkpoint_view(comp_id: int, checkpoint_id: int) -> dict:
    """Arrived / waiting / missed buckets plus counts for one checkpoint."""
    now = utcnow_naive()
    checkpoint = db.session.get(Checkpoint, checkpoint_id)

    groups = CheckpointGroup.query.filter(CheckpointGroup.competition_id == comp_id).all()
    group_routes: dict[int, list[int]] = {}
    cp_index_by_group: dict[int, int] = {}
    # All positions of this checkpoint on each route: a path may visit it
    # twice (butterfly loop), and route.index() alone would always resolve
    # to the first visit, misclassifying a team heading to a later visit.
    cp_occurrences_by_group: dict[int, list[int]] = {}
    for group in groups:
        route = resolve_route_ids(group)
        if checkpoint_id in route:
            group_routes[group.id] = route
            cp_index_by_group[group.id] = route.index(checkpoint_id)
            cp_occurrences_by_group[group.id] = [i for i, cid in enumerate(route) if cid == checkpoint_id]
    group_by_id = {g.id: g for g in groups}

    assignments = (
        TeamGroup.query.join(Team, TeamGroup.team_id == Team.id)
        .filter(Team.competition_id == comp_id, TeamGroup.active.is_(True))
        .all()
    )
    team_group: dict[int, int] = {tg.team_id: tg.group_id for tg in assignments}
    expected_team_ids = [tid for tid, gid in team_group.items() if gid in group_routes]

    teams = {t.id: t for t in Team.query.filter(Team.competition_id == comp_id).all()}

    # First check-in per (team, checkpoint) for every routed checkpoint.
    # Always include this checkpoint even when no group's route passes
    # through it (e.g. a checkpoint not placed on any path), otherwise
    # real arrivals here would never load and the "whoever is physically
    # standing there" arrived list would be empty.
    route_cp_ids = {cid for route in group_routes.values() for cid in route}
    route_cp_ids.add(checkpoint_id)
    team_cp_times: dict[int, dict[int, object]] = {}
    if route_cp_ids:
        checkins = (
            Checkin.query.filter(
                Checkin.competition_id == comp_id,
                Checkin.checkpoint_id.in_(route_cp_ids),
            )
            .order_by(Checkin.timestamp.asc())
            .all()
        )
        for checkin in checkins:
            team_cp_times.setdefault(checkin.team_id, {}).setdefault(
                checkin.checkpoint_id, checkin.timestamp
            )

    # Latest score entry per team at this checkpoint (scored/unscored badge).
    latest_entry: dict[int, ScoreEntry] = {}
    for entry in (
        ScoreEntry.query.filter(
            ScoreEntry.competition_id == comp_id,
            ScoreEntry.checkpoint_id == checkpoint_id,
        )
        .order_by(ScoreEntry.created_at.desc())
        .all()
    ):
        latest_entry.setdefault(entry.team_id, entry)

    # Observed leg samples per group: durations prev_stop -> this stop.
    leg_stats: dict[int, tuple[int, float | None]] = {}  # group_id -> (samples, mean)
    for gid, route in group_routes.items():
        idx = cp_index_by_group[gid]
        if idx == 0:
            leg_stats[gid] = (0, None)
            continue
        prev_id = route[idx - 1]
        durations = []
        for tid, gid2 in team_group.items():
            if gid2 != gid:
                continue
            times = team_cp_times.get(tid, {})
            start_ts = times.get(prev_id)
            end_ts = times.get(checkpoint_id)
            if start_ts and end_ts and end_ts >= start_ts:
                durations.append((end_ts - start_ts).total_seconds() / 60.0)
        mean = (sum(durations) / len(durations)) if durations else None
        leg_stats[gid] = (len(durations), mean)

    arrived = []
    waiting = []
    missed = []
    dnf_count = 0
    finished_count = 0

    # Arrivals include unexpected teams (wrong course, no group): the judge
    # sees whoever is physically standing there.
    arrived_ids = {
        tid for tid, times in team_cp_times.items() if checkpoint_id in times
    }
    for tid in arrived_ids:
        team = teams.get(tid)
        if not team:
            continue
        entry = latest_entry.get(tid)
        gid = team_group.get(tid)
        arrived.append(
            {
                "team": team,
                "group_name": group_by_id[gid].name if gid in group_by_id else "",
                "arrived_at": team_cp_times[tid][checkpoint_id],
                "arrived_hms": format_time_display(team_cp_times[tid][checkpoint_id]),
                "scored": entry is not None,
                "total": entry.total if entry else None,
                "expected": gid in group_routes,
            }
        )
    arrived.sort(key=lambda row: row["arrived_at"], reverse=True)

    for tid in expected_team_ids:
        if tid in arrived_ids:
            continue
        team = teams.get(tid)
        if not team:
            continue
        gid = team_group[tid]
        group = group_by_id[gid]
        route = group_routes[gid]
        times = team_cp_times.get(tid, {})

        if team.dnf:
            dnf_count += 1
            continue
        # Resolve which visit of this checkpoint the team is heading to.
        # On a butterfly route the target is the first occurrence still
        # ahead of the team's furthest recorded stop; if they are already
        # past every occurrence, use the last one so the "missed" check
        # below fires. On a normal (single-visit) route this is just the
        # sole index, so behaviour is unchanged.
        occurrences = cp_occurrences_by_group[gid]
        progressed = [i for i, cid in enumerate(route) if cid in times]
        furthest = max(progressed) if progressed else -1
        idx = next((i for i in occurrences if i > furthest), occurrences[-1])
        # A check-in past the LAST occurrence means they passed without
        # ever visiting (includes the finish, the strongest stop-waiting
        # signal). Using the last occurrence, not idx, so a team between
        # the first and second visit is still "waiting", not "missed".
        last_idx = occurrences[-1]
        later_hit = next((cid for cid in route[last_idx + 1 :] if cid in times), None)
        if later_hit is not None:
            if route and route[-1] in times:
                finished_count += 1
            missed.append({"team": team, "group_name": group.name})
            continue

        prev_id = route[idx - 1] if idx > 0 else None
        prev_ts = times.get(prev_id) if prev_id else None
        seen_stops = [cid for cid in route[:idx] if cid in times]
        last_seen_id = seen_stops[-1] if seen_stops else None
        last_seen_ts = times.get(last_seen_id) if last_seen_id else None

        eta_state = "not_started"
        eta_minutes = None
        if prev_ts is not None:
            samples, mean = leg_stats.get(gid, (0, None))
            estimate = mean if samples >= MIN_ETA_SAMPLES else None
            if estimate is None:
                estimate = _expected_leg_minutes(group, prev_id, checkpoint_id)
            elapsed = (now - prev_ts).total_seconds() / 60.0
            if estimate is not None:
                eta_minutes = estimate - elapsed
                eta_state = "eta" if eta_minutes >= 0 else "overdue"
            else:
                eta_state = "on_course"
        elif last_seen_ts is not None:
            eta_state = "on_course"

        last_seen_cp = db.session.get(Checkpoint, last_seen_id) if last_seen_id else None
        waiting.append(
            {
                "team": team,
                "group_name": group.name,
                "last_seen_name": last_seen_cp.name if last_seen_cp else "",
                "last_seen_at": last_seen_ts,
                "last_seen_label": format_datetime_display(last_seen_ts) if last_seen_ts else "",
                "last_seen_minutes_ago": (
                    (now - last_seen_ts).total_seconds() / 60.0 if last_seen_ts else None
                ),
                "eta_state": eta_state,
                "eta_minutes": eta_minutes,
            }
        )

    # Overdue first (most urgent for the judge), then nearest ETA, then
    # on-course, then not started. Within a state, smaller |eta| first.
    state_order = {"overdue": 0, "eta": 1, "on_course": 2, "not_started": 3}
    waiting.sort(
        key=lambda row: (
            state_order.get(row["eta_state"], 9),
            abs(row["eta_minutes"]) if row["eta_minutes"] is not None else 1e9,
            row["team"].number or 1e9,
        )
    )

    expected_total = len(expected_team_ids)
    arrived_expected = sum(1 for row in arrived if row["expected"])
    return {
        "checkpoint": checkpoint,
        "arrived": arrived,
        "waiting": waiting,
        "missed": missed,
        "dnf_count": dnf_count,
        "finished_count": finished_count,
        "expected_total": expected_total,
        "arrived_count": arrived_expected,
        "waiting_count": len(waiting),
    }
