# app/blueprints/judge/routes.py
"""The judge shell: mobile-first, scoped to the judge's checkpoint.

Three tabs (My CP / Teams / Results) plus the bulk-entry Table sub-tab
for checkpoints with bulk_entry_enabled (redesign plan 3.5/3.6). My CP
merges the old RFID console, manual check-in and score form into one
scan-first flow; corrections are re-submissions from the arrived list.
"""

from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_babel import gettext as _
from flask_login import current_user
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import (
    Checkin,
    Checkpoint,
    CheckpointGroup,
    JudgeCheckpoint,
    ScoreEntry,
    Team,
    TeamGroup,
)
from app.utils.audit import record_audit_event
from app.utils.competition import get_current_competition_id, get_current_competition_role
from app.utils.judge_view import build_judge_checkpoint_view
from app.utils.perms import roles_required
from app.utils.redirects import safe_redirect_target
from app.utils.scoring import compute_entry_total, resolve_fields
from app.utils.sheets_sync import mark_arrival_checkbox, update_checkpoint_scores
from app.utils.time import utcnow_naive

judge_bp = Blueprint("judge", __name__, template_folder="../../templates")


def _session_key(comp_id: int) -> str:
    return f"judge_checkpoint_{comp_id}"


def _available_checkpoints(comp_id: int) -> tuple[list[Checkpoint], int | None]:
    """Checkpoints this user may work on, plus the default checkpoint id."""
    role = get_current_competition_role()
    if role == "admin":
        checkpoints = (
            Checkpoint.query.filter(Checkpoint.competition_id == comp_id)
            .order_by(Checkpoint.position.asc().nulls_last(), Checkpoint.name.asc())
            .all()
        )
        return checkpoints, (checkpoints[0].id if checkpoints else None)
    assigned = (
        JudgeCheckpoint.query.join(Checkpoint, JudgeCheckpoint.checkpoint_id == Checkpoint.id)
        .filter(
            JudgeCheckpoint.user_id == current_user.id,
            Checkpoint.competition_id == comp_id,
        )
        .order_by(Checkpoint.position.asc().nulls_last(), Checkpoint.name.asc())
        .all()
    )
    checkpoints = [jc.checkpoint for jc in assigned if jc.checkpoint]
    default_row = next((jc for jc in assigned if jc.is_default), None)
    default_id = default_row.checkpoint_id if default_row else (checkpoints[0].id if checkpoints else None)
    return checkpoints, default_id


def _current_checkpoint(comp_id: int) -> tuple[Checkpoint | None, list[Checkpoint]]:
    checkpoints, default_id = _available_checkpoints(comp_id)
    if not checkpoints:
        return None, []
    allowed_ids = {cp.id for cp in checkpoints}
    selected_id = session.get(_session_key(comp_id))
    if selected_id not in allowed_ids:
        selected_id = default_id
    checkpoint = next((cp for cp in checkpoints if cp.id == selected_id), None)
    return checkpoint, checkpoints


def _require_context():
    comp_id = get_current_competition_id()
    if not comp_id:
        flash(_("Select a competition first."), "warning")
        return None, None, None, redirect(url_for("main.select_competition"))
    checkpoint, checkpoints = _current_checkpoint(comp_id)
    if checkpoint is None:
        flash(_("No checkpoints assigned yet. Ask an admin to assign you a checkpoint."), "warning")
        return comp_id, None, checkpoints, redirect(url_for("main.select_competition"))
    return comp_id, checkpoint, checkpoints, None


@judge_bp.route("/checkpoint", methods=["POST"])
@roles_required("judge", "admin")
def set_checkpoint():
    comp_id = get_current_competition_id()
    if not comp_id:
        return redirect(url_for("main.select_competition"))
    checkpoint_id = request.form.get("checkpoint_id", type=int)
    checkpoints, _default = _available_checkpoints(comp_id)
    if checkpoint_id in {cp.id for cp in checkpoints}:
        session[_session_key(comp_id)] = checkpoint_id
    # Validate 'next' so a crafted form value can't turn this into an
    # open redirect (matches the auth/main next-redirect handling).
    return redirect(safe_redirect_target(request.form.get("next"), url_for("judge.home")))


@judge_bp.route("/", methods=["GET"])
@roles_required("judge", "admin")
def home():
    comp_id, checkpoint, checkpoints, error_redirect = _require_context()
    if error_redirect:
        return error_redirect
    view = build_judge_checkpoint_view(comp_id, checkpoint.id)
    teams = (
        Team.query.filter(Team.competition_id == comp_id)
        .order_by(Team.number.asc().nulls_last(), Team.name.asc())
        .all()
    )
    return render_template(
        "judge_home.html",
        active_tab="home",
        checkpoint=checkpoint,
        checkpoints=checkpoints,
        view=view,
        teams=teams,
    )


@judge_bp.route("/teams", methods=["GET"])
@roles_required("judge", "admin")
def teams():
    comp_id, checkpoint, checkpoints, error_redirect = _require_context()
    if error_redirect:
        return error_redirect
    view = build_judge_checkpoint_view(comp_id, checkpoint.id)
    return render_template(
        "judge_teams.html",
        active_tab="teams",
        checkpoint=checkpoint,
        checkpoints=checkpoints,
        view=view,
    )


@judge_bp.route("/results", methods=["GET"])
@roles_required("judge", "admin")
def results():
    comp_id, checkpoint, checkpoints, error_redirect = _require_context()
    if error_redirect:
        return error_redirect
    from app.blueprints.scores.routes import _build_scores_context

    context = _build_scores_context(comp_id, None)
    rows_by_group: dict[str, list[dict]] = {}
    for row in context.get("rows") or []:
        rows_by_group.setdefault(row.get("group") or _("No group"), []).append(row)
    return render_template(
        "judge_results.html",
        active_tab="results",
        checkpoint=checkpoint,
        checkpoints=checkpoints,
        rows_by_group=rows_by_group,
    )


def _bulk_grid_data(comp_id: int, checkpoint: Checkpoint) -> list[dict]:
    """One section per category whose route includes the checkpoint:
    resolved fields as columns, teams as rows with their latest values."""
    from app.utils.paths import group_ids_containing_checkpoint

    member_ids = group_ids_containing_checkpoint(comp_id, checkpoint.id)
    groups = (
        CheckpointGroup.query.filter(CheckpointGroup.id.in_(member_ids))
        .order_by(CheckpointGroup.position.asc().nulls_last(), CheckpointGroup.name.asc())
        .all()
        if member_ids
        else []
    )
    latest_entry: dict[int, ScoreEntry] = {}
    for entry in (
        ScoreEntry.query.filter(
            ScoreEntry.competition_id == comp_id,
            ScoreEntry.checkpoint_id == checkpoint.id,
        )
        .order_by(ScoreEntry.created_at.desc())
        .all()
    ):
        latest_entry.setdefault(entry.team_id, entry)

    sections = []
    for group in groups:
        fields = resolve_fields(checkpoint.id, group.id)
        if checkpoint.dead_time_enabled:
            fields = [{"key": "dead_time", "label": _("Dead time (min)"), "hint": None}] + fields
        if not fields:
            fields = [{"key": "points", "label": _("Score"), "hint": None}]
        group_teams = (
            Team.query.join(TeamGroup, TeamGroup.team_id == Team.id)
            .filter(
                Team.competition_id == comp_id,
                TeamGroup.group_id == group.id,
                TeamGroup.active.is_(True),
            )
            .order_by(Team.number.asc().nulls_last(), Team.name.asc())
            .all()
        )
        rows = []
        for team in group_teams:
            entry = latest_entry.get(team.id)
            raw = entry.raw_fields if entry else {}
            rows.append(
                {
                    "team": team,
                    # 'raw_values', not 'values': Jinja attribute lookup on a
                    # dict would otherwise resolve the dict.values method.
                    "raw_values": {f["key"]: (raw or {}).get(f["key"], "") for f in fields},
                    "total": entry.total if entry else None,
                }
            )
        sections.append({"group": group, "fields": fields, "rows": rows})
    return sections


@judge_bp.route("/table", methods=["GET"])
@roles_required("judge", "admin")
def table():
    comp_id, checkpoint, checkpoints, error_redirect = _require_context()
    if error_redirect:
        return error_redirect
    if not checkpoint.bulk_entry_enabled and get_current_competition_role() != "admin":
        flash(_("Bulk entry is not enabled for this checkpoint."), "warning")
        return redirect(url_for("judge.home"))
    sections = _bulk_grid_data(comp_id, checkpoint)
    return render_template(
        "judge_table.html",
        active_tab="table",
        checkpoint=checkpoint,
        checkpoints=checkpoints,
        sections=sections,
    )


@judge_bp.route("/table", methods=["POST"])
@roles_required("judge", "admin")
def table_submit():
    comp_id, checkpoint, _checkpoints, error_redirect = _require_context()
    if error_redirect:
        return error_redirect
    if not checkpoint.bulk_entry_enabled and get_current_competition_role() != "admin":
        flash(_("Bulk entry is not enabled for this checkpoint."), "warning")
        return redirect(url_for("judge.home"))

    sections = _bulk_grid_data(comp_id, checkpoint)
    saved = 0
    for section in sections:
        group = section["group"]
        fields = section["fields"]
        # Identical for every team row in this section; resolve once
        # instead of re-querying ScoreField/ScoreFieldGroup per team.
        resolved = resolve_fields(checkpoint.id, group.id)
        for row in section["rows"]:
            team = row["team"]
            values: dict[str, str] = {}
            changed = False
            for field in fields:
                form_key = f"team_{team.id}_{field['key']}"
                if form_key not in request.form:
                    continue
                raw = (request.form.get(form_key) or "").strip()
                if raw == "":
                    # The grid pre-fills stored values, so an emptied cell
                    # is an explicit clear: mark the row changed and leave
                    # the key out of the new latest entry. Ignoring it
                    # silently kept e.g. points entered for the wrong team.
                    if str(row["raw_values"].get(field["key"], "")).strip() != "":
                        changed = True
                    continue
                try:
                    number = float(raw)
                except ValueError:
                    flash(
                        _(
                            "Invalid value for %(team)s / %(field)s.",
                            team=team.name,
                            field=field["key"],
                        ),
                        "warning",
                    )
                    return redirect(url_for("judge.table"))
                if number < 0:
                    flash(_("Score cannot be negative."), "warning")
                    return redirect(url_for("judge.table"))
                values[field["key"]] = raw
                if str(row["raw_values"].get(field["key"], "")) != raw:
                    changed = True
            # changed alone gates the save: values may be empty when the
            # judge cleared a row's only value, which still needs a new
            # (empty) latest entry to supersede the old one.
            if not changed:
                continue

            # Paper stations may have no scanned arrival; record one so the
            # entry hangs off a checkin like the single-team flow does.
            checkin = Checkin.query.filter_by(
                competition_id=comp_id, team_id=team.id, checkpoint_id=checkpoint.id
            ).first()
            if checkin is None and not checkpoint.is_virtual:
                # Insert inside a savepoint so a concurrent scan racing on
                # uq_team_checkpoint doesn't 500 and lose the whole grid;
                # on collision we reuse the existing arrival. Mirrors
                # /api/scores/submit and /api/scores/resolve.
                new_checkin = Checkin(
                    competition_id=comp_id,
                    team_id=team.id,
                    checkpoint_id=checkpoint.id,
                    timestamp=utcnow_naive(),
                    created_by_user_id=current_user.id,
                )
                try:
                    with db.session.begin_nested():
                        db.session.add(new_checkin)
                except IntegrityError:
                    checkin = Checkin.query.filter_by(
                        competition_id=comp_id, team_id=team.id, checkpoint_id=checkpoint.id
                    ).first()
                else:
                    checkin = new_checkin
                    try:
                        mark_arrival_checkbox(team.id, checkpoint.id, checkin.timestamp)
                    except Exception:
                        pass

            total = compute_entry_total(
                values,
                resolved,
                {"team_id": team.id, "competition_id": comp_id, "group_id": group.id},
            )
            entry = ScoreEntry(
                competition_id=comp_id,
                checkin_id=checkin.id if checkin else None,
                team_id=team.id,
                checkpoint_id=checkpoint.id,
                judge_user_id=current_user.id,
                raw_fields=values,
                total=total,
                created_at=utcnow_naive(),
            )
            db.session.add(entry)
            db.session.flush()
            record_audit_event(
                competition_id=comp_id,
                event_type="score_submitted",
                entity_type="score_entry",
                entity_id=entry.id,
                actor_user=current_user,
                summary=f"Bulk score entered for team {team.name} at {checkpoint.name}.",
                details={"team_id": team.id, "raw_fields": values, "total": total},
                created_at=entry.created_at,
            )
            saved += 1
            try:
                sheet_values = dict(values)
                if total is not None:
                    sheet_values["points"] = total
                update_checkpoint_scores(team.id, checkpoint.id, group.name, sheet_values, entry.created_at)
            except Exception:
                pass

    db.session.commit()
    flash(_("Saved %(count)s team scores.", count=saved), "success")
    return redirect(url_for("judge.table"))
