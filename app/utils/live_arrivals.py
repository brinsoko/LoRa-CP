from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import joinedload

from app.models import (
    Checkin,
    Checkpoint,
    CheckpointGroup,
    CheckpointGroupLink,
    GlobalScoreRule,
    Team,
    TeamGroup,
)
from app.utils.time import format_datetime_display, utcnow_naive

VALID_TEAM_SORTS = {"number_asc", "number_desc", "name_asc", "name_desc", "status", "latest"}


def _iso_dt(value: datetime | None) -> str | None:
    if not value:
        return None
    return value.replace(microsecond=0).isoformat()


def _display_dt(value: datetime | None) -> str:
    return format_datetime_display(value)


def _safe_int(value) -> int | None:
    try:
        number = int(value)
    except Exception:
        return None
    return number if number > 0 else None


def _minutes_between(start: datetime | None, end: datetime | None) -> float | None:
    if not start or not end or end < start:
        return None
    return (end - start).total_seconds() / 60.0


def _minutes_label(minutes: float | None) -> str:
    if minutes is None:
        return ""
    if minutes < 60:
        return f"{round(minutes):.0f} min"
    hours = int(minutes // 60)
    remainder = int(round(minutes % 60))
    return f"{hours} h {remainder:02d} min"


def _build_group_routes(comp_id: int) -> tuple[dict[int, list[int]], dict[int, int], dict[int, int]]:
    links = (
        CheckpointGroupLink.query.join(CheckpointGroup, CheckpointGroupLink.group_id == CheckpointGroup.id)
        .filter(CheckpointGroup.competition_id == comp_id)
        .order_by(
            CheckpointGroupLink.group_id.asc(),
            CheckpointGroupLink.position.asc().nulls_last(),
            CheckpointGroupLink.checkpoint_id.asc(),
        )
        .all()
    )
    group_checkpoint_order: dict[int, list[int]] = {}
    for link in links:
        group_checkpoint_order.setdefault(link.group_id, []).append(link.checkpoint_id)

    group_start: dict[int, int] = {}
    group_finish: dict[int, int] = {}
    for group_id, checkpoint_ids in group_checkpoint_order.items():
        if checkpoint_ids:
            group_start[group_id] = checkpoint_ids[0]
            group_finish[group_id] = checkpoint_ids[-1]

    rules = GlobalScoreRule.query.filter(GlobalScoreRule.competition_id == comp_id).all()
    for rule in rules:
        time_rule = (rule.rules or {}).get("time") or {}
        start_id = _safe_int(time_rule.get("start_checkpoint_id"))
        finish_id = _safe_int(time_rule.get("end_checkpoint_id"))
        if start_id:
            group_start[rule.group_id] = start_id
        if finish_id:
            group_finish[rule.group_id] = finish_id

    return group_checkpoint_order, group_start, group_finish


def _sort_team_rows(rows: list[dict], sort: str) -> list[dict]:
    sort = sort if sort in VALID_TEAM_SORTS else "number_asc"
    if sort == "name_desc":
        return sorted(rows, key=lambda row: ((row.get("name") or "").lower(), row.get("number") or 0), reverse=True)
    if sort == "name_asc":
        return sorted(rows, key=lambda row: ((row.get("name") or "").lower(), row.get("number") or 0))
    if sort == "number_desc":
        return sorted(
            rows,
            key=lambda row: (
                row.get("number") is None,
                -(row.get("number") or 0),
                (row.get("name") or "").lower(),
            ),
        )
    if sort == "status":
        status_order = {"on_course": 0, "started": 1, "not_started": 2, "dnf": 3, "finished": 4, "not_routed": 5}
        return sorted(
            rows,
            key=lambda row: (
                status_order.get(row.get("status"), 99),
                row.get("number") is None,
                row.get("number") or 0,
                (row.get("name") or "").lower(),
            ),
        )
    if sort == "latest":
        return sorted(
            rows,
            key=lambda row: (
                row.get("_latest_sort") or datetime.min,
                row.get("number") is None,
                row.get("number") or 0,
            ),
            reverse=True,
        )
    return sorted(
        rows,
        key=lambda row: (
            row.get("number") is None,
            row.get("number") or 0,
            (row.get("name") or "").lower(),
        ),
    )


def build_live_arrivals(comp_id: int, group_id: int | None = None, sort: str = "number_asc") -> dict:
    now = utcnow_naive()
    groups = (
        CheckpointGroup.query.filter(CheckpointGroup.competition_id == comp_id)
        .order_by(CheckpointGroup.position.asc().nulls_last(), CheckpointGroup.name.asc())
        .all()
    )
    group_by_id = {group.id: group for group in groups}
    selected_group_id = group_id if group_id in group_by_id else None

    teams_query = Team.query.filter(Team.competition_id == comp_id)
    if selected_group_id:
        teams_query = teams_query.join(TeamGroup, TeamGroup.team_id == Team.id).filter(
            TeamGroup.group_id == selected_group_id, TeamGroup.active.is_(True)
        )
    teams = teams_query.order_by(Team.number.asc().nulls_last(), Team.name.asc()).all()

    team_ids = [team.id for team in teams]
    all_team_ids = set(team_ids)
    group_names = {group.id: group.name for group in groups}
    group_order = {group.id: idx for idx, group in enumerate(groups)}
    group_checkpoint_order, group_start, group_finish = _build_group_routes(comp_id)

    if selected_group_id:
        checkpoint_ids_for_view = group_checkpoint_order.get(selected_group_id, [])
        checkpoint_by_id = {}
        if checkpoint_ids_for_view:
            checkpoints_for_group = Checkpoint.query.filter(
                Checkpoint.competition_id == comp_id,
                Checkpoint.is_virtual.is_(False),
                Checkpoint.id.in_(checkpoint_ids_for_view),
            ).all()
            checkpoint_by_id = {checkpoint.id: checkpoint for checkpoint in checkpoints_for_group}
        checkpoints = [
            checkpoint_by_id[checkpoint_id]
            for checkpoint_id in checkpoint_ids_for_view
            if checkpoint_id in checkpoint_by_id
        ]
    else:
        checkpoints = (
            Checkpoint.query.filter(Checkpoint.competition_id == comp_id, Checkpoint.is_virtual.is_(False))
            .order_by(Checkpoint.name.asc())
            .all()
        )

    checkpoint_ids_for_view = {checkpoint.id for checkpoint in checkpoints}

    team_group_ids: dict[int, list[int]] = {}
    group_team_ids: dict[int, set[int]] = {}
    active_assignments = (
        TeamGroup.query.join(Team, TeamGroup.team_id == Team.id)
        .filter(Team.competition_id == comp_id, TeamGroup.active.is_(True))
        .all()
    )
    for assignment in active_assignments:
        team_group_ids.setdefault(assignment.team_id, []).append(assignment.group_id)
        group_team_ids.setdefault(assignment.group_id, set()).add(assignment.team_id)

    for group_ids in team_group_ids.values():
        group_ids.sort(key=lambda group_id: (group_order.get(group_id, 10_000), group_names.get(group_id, "")))

    checkpoint_group_ids: dict[int, set[int]] = {}
    for group_id, checkpoint_ids in group_checkpoint_order.items():
        for checkpoint_id in checkpoint_ids:
            checkpoint_group_ids.setdefault(checkpoint_id, set()).add(group_id)

    checkins = []
    if not selected_group_id or (team_ids and checkpoint_ids_for_view):
        checkins_query = Checkin.query.filter(Checkin.competition_id == comp_id).options(
            joinedload(Checkin.team), joinedload(Checkin.checkpoint)
        )
        if selected_group_id:
            checkins_query = checkins_query.filter(Checkin.team_id.in_(team_ids)).filter(
                Checkin.checkpoint_id.in_(checkpoint_ids_for_view)
            )
        checkins = checkins_query.order_by(Checkin.timestamp.asc(), Checkin.id.asc()).all()

    team_cp_times: dict[int, dict[int, datetime]] = {team_id: {} for team_id in team_ids}
    latest_by_team: dict[int, Checkin] = {}
    latest_by_checkpoint: dict[int, Checkin] = {}
    arrived_team_ids_by_checkpoint: dict[int, set[int]] = {}
    for checkin in checkins:
        team_cp_times.setdefault(checkin.team_id, {}).setdefault(checkin.checkpoint_id, checkin.timestamp)
        latest_by_team[checkin.team_id] = checkin
        latest_by_checkpoint[checkin.checkpoint_id] = checkin
        arrived_team_ids_by_checkpoint.setdefault(checkin.checkpoint_id, set()).add(checkin.team_id)

    has_team_group_assignments = bool(team_group_ids)
    checkpoint_rows = []
    for checkpoint in checkpoints:
        linked_group_ids = checkpoint_group_ids.get(checkpoint.id, set())
        if selected_group_id:
            expected_team_ids = set(all_team_ids)
        elif has_team_group_assignments and linked_group_ids:
            expected_team_ids = set()
            for group_id in linked_group_ids:
                expected_team_ids.update(group_team_ids.get(group_id, set()))
        else:
            expected_team_ids = set(all_team_ids)

        arrived_team_ids = arrived_team_ids_by_checkpoint.get(checkpoint.id, set())
        expected_arrived_ids = arrived_team_ids & expected_team_ids
        extra_arrivals = arrived_team_ids - expected_team_ids
        expected_count = len(expected_team_ids)
        arrived_count = len(expected_arrived_ids)
        latest = latest_by_checkpoint.get(checkpoint.id)
        progress = round((arrived_count / expected_count) * 100) if expected_count else 0
        checkpoint_rows.append(
            {
                "id": checkpoint.id,
                "name": checkpoint.name,
                "expected_count": expected_count,
                "arrived_count": arrived_count,
                "missing_count": max(expected_count - arrived_count, 0),
                "extra_count": len(extra_arrivals),
                "progress": progress,
                "latest_team": latest.team.name if latest and latest.team else "",
                "latest_at": _iso_dt(latest.timestamp if latest else None),
                "latest_at_label": _display_dt(latest.timestamp if latest else None),
            }
        )

    team_rows = []
    finished_count = 0
    started_count = 0
    dnf_count = 0
    not_finished_count = 0
    for team in teams:
        route_group_ids = team_group_ids.get(team.id, [])
        route_group_id = selected_group_id or (route_group_ids[0] if route_group_ids else None)
        route_group_name = group_names.get(route_group_id, "") if route_group_id else ""
        cp_times = team_cp_times.get(team.id, {})
        latest = latest_by_team.get(team.id)

        start_checkpoint_id = group_start.get(route_group_id) if route_group_id else None
        finish_checkpoint_id = group_finish.get(route_group_id) if route_group_id else None
        started_at = cp_times.get(start_checkpoint_id) if start_checkpoint_id else None
        finished_at = cp_times.get(finish_checkpoint_id) if finish_checkpoint_id else None

        if not started_at and not route_group_id and cp_times:
            started_at = min(cp_times.values())

        if team.dnf:
            dnf_count += 1
        if started_at:
            started_count += 1
        if finished_at:
            finished_count += 1
        if team.dnf:
            status = "dnf"
            elapsed_end = finished_at or (latest.timestamp if latest else now)
        elif finished_at:
            status = "finished"
            elapsed_end = finished_at
        elif started_at:
            status = "on_course" if route_group_id else "started"
            elapsed_end = now
            not_finished_count += 1
        elif route_group_id:
            status = "not_started"
            elapsed_end = None
            not_finished_count += 1
        else:
            status = "not_routed"
            elapsed_end = None
            not_finished_count += 1

        elapsed_minutes = _minutes_between(started_at, elapsed_end)
        latest_at = latest.timestamp if latest else None
        team_rows.append(
            {
                "id": team.id,
                "name": team.name,
                "number": team.number,
                "group": route_group_name,
                "status": status,
                "started_at": _iso_dt(started_at),
                "started_at_label": _display_dt(started_at),
                "finished_at": _iso_dt(finished_at),
                "finished_at_label": _display_dt(finished_at),
                "latest_checkpoint": latest.checkpoint.name if latest and latest.checkpoint else "",
                "latest_at": _iso_dt(latest_at),
                "latest_at_label": _display_dt(latest_at),
                "elapsed_minutes": elapsed_minutes,
                "elapsed_label": _minutes_label(elapsed_minutes),
                "_latest_sort": latest_at,
            }
        )

    team_rows = _sort_team_rows(team_rows, sort)
    for row in team_rows:
        row.pop("_latest_sort", None)

    return {
        "generated_at": _iso_dt(now),
        "generated_at_label": _display_dt(now),
        "filters": {
            "group_id": selected_group_id,
            "sort": sort if sort in VALID_TEAM_SORTS else "number_asc",
        },
        "summary": {
            "teams_count": len(teams),
            "started_count": started_count,
            "finished_count": finished_count,
            "not_finished_count": not_finished_count,
            "dnf_count": dnf_count,
            "checkins_count": len(checkins),
        },
        "checkpoints": checkpoint_rows,
        "teams": team_rows,
    }
