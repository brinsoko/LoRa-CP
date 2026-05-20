from __future__ import annotations

import time
from datetime import datetime

from flask import current_app
from sqlalchemy import func

from app.extensions import db
from app.models import (
    Checkin,
    Checkpoint,
    CheckpointGroup,
    CheckpointGroupLink,
    ScoreEntry,
    SheetConfig,
    Team,
    TeamGroup,
)
from app.utils.competition import get_competition_group_order
from app.utils.export_safety import escape_formula_cell
from app.utils.lang_store import load_lang
from app.utils.sheets_client import get_sheets_client
from app.utils.sheets_settings import sheets_sync_enabled


def _norm_name(value: str | None) -> str:
    return (value or "").strip().casefold()


def _resolve_group_from_cfg(
    competition_id: int | None,
    grp_def: dict,
    cache: dict[int, list[CheckpointGroup]],
) -> CheckpointGroup | None:
    if competition_id is None:
        return None
    groups = cache.get(competition_id)
    if groups is None:
        groups = CheckpointGroup.query.filter(CheckpointGroup.competition_id == competition_id).all()
        cache[competition_id] = groups

    group_id = grp_def.get("group_id")
    if group_id is not None:
        try:
            group_id = int(group_id)
        except Exception:
            group_id = None
    if group_id is not None:
        for g in groups:
            if g.id == group_id:
                return g

    name_norm = _norm_name(grp_def.get("name"))
    if not name_norm:
        return None
    for g in groups:
        if _norm_name(g.name) == name_norm:
            return g
    return None


def _get_global_group_order(spreadsheet_id: str) -> list[str]:
    cfgs = (
        SheetConfig.query.filter(SheetConfig.spreadsheet_id == spreadsheet_id)
        .filter(SheetConfig.tab_type == "checkpoint")
        .order_by(SheetConfig.id.asc())
        .all()
    )
    for cfg in cfgs:
        if cfg.config and cfg.config.get("groups"):
            return [grp.get("name") for grp in (cfg.config or {}).get("groups", []) if grp.get("name")]
    return []


def _get_default_group_order(spreadsheet_id: str | None, competition_id: int | None) -> list[str]:
    if competition_id:
        order = get_competition_group_order(competition_id)
        if order:
            return order
    if spreadsheet_id:
        return _get_global_group_order(spreadsheet_id)
    return []


def _get_group_checkpoint_order_from_db() -> dict[str, list[str]]:
    """Return default checkpoint order per group based on CheckpointGroupLink positions."""
    orders: dict[str, list[str]] = {}
    groups = CheckpointGroup.query.options(
        db.joinedload(CheckpointGroup.checkpoint_links).joinedload(CheckpointGroupLink.checkpoint)
    ).all()
    for g in groups:
        # order by position asc
        ordered = sorted(g.checkpoint_links, key=lambda cl: cl.position if cl.position is not None else 0)
        orders[g.name] = [cl.checkpoint.name for cl in ordered if cl.checkpoint]
    return orders


def _sort_groups(groups: list[CheckpointGroup], order: list[str]) -> list[CheckpointGroup]:
    order_norm = [g.lower().strip() for g in order]

    def key(g: CheckpointGroup):
        norm = g.name.lower().strip()
        return (order_norm.index(norm) if norm in order_norm else len(order_norm), g.name)

    return sorted(groups, key=key)


def _group_start_cols_from_config(cfg: dict) -> list[int]:
    cols = []
    current = 1
    dead_time_enabled = bool(cfg.get("dead_time_enabled"))
    time_enabled = bool(cfg.get("time_enabled"))
    for grp in cfg.get("groups", []):
        cols.append(current)
        current += 1 + (1 if dead_time_enabled else 0) + (1 if time_enabled else 0) + len(grp.get("fields", [])) + 1
    return cols


def _time_col_for_group_in_cp(cfg_blob: dict, group_name: str) -> int | None:
    """Return the 1-based Time column index for `group_name` in a CP's
    SheetConfig.config, or None when time_enabled is off or the group
    isn't present. Used by the Score tab's časovnica + found formulas
    to look up arrival timestamps across per-CP tabs."""
    if not cfg_blob.get("time_enabled"):
        return None
    dead_time_enabled = bool(cfg_blob.get("dead_time_enabled"))
    cols = _group_start_cols_from_config(cfg_blob)
    target = (group_name or "").strip().lower()
    for grp, start_col in zip(cfg_blob.get("groups", []), cols, strict=False):
        name = (grp.get("name") or "").strip().lower()
        if name == target:
            # Layout per group: [group_name, dead_time?, time, fields..., points]
            return start_col + 1 + (1 if dead_time_enabled else 0)
    return None


def _team_col_for_group_in_cp(cfg_blob: dict, group_name: str) -> int | None:
    """1-based column of the team-number column inside a group's block
    on a per-CP tab. The publish grid puts the team label in the first
    column of each group block."""
    cols = _group_start_cols_from_config(cfg_blob)
    target = (group_name or "").strip().lower()
    for grp, start_col in zip(cfg_blob.get("groups", []), cols, strict=False):
        if (grp.get("name") or "").strip().lower() == target:
            return start_col
    return None


def _build_global_rule_formulas(
    *,
    group,
    global_rule: dict,
    cp_id_to_name: dict[int, str],
    relevant_cfgs: list,
    row_idx: int,
    dead_time_sum_expr: str,
) -> tuple[str, str]:
    """Construct the per-team Časovnica + Found-points formulas for the
    Score tab so the spreadsheet computes the final score without our
    system. Both formulas reach across per-CP tabs via INDEX/MATCH
    against the Time column.

    Returns (casovnica_formula, found_formula). When the rule can't be
    expressed (no start/end CP, or those tabs don't have time_enabled),
    returns "=0" placeholders so the Total column still adds cleanly.
    """
    from gspread.utils import rowcol_to_a1

    def _col_letter(col: int) -> str:
        return rowcol_to_a1(1, col).rstrip("1")

    # --- Časovnica (Article 39) ---
    cas_formula = "=0"
    time_rule = global_rule.get("time") or {}
    start_cp_name = cp_id_to_name.get(time_rule.get("start_checkpoint_id"))
    end_cp_name = cp_id_to_name.get(time_rule.get("end_checkpoint_id"))
    if start_cp_name and end_cp_name:
        # Find the Time/Team columns on each of those tabs for this
        # group. If either tab's time_enabled is off (no Time column
        # was written), we can't compute the duration on the sheet.
        start_cfg = next((c for c in relevant_cfgs if c.tab_name == start_cp_name), None)
        end_cfg = next((c for c in relevant_cfgs if c.tab_name == end_cp_name), None)
        if start_cfg is not None and end_cfg is not None:
            s_time_col = _time_col_for_group_in_cp(start_cfg.config or {}, group.name)
            s_team_col = _team_col_for_group_in_cp(start_cfg.config or {}, group.name)
            e_time_col = _time_col_for_group_in_cp(end_cfg.config or {}, group.name)
            e_team_col = _team_col_for_group_in_cp(end_cfg.config or {}, group.name)
            if all(c is not None for c in (s_time_col, s_team_col, e_time_col, e_team_col)):
                s_t = _col_letter(s_time_col)
                s_a = _col_letter(s_team_col)
                e_t = _col_letter(e_time_col)
                e_a = _col_letter(e_team_col)
                start_lookup = (
                    f"INDEX('{start_cp_name}'!{s_t}:{s_t}; MATCH(B{row_idx}; '{start_cp_name}'!{s_a}:{s_a}; 0))"
                )
                end_lookup = (
                    f"INDEX('{end_cp_name}'!{e_t}:{e_t}; MATCH(B{row_idx}; '{end_cp_name}'!{e_a}:{e_a}; 0))"
                )
                max_p = _fmt_num(time_rule.get("max_points") or 0)
                threshold = _fmt_num(time_rule.get("threshold_minutes") or 0)
                penalty_p = _fmt_num(time_rule.get("penalty_points") or 0)
                # penalty_minutes is the "per N minutes" denominator;
                # never let it be 0 (would divide-by-zero in the sheet).
                try:
                    pm = float(time_rule.get("penalty_minutes") or 0)
                except (TypeError, ValueError):
                    pm = 0
                penalty_m = _fmt_num(pm) if pm > 0 else "1"
                min_p = _fmt_num(time_rule.get("min_points") or 0)
                cas_formula = (
                    f"=IFERROR(IF({end_lookup}=\"\"; 0; "
                    f"MAX({max_p}-MAX(0; ({end_lookup}-{start_lookup})*1440-({dead_time_sum_expr})-{threshold})"
                    f"/{penalty_m}*{penalty_p}; {min_p})); 0)"
                )

    # --- Found-points (Article 38) ---
    found_formula = "=0"
    found_rule = global_rule.get("found") or {}
    points_per = found_rule.get("points_per")
    if points_per is not None:
        excluded_names: set[str] = set()
        if found_rule.get("exclude_start_checkpoint") and start_cp_name:
            excluded_names.add(start_cp_name)
        if found_rule.get("exclude_end_checkpoint") and end_cp_name:
            excluded_names.add(end_cp_name)
        per_cp_terms: list[str] = []
        for cfg in relevant_cfgs:
            if cfg.tab_name in excluded_names:
                continue
            t_col = _time_col_for_group_in_cp(cfg.config or {}, group.name)
            a_col = _team_col_for_group_in_cp(cfg.config or {}, group.name)
            if t_col is None or a_col is None:
                # CP doesn't track time for this group; can't tell
                # arrival from the sheet alone, so it doesn't contribute.
                continue
            tcl = _col_letter(t_col)
            acl = _col_letter(a_col)
            lookup = (
                f"INDEX('{cfg.tab_name}'!{tcl}:{tcl}; MATCH(B{row_idx}; '{cfg.tab_name}'!{acl}:{acl}; 0))"
            )
            per_cp_terms.append(f"IFERROR(IF({lookup}<>\"\"; 1; 0); 0)")
        if per_cp_terms:
            found_formula = f"={_fmt_num(points_per)}*({'+'.join(per_cp_terms)})"

    return cas_formula, found_formula


def sync_all_checkpoint_tabs(competition_id: int | None = None):
    """Refresh team numbers and checkbox validation for all checkpoint-type tab configs."""
    if not sheets_sync_enabled():
        return
    configs = SheetConfig.query.filter(SheetConfig.tab_type == "checkpoint")
    if competition_id is not None:
        configs = configs.filter(SheetConfig.competition_id == competition_id)
    configs = configs.all()
    if not configs:
        return

    try:
        client = get_sheets_client(current_app)
    except Exception as exc:
        current_app.logger.warning("Sheets sync skipped: %s", exc)
        return

    group_cache: dict[int, list[CheckpointGroup]] = {}
    for cfg in configs:
        comp_id = cfg.competition_id
        groups = (cfg.config or {}).get("groups", [])
        if not groups:
            continue
        group_cols = _group_start_cols_from_config(cfg.config or {})

        for grp, start_col in zip(groups, group_cols, strict=False):
            db_group = _resolve_group_from_cfg(comp_id, grp, group_cache)
            if not db_group:
                continue
            nums = (
                db.session.query(Team.id, Team.number, Team.name)
                .join(TeamGroup, TeamGroup.team_id == Team.id)
                .filter(TeamGroup.group_id == db_group.id, Team.competition_id == comp_id)
                .order_by(Team.number.asc().nulls_last(), Team.name.asc())
                .all()
            )
            values = [n[1] if n[1] is not None else (n[2] or "") for n in nums]
            if values:
                client.update_column(cfg.spreadsheet_id, cfg.tab_name, start_col, 2, values)


def mark_arrival_checkbox(team_id: int, checkpoint_id: int, arrived_at: datetime | None = None):
    """Public entrypoint — schedules the Sheets write on the background worker
    when an app context is available, falls back to a synchronous call when
    not (e.g. tests, CLI scripts, or direct callers that have already chosen
    to take the latency)."""
    try:
        app = current_app._get_current_object()
    except RuntimeError:
        return mark_arrival_checkbox_sync(team_id, checkpoint_id, arrived_at)
    if app.config.get("SHEETS_SYNC_INLINE"):
        return mark_arrival_checkbox_sync(team_id, checkpoint_id, arrived_at)
    from app.utils.sheets_sync_worker import enqueue_mark_arrival

    enqueue_mark_arrival(app, team_id, checkpoint_id, arrived_at)


def mark_arrival_checkbox_sync(team_id: int, checkpoint_id: int, arrived_at: datetime | None = None):
    """Set arrived checkbox TRUE for the given team/checkpoint in any linked checkpoint tab."""
    if not sheets_sync_enabled():
        return
    team = db.session.get(Team, team_id)
    checkpoint = db.session.get(Checkpoint, checkpoint_id)
    if not team or not checkpoint:
        return

    configs = (
        SheetConfig.query.filter(SheetConfig.tab_type == "checkpoint")
        .filter(SheetConfig.checkpoint_id == checkpoint_id)
        .filter(SheetConfig.competition_id == checkpoint.competition_id)
        .all()
    )
    if not configs:
        return

    try:
        client = get_sheets_client(current_app)
    except Exception as exc:
        current_app.logger.warning("Sheets arrival sync skipped: %s", exc)
        return

    # Team may belong to multiple groups; mark in each matching block.
    group_cache: dict[int, list[CheckpointGroup]] = {}
    for cfg in configs:
        group_defs = (cfg.config or {}).get("groups", [])
        group_cols = _group_start_cols_from_config(cfg.config or {})
        time_enabled = bool((cfg.config or {}).get("time_enabled"))
        for grp_def, start_col in zip(group_defs, group_cols, strict=False):
            db_group = _resolve_group_from_cfg(cfg.competition_id, grp_def, group_cache)
            if not db_group:
                continue
            # Is team in this group?
            belongs = TeamGroup.query.filter(TeamGroup.team_id == team.id, TeamGroup.group_id == db_group.id).first()
            if not belongs:
                continue
            # Determine row by sorted team numbers
            nums = (
                db.session.query(Team.id, Team.number, Team.name)
                .join(TeamGroup, TeamGroup.team_id == Team.id)
                .filter(TeamGroup.group_id == db_group.id)
                .order_by(Team.number.asc().nulls_last(), Team.name.asc())
                .all()
            )
            ordered = [n[0] for n in nums]
            try:
                idx = ordered.index(team.id)
            except ValueError:
                continue
            row = 2 + idx  # header at row 1
            time_col = None
            if time_enabled:
                dead_time_enabled = bool((cfg.config or {}).get("dead_time_enabled"))
                time_col = start_col + 1
                if dead_time_enabled:
                    time_col += 1  # shift if dead time sits before time
            try:
                if time_col:
                    ts = arrived_at or datetime.now()
                    ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")
                    client.update_cell(cfg.spreadsheet_id, cfg.tab_name, row, time_col, ts_str)
            except Exception as exc:
                current_app.logger.warning("Could not update arrival checkbox: %s", exc)


def update_checkpoint_scores(
    team_id: int, checkpoint_id: int, group_name: str, values: dict, scored_at: datetime | None = None
):
    """Public entrypoint — see mark_arrival_checkbox for the dispatch policy."""
    try:
        app = current_app._get_current_object()
    except RuntimeError:
        return update_checkpoint_scores_sync(team_id, checkpoint_id, group_name, values, scored_at)
    if app.config.get("SHEETS_SYNC_INLINE"):
        return update_checkpoint_scores_sync(team_id, checkpoint_id, group_name, values, scored_at)
    from app.utils.sheets_sync_worker import enqueue_update_scores

    enqueue_update_scores(app, team_id, checkpoint_id, group_name, values, scored_at)


def update_checkpoint_scores_sync(
    team_id: int, checkpoint_id: int, group_name: str, values: dict, scored_at: datetime | None = None
):
    """Update score-related fields for a team in a checkpoint tab based on config layout."""
    if not sheets_sync_enabled():
        return
    team = db.session.get(Team, team_id)
    checkpoint = db.session.get(Checkpoint, checkpoint_id)
    if not team or not checkpoint:
        return

    configs = (
        SheetConfig.query.filter(SheetConfig.tab_type == "checkpoint")
        .filter(SheetConfig.checkpoint_id == checkpoint_id)
        .filter(SheetConfig.competition_id == checkpoint.competition_id)
        .all()
    )
    if not configs:
        return

    try:
        client = get_sheets_client(current_app)
    except Exception as exc:
        current_app.logger.warning("Sheets score sync skipped: %s", exc)
        return

    group_cache: dict[int, list[CheckpointGroup]] = {}
    for cfg in configs:
        group_defs = (cfg.config or {}).get("groups", [])
        group_cols = _group_start_cols_from_config(cfg.config or {})
        time_enabled = bool((cfg.config or {}).get("time_enabled"))
        dead_time_enabled = bool((cfg.config or {}).get("dead_time_enabled"))
        dead_time_header = (cfg.config or {}).get("dead_time_header") or "Dead Time"
        time_header = (cfg.config or {}).get("time_header") or "Time"
        points_header = (cfg.config or {}).get("points_header") or "Points"

        for grp_def, start_col in zip(group_defs, group_cols, strict=False):
            grp_name = (grp_def.get("name") or "").strip()
            if _norm_name(grp_name) != _norm_name(group_name):
                continue

            db_group = _resolve_group_from_cfg(cfg.competition_id, grp_def, group_cache)
            if not db_group:
                continue

            nums = (
                db.session.query(Team.id, Team.number, Team.name)
                .join(TeamGroup, TeamGroup.team_id == Team.id)
                .filter(TeamGroup.group_id == db_group.id, Team.competition_id == cfg.competition_id)
                .order_by(Team.number.asc().nulls_last(), Team.name.asc())
                .all()
            )
            ordered = [n[0] for n in nums]
            try:
                idx = ordered.index(team.id)
            except ValueError:
                continue
            row = 2 + idx

            col = start_col + 1
            if dead_time_enabled:
                if dead_time_header in values or "dead_time" in values:
                    client.update_cell(
                        cfg.spreadsheet_id,
                        cfg.tab_name,
                        row,
                        col,
                        values.get(dead_time_header, values.get("dead_time")),
                    )
                col += 1
            if time_enabled:
                if time_header in values or "time" in values:
                    client.update_cell(
                        cfg.spreadsheet_id, cfg.tab_name, row, col, values.get(time_header, values.get("time"))
                    )
                elif scored_at:
                    client.update_cell(
                        cfg.spreadsheet_id, cfg.tab_name, row, col, scored_at.strftime("%Y-%m-%d %H:%M:%S")
                    )
                col += 1

            for field_name in grp_def.get("fields") or []:
                if field_name in values:
                    client.update_cell(cfg.spreadsheet_id, cfg.tab_name, row, col, values.get(field_name))
                col += 1

            # Points cell: when this group's config flags points_formula
            # (set by publish_local_configs_to_spreadsheet after embedding
            # a per-row formula), skip the write so we don't clobber the
            # formula with a raw number. The spreadsheet then recomputes
            # Points from the raw field cells we just wrote.
            if not grp_def.get("points_formula"):
                if points_header in values or "points" in values:
                    client.update_cell(
                        cfg.spreadsheet_id, cfg.tab_name, row, col, values.get(points_header, values.get("points"))
                    )


def build_arrivals_tab(
    spreadsheet_id: str,
    tab_name: str,
    competition_id: int | None = None,
    group_order_override: list[str] | None = None,
    checkpoint_order_override: list[str] | None = None,
    per_group_checkpoint_order: dict[str, list[str]] | None = None,
):
    """Build arrivals matrix (groups x checkpoint tabs)."""
    if not sheets_sync_enabled():
        return "Sheets sync is disabled."
    group_order = group_order_override or _get_default_group_order(spreadsheet_id, competition_id)
    # The per-group checkpoint ordering is computed inside the second pass
    # (build_score_tab) when actually needed; the assignment that lived here
    # was a refactor leftover that issued a redundant DB query on every call.

    cp_configs = (
        SheetConfig.query.filter(SheetConfig.spreadsheet_id == spreadsheet_id)
        .filter(SheetConfig.tab_type == "checkpoint")
        .order_by(SheetConfig.tab_name.asc())
        .all()
    )
    # Exclude virtual checkpoints from arrivals — they have no check-in concept.
    from app.models import Checkpoint as _CP

    virtual_cp_ids = {row[0] for row in db.session.query(_CP.id).filter(_CP.is_virtual.is_(True)).all()}
    cp_configs = [cfg for cfg in cp_configs if cfg.checkpoint_id not in virtual_cp_ids]
    if not cp_configs:
        return "No checkpoint tab configs found."
    if checkpoint_order_override:
        name_to_cfg = {cfg.tab_name: cfg for cfg in cp_configs}
        ordered = []
        for name in checkpoint_order_override:
            cfg = name_to_cfg.get(name)
            if cfg:
                ordered.append(cfg)
        # append any remaining
        for cfg in cp_configs:
            if cfg not in ordered:
                ordered.append(cfg)
        cp_configs = ordered

    groups_query = db.session.query(CheckpointGroup)
    if competition_id is not None:
        groups_query = groups_query.filter(CheckpointGroup.competition_id == competition_id)
    groups = _sort_groups(groups_query.all(), group_order)
    group_team_numbers = {}
    for g in groups:
        nums = (
            db.session.query(Team.number)
            .join(TeamGroup, TeamGroup.team_id == Team.id)
            .filter(TeamGroup.group_id == g.id)
            .filter(Team.number.isnot(None))
            .order_by(Team.number.asc())
            .all()
        )
        group_team_numbers[g.name] = [n[0] for n in nums if n[0] is not None]

    values = []
    for g in groups:
        teams = group_team_numbers.get(g.name, [])
        if not teams:
            continue
        # Only include checkpoint tabs that actually contain this group
        relevant = []
        for cfg in cp_configs:
            grp_names = [grp["name"] for grp in (cfg.config or {}).get("groups", [])]
            grp_norm = [n.lower().strip() for n in grp_names]
            if g.name.lower().strip() in grp_norm:
                relevant.append(cfg)
        if not relevant:
            continue

        header_row = [escape_formula_cell(g.name)] + [escape_formula_cell(cfg.tab_name) for cfg in relevant]
        values.append(header_row)
        for team_num in teams:
            row_idx = len(values) + 1
            row = [team_num]
            for cfg in relevant:
                cols = _group_start_cols_from_config(cfg.config or {})
                grp_names = [grp["name"] for grp in (cfg.config or {}).get("groups", [])]
                try:
                    idx = [n.lower().strip() for n in grp_names].index(g.name.lower().strip())
                    start_col = cols[idx]
                    from gspread.utils import rowcol_to_a1

                    team_col_letter = rowcol_to_a1(1, start_col).rstrip("1")
                    time_enabled = bool((cfg.config or {}).get("time_enabled"))
                    dead_time_enabled = bool((cfg.config or {}).get("dead_time_enabled"))
                    time_col = None
                    if time_enabled:
                        time_col = start_col + 1 + (1 if dead_time_enabled else 0)
                    if time_col:
                        time_col_letter = rowcol_to_a1(1, time_col).rstrip("1")
                        formula = (
                            f"=IFERROR("
                            f"INDEX('{cfg.tab_name}'!{time_col_letter}:{time_col_letter}; "
                            f"MATCH(A{row_idx}; '{cfg.tab_name}'!{team_col_letter}:{team_col_letter}; 0)"
                            f'); "")'
                        )
                    else:
                        formula = '""'
                except Exception:
                    formula = '""'
                row.append(formula)
            values.append(row)
        values.append([])
        values.append([])

    # Guard the silent-success path: if every group was skipped (no teams
    # with numbers, or no SheetConfig.config "groups" entry whose name
    # matches a current CheckpointGroup name), values is empty. The Sheets
    # API treats an empty values write as a no-op, so the worksheet would
    # get cleared and refilled with nothing while the caller still flashes
    # success. Surface a real warning instead, and don't touch the tab.
    if not any(values):
        return (
            "No arrivals data to write. Check that groups have teams with "
            "numbers assigned and that the checkpoint tab configs reference "
            "the current group names."
        )

    client = get_sheets_client(current_app)
    ss = client._call(client.gc.open_by_key, spreadsheet_id)
    try:
        ws = client._call(ss.worksheet, tab_name)
        client._call(ws.clear)
    except Exception:
        ws = client._call(ss.add_worksheet, title=tab_name, rows=500, cols=100)
    client._call(
        ws.update,
        range_name="A1",
        values=values,
        value_input_option="USER_ENTERED",
    )
    # Apply conditional formatting: non-empty (arrived) -> green, empty (not arrived) -> red
    try:
        last_row = len(values)
        last_col = max((len(r) for r in values), default=1)
        requests = [
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [
                            {
                                "sheetId": ws.id,
                                "startRowIndex": 1,
                                "endRowIndex": last_row,
                                "startColumnIndex": 1,
                                "endColumnIndex": last_col,
                            }
                        ],
                        "booleanRule": {
                            "condition": {
                                "type": "CUSTOM_FORMULA",
                                "values": [{"userEnteredValue": "=NOT(ISBLANK(B2))"}],
                            },
                            "format": {"backgroundColor": {"red": 0.8, "green": 1, "blue": 0.8}},
                        },
                    },
                    "index": 0,
                }
            },
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [
                            {
                                "sheetId": ws.id,
                                "startRowIndex": 1,
                                "endRowIndex": last_row,
                                "startColumnIndex": 1,
                                "endColumnIndex": last_col,
                            }
                        ],
                        "booleanRule": {
                            "condition": {
                                "type": "CUSTOM_FORMULA",
                                "values": [{"userEnteredValue": "=ISBLANK(B2)"}],
                            },
                            "format": {"backgroundColor": {"red": 1, "green": 0.8, "blue": 0.8}},
                        },
                    },
                    "index": 1,
                }
            },
        ]
        client._call(ss.batch_update, {"requests": requests})
    except Exception as exc:
        current_app.logger.warning("Could not apply arrivals conditional formatting: %s", exc)
    return None


def build_teams_tab(
    spreadsheet_id: str,
    tab_name: str = "Ekipe",
    headers: list[str] | None = None,
    group_order_override: list[str] | None = None,
    competition_id: int | None = None,
):
    if not sheets_sync_enabled():
        return "Sheets sync is disabled."
    lang = load_lang()
    group_order = group_order_override or _get_default_group_order(spreadsheet_id, competition_id)
    groups_query = db.session.query(CheckpointGroup)
    if competition_id is not None:
        groups_query = groups_query.filter(CheckpointGroup.competition_id == competition_id)
    groups = _sort_groups(groups_query.all(), group_order)
    group_blocks = []
    max_rows = 0
    for g in groups:
        teams = (
            Team.query.join(TeamGroup, TeamGroup.team_id == Team.id)
            .filter(TeamGroup.group_id == g.id)
            .order_by(Team.number.asc().nulls_last(), Team.name.asc())
            .all()
        )
        rows = []
        for t in teams:
            rows.append([t.number or "", t.name, t.organization or "", ""])  # last col reserved for points
        max_rows = max(max_rows, len(rows))
        group_blocks.append({"name": g.name, "rows": rows})

    # Build grid horizontally: each group = headers
    if not headers:
        headers = [
            lang.get("teams_number_header", "Številka"),
            lang.get("teams_name_header", "Ime ekipe"),
            lang.get("teams_org_header", "Rod/Org"),
            lang.get("teams_points_header", "Skupne točke"),
        ]
    col_count = len(headers)
    header = []
    subheader = []
    for block in group_blocks:
        header.extend([escape_formula_cell(block["name"])] + [""] * (col_count - 1))
        subheader.extend(headers)

    values = [header, subheader]
    for i in range(max_rows):
        row = []
        for block in group_blocks:
            if i < len(block["rows"]):
                # pad to col_count
                safe_row = [escape_formula_cell(v) if isinstance(v, str) else v for v in block["rows"][i]]
                row.extend(safe_row + [""] * (col_count - len(block["rows"][i])))
            else:
                row.extend([""] * col_count)
        values.append(row)

    # Same guard as build_arrivals_tab: when no group has rows the only
    # content is empty header/subheader rows. Return a warning so the
    # caller doesn't flash a misleading success.
    if not group_blocks or not any(values):
        return (
            "No team data to write. Check that the competition has groups "
            "with teams assigned."
        )

    client = get_sheets_client(current_app)
    ss = client._call(client.gc.open_by_key, spreadsheet_id)
    try:
        ws = client._call(ss.worksheet, tab_name)
        client._call(ws.clear)
    except Exception:
        ws = client._call(ss.add_worksheet, title=tab_name, rows=500, cols=20)
    client._call(
        ws.update,
        range_name="A1",
        values=values,
        value_input_option="USER_ENTERED",
    )


def build_score_tab(
    spreadsheet_id: str,
    tab_name: str = "Skupni seštevek",
    include_dead_time_sum: bool = True,
    group_order_override: list[str] | None = None,
    checkpoint_order_override: list[str] | None = None,
    per_group_checkpoint_order: dict[str, list[str]] | None = None,
    competition_id: int | None = None,
):
    if not sheets_sync_enabled():
        return "Sheets sync is disabled."
    lang = load_lang()
    group_order = group_order_override or _get_default_group_order(spreadsheet_id, competition_id)
    per_group_cp_order = per_group_checkpoint_order or _get_group_checkpoint_order_from_db()
    cp_configs = (
        SheetConfig.query.filter(SheetConfig.spreadsheet_id == spreadsheet_id)
        .filter(SheetConfig.tab_type == "checkpoint")
        .order_by(SheetConfig.tab_name.asc())
        .all()
    )
    if not cp_configs:
        return "No checkpoint tab configs found."

    groups_query = db.session.query(CheckpointGroup)
    if competition_id is not None:
        groups_query = groups_query.filter(CheckpointGroup.competition_id == competition_id)
    groups = _sort_groups(groups_query.all(), group_order)

    # Phase 2 independence: read GlobalScoreRule per group so we can
    # emit Časovnica (Article 39) and Found-points (Article 38) columns
    # on the Score tab as formulas, not as system-computed values.
    # Resolve start/end checkpoint names from the local Checkpoint
    # rows; without those, no time-race formula is possible.
    from app.models import Checkpoint as _CP
    from app.models import GlobalScoreRule

    global_rules_by_group: dict[int, dict] = {}
    if competition_id is not None:
        for gr in GlobalScoreRule.query.filter_by(competition_id=competition_id).all():
            global_rules_by_group[gr.group_id] = gr.rules or {}
    cp_id_to_name = {
        c.id: c.name
        for c in _CP.query.filter(_CP.competition_id == competition_id).all()
    } if competition_id is not None else {}

    values = []
    blocks = []  # track ranges for org summary

    for g in groups:
        teams = (
            Team.query.join(TeamGroup, TeamGroup.team_id == Team.id)
            .filter(TeamGroup.group_id == g.id)
            .order_by(Team.number.asc().nulls_last(), Team.name.asc())
            .all()
        )
        if not teams:
            continue

        # Only include checkpoint tabs that actually contain this group, honoring per-group order if provided
        candidate_cfgs = cp_configs
        if per_group_cp_order and g.name in per_group_cp_order:
            ordered_names = per_group_cp_order.get(g.name, [])
            name_to_cfg = {cfg.tab_name: cfg for cfg in candidate_cfgs}
            ordered_cfgs = []
            for nm in ordered_names:
                cfg = name_to_cfg.get(nm)
                if cfg:
                    ordered_cfgs.append(cfg)
            # append remaining relevant
            for cfg in candidate_cfgs:
                if cfg in ordered_cfgs:
                    continue
                grp_names = [grp["name"] for grp in (cfg.config or {}).get("groups", [])]
                if g.name.lower().strip() in [n.lower().strip() for n in grp_names]:
                    ordered_cfgs.append(cfg)
            candidate_cfgs = ordered_cfgs

        # per-group checkpoint order
        ordered_cfgs_for_group = []
        if per_group_cp_order and g.name in per_group_cp_order:
            order_names = per_group_cp_order.get(g.name, [])
            name_to_cfg = {cfg.tab_name: cfg for cfg in candidate_cfgs}
            for nm in order_names:
                cfg = name_to_cfg.get(nm)
                if cfg and cfg not in ordered_cfgs_for_group:
                    # ensure group present in cfg
                    grp_names = [grp["name"] for grp in (cfg.config or {}).get("groups", [])]
                    if g.name.lower().strip() in [n.lower().strip() for n in grp_names]:
                        ordered_cfgs_for_group.append(cfg)
            # append remaining relevant
            for cfg in candidate_cfgs:
                if cfg in ordered_cfgs_for_group:
                    continue
                grp_names = [grp["name"] for grp in (cfg.config or {}).get("groups", [])]
                if g.name.lower().strip() in [n.lower().strip() for n in grp_names]:
                    ordered_cfgs_for_group.append(cfg)
            relevant = ordered_cfgs_for_group
        else:
            relevant = []
            for cfg in candidate_cfgs:
                grp_names = [grp["name"] for grp in (cfg.config or {}).get("groups", [])]
                grp_norm = [n.lower().strip() for n in grp_names]
                if g.name.lower().strip() in grp_norm:
                    relevant.append(cfg)
        if not relevant:
            continue

        # Build header respecting group order: first global group order, then relevant checkpoint tabs in order
        header = [
            lang.get("score_group_header", "Skupina"),
            lang.get("score_number_header", "Številka"),
            lang.get("score_team_header", "Ime ekipe"),
            lang.get("score_org_header", "Rod/Org"),
        ]
        header.extend([escape_formula_cell(cfg.tab_name) for cfg in relevant])
        if include_dead_time_sum:
            header.append(lang.get("score_dead_time_sum_header", "Mrtvi čas (sum)"))
        # Phase 2: dedicated columns for the two global contributions
        # so the spreadsheet can sum the final score on its own.
        header.append(lang.get("score_casovnica_header", "Časovnica"))
        header.append(lang.get("score_found_header", "Najdene KT"))
        header.append(lang.get("score_total_header", "Skupaj točke"))
        start_row = len(values) + 1  # header row index (1-based)
        values.append(header)

        # For each team, compute row with same column positions
        for t in teams:
            row_idx = len(values) + 1
            row = [
                escape_formula_cell(g.name),
                t.number or "",
                escape_formula_cell(t.name),
                escape_formula_cell(t.organization or ""),
            ]
            cp_formulas = []
            dead_time_formulas = []
            for cfg in relevant:
                cols = _group_start_cols_from_config(cfg.config or {})
                grp_names = [grp["name"] for grp in (cfg.config or {}).get("groups", [])]
                try:
                    idx = [n.lower().strip() for n in grp_names].index(g.name.lower().strip())
                    start_col = cols[idx]
                    fields_len = len((cfg.config or {}).get("groups", [])[idx].get("fields", []))
                    dead_time = 1 if (cfg.config or {}).get("dead_time_enabled") else 0
                    time_enabled = 1 if (cfg.config or {}).get("time_enabled") else 0
                    points_col = start_col + 1 + time_enabled + dead_time + fields_len  # time? + dead + fields + points
                    dead_time_col = None
                    if dead_time:
                        dead_time_col = start_col + 1
                    from gspread.utils import rowcol_to_a1

                    team_col_letter = rowcol_to_a1(1, start_col).rstrip("1")
                    points_col_letter = rowcol_to_a1(1, points_col).rstrip("1")
                    formula = (
                        f"=IFERROR("
                        f"INDEX('{cfg.tab_name}'!{points_col_letter}:{points_col_letter}; "
                        f"MATCH(B{row_idx}; '{cfg.tab_name}'!{team_col_letter}:{team_col_letter}; 0)"
                        f"); 0)"
                    )
                    if dead_time_col:
                        dead_time_col_letter = rowcol_to_a1(1, dead_time_col).rstrip("1")
                        dt_formula = (
                            f"=IFERROR("
                            f"INDEX('{cfg.tab_name}'!{dead_time_col_letter}:{dead_time_col_letter}; "
                            f"MATCH(B{row_idx}; '{cfg.tab_name}'!{team_col_letter}:{team_col_letter}; 0)"
                            f"); 0)"
                        )
                    else:
                        dt_formula = "=0"
                except Exception:
                    formula = "=0"
                    dt_formula = "=0"
                cp_formulas.append(formula)
                dead_time_formulas.append(dt_formula)
            # Assemble row with consistent columns
            row.extend(cp_formulas)

            def _strip_eq(expr: str) -> str:
                return expr[1:] if expr.startswith("=") else expr

            dead_time_sum_expr = "0"
            if include_dead_time_sum:
                if dead_time_formulas and any(f not in ("=0", "0") for f in dead_time_formulas):
                    dt_total_formula = f"=SUM({';'.join(_strip_eq(f) for f in dead_time_formulas)})"
                    dead_time_sum_expr = (
                        f"SUM({';'.join(_strip_eq(f) for f in dead_time_formulas)})"
                    )
                else:
                    dt_total_formula = "=0"
                row.append(dt_total_formula)

            # Phase 2: Časovnica + Found formulas, derived from the
            # per-CP tabs' Time columns. Each is computed entirely from
            # cells on this spreadsheet so the system can be offline
            # and the Score tab still produces the correct final total.
            cas_formula, found_formula = _build_global_rule_formulas(
                group=g,
                global_rule=global_rules_by_group.get(g.id) or {},
                cp_id_to_name=cp_id_to_name,
                relevant_cfgs=relevant,
                row_idx=row_idx,
                dead_time_sum_expr=dead_time_sum_expr,
            )
            row.append(cas_formula)
            row.append(found_formula)

            # Total is now the sum of per-CP points + časovnica + found,
            # all computed from cells on the spreadsheet.
            total_pieces = [_strip_eq(f) for f in cp_formulas]
            total_pieces.append(_strip_eq(cas_formula))
            total_pieces.append(_strip_eq(found_formula))
            if total_pieces:
                pts_total_formula = f"=SUM({';'.join(total_pieces)})"
            else:
                pts_total_formula = "=0"
            row.append(pts_total_formula)
            values.append(row)
        # record block range for org summary (data rows only)
        data_start = start_row + 1
        data_end = start_row + len(teams)
        total_col_idx = len(header)
        blocks.append(
            {
                "num_col": 2,
                "name_col": 3,
                "org_col": 4,
                "total_col": total_col_idx,
                "start_row": data_start,
                "end_row": data_end,
            }
        )
        values.append([])

    # Organization summary at bottom
    orgs = (
        db.session.query(Team.organization)
        .filter(Team.organization.isnot(None))
        .filter(func.trim(Team.organization) != "")
        .distinct()
        .order_by(Team.organization.asc())
        .all()
    )
    org_names = [o[0] for o in orgs if o[0]]
    if org_names:
        values.append([])
        values.append(
            [
                lang.get("score_org_section_header", "Organizacija"),
                lang.get("score_org_teams_header", "Ekipe"),
                lang.get("score_org_numbers_header", "Številke"),
                lang.get("score_org_count_header", "Št ekip"),
                lang.get("score_org_total_header", "Skupaj točke (org)"),
            ]
        )
        for org in org_names:
            row_idx = len(values) + 1

            def col_letter(idx: int) -> str:
                from gspread.utils import rowcol_to_a1

                return rowcol_to_a1(1, idx).rstrip("1")

            name_filters = []
            num_filters = []
            total_filters = []
            for b in blocks:
                if b["end_row"] < b["start_row"]:
                    continue
                ncol = col_letter(b["name_col"])
                ocol = col_letter(b["org_col"])
                tcol = col_letter(b["num_col"])
                pcol = col_letter(b["total_col"])
                name_filters.append(
                    f"FILTER({ncol}{b['start_row']}:{ncol}{b['end_row']}; {ocol}{b['start_row']}:{ocol}{b['end_row']}=A{row_idx})"  # noqa: E501
                )
                num_filters.append(
                    f"FILTER({tcol}{b['start_row']}:{tcol}{b['end_row']}; {ocol}{b['start_row']}:{ocol}{b['end_row']}=A{row_idx})"  # noqa: E501
                )
                total_filters.append(
                    f"FILTER({pcol}{b['start_row']}:{pcol}{b['end_row']}; {ocol}{b['start_row']}:{ocol}{b['end_row']}=A{row_idx})"  # noqa: E501
                )
            # assemble base arrays
            names_raw = "{" + "; ".join(name_filters) + "}"
            nums_raw = "{" + "; ".join(num_filters) + "}"
            totals_raw = "{" + "; ".join(total_filters) + "}"
            # wrap with IFERROR to avoid #N/A when filters have no matches
            names_expr = f'IFERROR({names_raw}; "")'
            nums_expr = f'IFERROR({nums_raw}; "")'
            totals_expr = f"IFERROR({totals_raw}; 0)"
            count_expr = f"=SUMPRODUCT(N(LEN({nums_expr})>0))"
            org_row = [
                escape_formula_cell(org),
                f'=TEXTJOIN(", "; TRUE; {names_expr})',
                f'=TEXTJOIN(", "; TRUE; {nums_expr})',
                count_expr,
                f"=SUM({totals_expr})",
            ]
            values.append(org_row)

    # Same guard as build_arrivals_tab: empty values means no group had
    # teams and no checkpoint config matched any current group name. The
    # write would silently no-op and the caller would flash success.
    if not any(values):
        return (
            "No score data to write. Check that groups have teams assigned "
            "and that checkpoint tab configs reference the current group names."
        )

    client = get_sheets_client(current_app)
    ss = client._call(client.gc.open_by_key, spreadsheet_id)
    try:
        ws = client._call(ss.worksheet, tab_name)
        client._call(ws.clear)
    except Exception:
        ws = client._call(ss.add_worksheet, title=tab_name, rows=800, cols=50)
    client._call(
        ws.update,
        range_name="A1",
        values=values,
        value_input_option="USER_ENTERED",
    )


def wizard_build_checkpoint_tabs(
    spreadsheet_id: str,
    arrived_header: str,
    points_header: str,
    dead_time_header: str,
    time_header: str,
    group_order: list[str] | None,
    competition_id: int | None = None,
    per_checkpoint_extra_fields: dict[int, list[str]] | None = None,
    per_checkpoint_dead_time: dict[int, bool] | None = None,
    per_checkpoint_groups: dict[int, list[int]] | None = None,
    per_checkpoint_tabnames: dict[int, str] | None = None,
    create_only: set[int] | None = None,
    checkpoint_order_override: list[str] | None = None,
    per_group_checkpoint_order: dict[str, list[str]] | None = None,
    record_time_cp: set[int] | None = None,
    pause_every: int | None = None,
    pause_seconds: int = 65,
):
    """Create checkpoint tabs for all checkpoints with groups ordered by group_order."""
    if not group_order:
        group_order = _get_default_group_order(spreadsheet_id, competition_id)
    checkpoints_query = Checkpoint.query
    if competition_id is not None:
        checkpoints_query = checkpoints_query.filter(Checkpoint.competition_id == competition_id)
    checkpoints = checkpoints_query.order_by(Checkpoint.name.asc()).all()
    if not checkpoints:
        return 0, 0

    group_order_norm = [g.lower().strip() for g in group_order]

    client = get_sheets_client(current_app)

    created = 0
    skipped = 0

    for idx, cp in enumerate(checkpoints, start=1):
        if create_only is not None and cp.id not in create_only:
            skipped += 1
            continue

        tab_title = per_checkpoint_tabnames.get(cp.id) if per_checkpoint_tabnames else None
        if not tab_title:
            tab_title = cp.name

        # Skip if already configured
        existing = SheetConfig.query.filter_by(spreadsheet_id=spreadsheet_id, tab_name=tab_title).first()
        if existing:
            skipped += 1
            continue

        # Groups attached to this checkpoint, ordered by group_order then name
        if per_checkpoint_groups and cp.id in per_checkpoint_groups:
            raw_groups = CheckpointGroup.query.filter(CheckpointGroup.id.in_(per_checkpoint_groups[cp.id])).all()
        else:
            raw_groups = cp.groups or []

        def _sort_key(g):
            norm = g.name.lower().strip()
            return (group_order_norm.index(norm) if norm in group_order_norm else len(group_order_norm), g.name)

        ordered_groups = sorted(raw_groups, key=_sort_key)
        extra_fields = per_checkpoint_extra_fields.get(cp.id, []) if per_checkpoint_extra_fields else []
        time_enabled = bool(record_time_cp and cp.id in record_time_cp)
        groups_def = [{"group_id": g.id, "name": g.name, "fields": list(extra_fields)} for g in ordered_groups]
        dead_time_enabled = per_checkpoint_dead_time.get(cp.id, True) if per_checkpoint_dead_time else True
        if not groups_def:
            continue

        # Build headers
        headers = []
        group_start_cols = []
        current_col = 1
        for grp in groups_def:
            group_start_cols.append(current_col)
            headers.append(grp["name"])
            if dead_time_enabled:
                headers.append(dead_time_header)
            if time_enabled:
                headers.append(time_header)
            headers.extend(grp.get("fields", []))
            headers.append(points_header)
            current_col += (
                1 + (1 if dead_time_enabled else 0) + (1 if time_enabled else 0) + len(grp.get("fields", [])) + 1
            )

        ws = client.add_tab(spreadsheet_id, tab_title)
        client.set_header_row(spreadsheet_id, tab_title, headers)

        for grp, start_col in zip(groups_def, group_start_cols, strict=False):
            db_group = db.session.get(CheckpointGroup, grp.get("group_id"))
            if not db_group:
                continue
            if competition_id is not None and db_group.competition_id != competition_id:
                continue
            if not db_group:
                continue
            nums_q = (
                db.session.query(Team.id, Team.number, Team.name)
                .join(TeamGroup, TeamGroup.team_id == Team.id)
                .filter(TeamGroup.group_id == db_group.id)
            )
            if competition_id is not None:
                nums_q = nums_q.filter(Team.competition_id == competition_id)
            nums = nums_q.order_by(Team.number.asc().nulls_last(), Team.name.asc()).all()
            values = [n[1] if n[1] is not None else (n[2] or "") for n in nums]
            if values:
                client.update_column(spreadsheet_id, tab_title, start_col, 2, values)

        record = SheetConfig(
            competition_id=competition_id or cp.competition_id,
            spreadsheet_id=spreadsheet_id,
            spreadsheet_name=ws.spreadsheet.title,
            tab_name=tab_title,
            tab_type="checkpoint",
            checkpoint_id=cp.id,
            config={
                "arrived_header": arrived_header,
                "dead_time_enabled": dead_time_enabled,
                "dead_time_header": dead_time_header,
                "time_enabled": time_enabled,
                "time_header": time_header,
                "points_header": points_header,
                "groups": groups_def,
                "checkpoint_order": checkpoint_order_override,
                "per_group_checkpoint_order": per_group_checkpoint_order,
            },
        )
        db.session.add(record)
        db.session.flush()
        created += 1

        if pause_every and idx % pause_every == 0:
            time.sleep(pause_seconds)

    db.session.commit()
    return created, skipped


def wizard_create_checkpoint_configs(
    spreadsheet_id: str,
    spreadsheet_name: str,
    arrived_header: str,
    points_header: str,
    dead_time_header: str,
    time_header: str,
    group_order: list[str] | None,
    competition_id: int | None = None,
    per_checkpoint_extra_fields: dict[int, list[str]] | None = None,
    per_checkpoint_dead_time: dict[int, bool] | None = None,
    per_checkpoint_groups: dict[int, list[int]] | None = None,
    per_checkpoint_tabnames: dict[int, str] | None = None,
    create_only: set[int] | None = None,
    checkpoint_order_override: list[str] | None = None,
    per_group_checkpoint_order: dict[str, list[str]] | None = None,
    record_time_cp: set[int] | None = None,
):
    """Create checkpoint tab configs locally without contacting Google Sheets."""
    if not group_order:
        group_order = _get_default_group_order(spreadsheet_id, competition_id)
    checkpoints_query = Checkpoint.query
    if competition_id is not None:
        checkpoints_query = checkpoints_query.filter(Checkpoint.competition_id == competition_id)
    checkpoints = checkpoints_query.order_by(Checkpoint.name.asc()).all()
    if not checkpoints:
        return 0, 0

    group_order_norm = [g.lower().strip() for g in group_order]
    created = 0
    skipped = 0

    for cp in checkpoints:
        if create_only is not None and cp.id not in create_only:
            skipped += 1
            continue

        tab_title = per_checkpoint_tabnames.get(cp.id) if per_checkpoint_tabnames else None
        if not tab_title:
            tab_title = cp.name

        existing = SheetConfig.query.filter_by(spreadsheet_id=spreadsheet_id, tab_name=tab_title).first()
        if existing:
            skipped += 1
            continue

        if per_checkpoint_groups and cp.id in per_checkpoint_groups:
            raw_groups = CheckpointGroup.query.filter(CheckpointGroup.id.in_(per_checkpoint_groups[cp.id])).all()
        else:
            raw_groups = cp.groups or []

        def _sort_key(g):
            norm = g.name.lower().strip()
            return (group_order_norm.index(norm) if norm in group_order_norm else len(group_order_norm), g.name)

        ordered_groups = sorted(raw_groups, key=_sort_key)
        extra_fields = per_checkpoint_extra_fields.get(cp.id, []) if per_checkpoint_extra_fields else []
        time_enabled = bool(record_time_cp and cp.id in record_time_cp)
        groups_def = [{"group_id": g.id, "name": g.name, "fields": list(extra_fields)} for g in ordered_groups]
        dead_time_enabled = per_checkpoint_dead_time.get(cp.id, True) if per_checkpoint_dead_time else True
        if not groups_def:
            continue

        record = SheetConfig(
            competition_id=competition_id or cp.competition_id,
            spreadsheet_id=spreadsheet_id,
            spreadsheet_name=spreadsheet_name,
            tab_name=tab_title,
            tab_type="checkpoint",
            checkpoint_id=cp.id,
            config={
                "arrived_header": arrived_header,
                "dead_time_enabled": dead_time_enabled,
                "dead_time_header": dead_time_header,
                "time_enabled": time_enabled,
                "time_header": time_header,
                "points_header": points_header,
                "groups": groups_def,
                "checkpoint_order": checkpoint_order_override,
                "per_group_checkpoint_order": per_group_checkpoint_order,
            },
        )
        db.session.add(record)
        db.session.flush()
        created += 1

    db.session.commit()
    return created, skipped


# ---------------------------------------------------------------------------
# Publish local-only configs to a real Google Sheet
# ---------------------------------------------------------------------------


def _fmt_num(value) -> str:
    """Format a number for inclusion in a Sheets formula. Integer values
    render without a trailing .0 to keep formulas readable."""
    try:
        n = float(value)
    except (TypeError, ValueError):
        return "0"
    if n == int(n):
        return str(int(n))
    return repr(n)


def _field_rule_to_formula(rule, cell_ref: str) -> str | None:
    """Translate one ScoreRule field rule into a Sheets formula expression
    (no leading '=') that reads `cell_ref` and produces the field's
    point contribution.

    Returns None when the rule cannot be expressed as a static formula
    (time_race, found across other tabs, interpolate — handled
    elsewhere or system-dependent). Callers fall back to a system-
    written raw value for those.
    """
    if rule is None:
        # No rule means raw passthrough; Sheets treats empty cells as 0
        # in arithmetic.
        return cell_ref
    if isinstance(rule, list):
        # Rule chains aren't used in this race; if one shows up, just
        # apply the first rule and ignore the rest (matches the app's
        # _apply_field_rule which would compose but we don't need that).
        if not rule:
            return cell_ref
        return _field_rule_to_formula(rule[0], cell_ref)
    if not isinstance(rule, dict) or not rule:
        return cell_ref

    rule_type = (rule.get("type") or "").lower()

    if rule_type == "multiplier":
        return f"({cell_ref}*{_fmt_num(rule.get('factor'))})"

    if rule_type == "mapping":
        m = rule.get("map") or {}
        if not m:
            return "0"
        # Build IFS(cell=k1; v1; cell=k2; v2; ...; TRUE; 0). Sort by key
        # so the output is stable across publishes. Quote non-numeric
        # keys so the formula compares strings correctly.
        parts: list[tuple[str, str]] = []
        for k, v in m.items():
            try:
                key_repr = _fmt_num(float(k))
            except (TypeError, ValueError):
                key_repr = f'"{str(k)}"'
            parts.append((key_repr, _fmt_num(v)))
        parts.sort(key=lambda p: p[0])
        ifs_args: list[str] = []
        for key_repr, val_num in parts:
            ifs_args.append(f"{cell_ref}={key_repr}")
            ifs_args.append(val_num)
        ifs_args.append("TRUE")
        ifs_args.append("0")
        return f"IFS({'; '.join(ifs_args)})"

    if rule_type == "deviation":
        target = _fmt_num(rule.get("target"))
        max_p = _fmt_num(rule.get("max_points"))
        penalty_p = _fmt_num(rule.get("penalty_points"))
        # Guard against a zero penalty_distance which would divide-by-zero.
        try:
            pd_num = float(rule.get("penalty_distance") or 0)
        except (TypeError, ValueError):
            pd_num = 0
        if pd_num == 0:
            return _fmt_num(rule.get("min_points") or 0)
        penalty_d = _fmt_num(pd_num)
        min_p = _fmt_num(rule.get("min_points") or 0)
        # Empty cell -> deviation would compute |0-target| which is
        # spurious; explicitly return 0 for empty inputs.
        deviation_expr = (
            f"MAX({max_p}-ABS({cell_ref}-{target})/{penalty_d}*{penalty_p}; {min_p})"
        )
        return f'IF({cell_ref}=""; 0; {deviation_expr})'

    # Unknown / unsupported rule type (interpolate, found, time_race
    # when nested inside field_rules) — bail out so the caller falls
    # back to system-written raw values.
    return None


def _points_formula_from_rule(
    rule_blob: dict | None,
    field_columns: dict[str, int],
    row: int,
) -> str | None:
    """Compose the Points-cell formula for one (CP, group) from a
    ScoreRule blob.

    `field_columns` maps each field name to its 1-based column index
    inside the group block; `row` is the spreadsheet row of the team.

    Returns the cell formula (with leading '=') or None if the rule
    isn't expressible as a formula (e.g., the rule is time_race-only,
    or any single field rule comes back unsupported).
    """
    if not rule_blob:
        return None
    # time_race owns the whole Points cell; the system writes it and
    # nothing on the sheet can recompute it without RANK/LET helpers.
    if rule_blob.get("time_race"):
        return None

    field_rules = rule_blob.get("field_rules") or {}
    total_fields = rule_blob.get("total_fields") or list(field_rules.keys())
    # Skip dead_time from the total — matches _compute_total's behavior
    # which excludes "dead_time" from total_fields.
    total_fields = [f for f in total_fields if f != "dead_time"]
    if not total_fields:
        return None

    from gspread.utils import rowcol_to_a1

    pieces: list[str] = []
    for f in total_fields:
        col = field_columns.get(f)
        if col is None:
            continue
        cell_ref = rowcol_to_a1(row, col)
        rule = field_rules.get(f)
        piece = _field_rule_to_formula(rule, cell_ref)
        if piece is None:
            # One unsupported rule type drops the whole formula — falls
            # back to system-written value at this Points cell.
            return None
        pieces.append(piece)
    if not pieces:
        return None
    return "=" + "+".join(pieces)


def _build_local_cp_grid(
    cfg: SheetConfig, competition_id: int
) -> tuple[list[list], dict[int, bool]]:
    """Build the full per-checkpoint grid (headers + all team rows) from a
    SheetConfig, including any existing ScoreEntry data and check-in
    timestamps. Returned as a 2D list suitable for a single
    ws.update("A1", values, value_input_option="USER_ENTERED") call,
    along with a per-group dict marking which groups now have a
    Points cell that's a Sheets *formula* (so the system knows not to
    clobber it on subsequent score writes).

    Writing the entire tab in one batched update is intentional: each CP
    costs ~2 Sheets API calls (add_tab + update) instead of the
    per-column wizard pattern that costs 1 per group, so a 14-CP publish
    stays comfortably under the 40-calls-per-60s quota.
    """
    from app.models import ScoreRule

    cfg_blob = cfg.config or {}
    groups_def = cfg_blob.get("groups") or []
    dead_time_enabled = bool(cfg_blob.get("dead_time_enabled"))
    time_enabled = bool(cfg_blob.get("time_enabled"))
    dead_time_header = cfg_blob.get("dead_time_header") or "Dead Time"
    time_header = cfg_blob.get("time_header") or "Time"
    points_header = cfg_blob.get("points_header") or "Points"

    headers: list[str] = []
    group_specs: list[dict] = []
    current_col = 1
    for grp in groups_def:
        block_start = current_col
        headers.append(grp.get("name") or "")
        if dead_time_enabled:
            headers.append(dead_time_header)
        if time_enabled:
            headers.append(time_header)
        fields = list(grp.get("fields") or [])
        headers.extend(fields)
        headers.append(points_header)
        block_width = 1 + (1 if dead_time_enabled else 0) + (1 if time_enabled else 0) + len(fields) + 1
        group_specs.append(
            {
                "group_id": grp.get("group_id"),
                "fields": fields,
                "start_col": block_start,
                "width": block_width,
            }
        )
        current_col += block_width

    total_cols = current_col - 1
    if total_cols <= 0:
        return [headers], {}

    # Per group: ordered team list + their latest score + check-in timestamp
    cp_id = cfg.checkpoint_id
    teams_per_group: list[list[dict]] = []
    max_rows = 0
    for spec in group_specs:
        gid = spec["group_id"]
        if not gid:
            teams_per_group.append([])
            continue
        teams = (
            db.session.query(Team.id, Team.number, Team.name)
            .join(TeamGroup, TeamGroup.team_id == Team.id)
            .filter(TeamGroup.group_id == gid)
            .filter(Team.competition_id == competition_id)
            .order_by(Team.number.asc().nulls_last(), Team.name.asc())
            .all()
        )
        rows: list[dict] = []
        for t_id, t_num, t_name in teams:
            score = None
            checkin_ts = None
            if cp_id is not None:
                score = (
                    ScoreEntry.query.filter_by(
                        competition_id=competition_id, team_id=t_id, checkpoint_id=cp_id
                    )
                    .order_by(ScoreEntry.created_at.desc())
                    .first()
                )
                ci = Checkin.query.filter_by(
                    competition_id=competition_id, team_id=t_id, checkpoint_id=cp_id
                ).first()
                if ci and ci.timestamp:
                    checkin_ts = ci.timestamp
            rows.append(
                {
                    "team_label": t_num if t_num is not None else (t_name or ""),
                    "score": score,
                    "checkin_ts": checkin_ts,
                }
            )
        teams_per_group.append(rows)
        max_rows = max(max_rows, len(rows))

    # Precompute per-group field-column maps and the ScoreRule blob so
    # we can emit a Points formula per team row when the rule is
    # expressible. group_has_formula tells the caller which (CP, group)
    # pairs become formula-driven; the per-CP SheetConfig stores this
    # as a flag so future score writes skip the Points cell.
    rule_by_group: dict[int, dict] = {}
    field_cols_by_group: dict[int, dict[str, int]] = {}
    group_has_formula: dict[int, bool] = {}
    points_col_by_group: dict[int, int] = {}
    if cp_id is not None:
        for spec in group_specs:
            gid = spec["group_id"]
            if not gid:
                continue
            field_cols: dict[str, int] = {}
            col_cursor = spec["start_col"]
            col_cursor += 1  # group name column
            if dead_time_enabled:
                field_cols["dead_time"] = col_cursor
                col_cursor += 1
            if time_enabled:
                col_cursor += 1  # time column (no field rule applies)
            for f in spec["fields"]:
                field_cols[f] = col_cursor
                col_cursor += 1
            points_col_by_group[gid] = col_cursor  # 1-based column of Points
            field_cols_by_group[gid] = field_cols
            rule = (
                ScoreRule.query.filter_by(
                    competition_id=competition_id, checkpoint_id=cp_id, group_id=gid
                )
                .order_by(ScoreRule.created_at.desc())
                .first()
            )
            if rule is not None:
                rule_by_group[gid] = rule.rules or {}

    # Header row (escape any user-supplied strings)
    values: list[list] = [[escape_formula_cell(h) if isinstance(h, str) else h for h in headers]]

    for i in range(max_rows):
        row: list = [""] * total_cols
        for spec, group_rows in zip(group_specs, teams_per_group, strict=False):
            if i >= len(group_rows):
                continue
            t = group_rows[i]
            cursor = spec["start_col"] - 1  # 0-indexed
            label = t["team_label"]
            row[cursor] = label if not isinstance(label, str) else escape_formula_cell(label)
            cursor += 1
            if dead_time_enabled:
                if t["score"] is not None:
                    dt = (t["score"].raw_fields or {}).get("dead_time")
                    if dt is not None and dt != "":
                        row[cursor] = dt if not isinstance(dt, str) else escape_formula_cell(dt)
                cursor += 1
            if time_enabled:
                if t["checkin_ts"] is not None:
                    row[cursor] = t["checkin_ts"].strftime("%Y-%m-%d %H:%M:%S")
                cursor += 1
            for f in spec["fields"]:
                if t["score"] is not None:
                    val = (t["score"].raw_fields or {}).get(f)
                    if val is not None and val != "":
                        row[cursor] = escape_formula_cell(val) if isinstance(val, str) else val
                cursor += 1
            # Points column. Prefer a formula computed from the raw field
            # cells in this row so the spreadsheet stays independent of
            # the system; fall back to the system-written total when the
            # rule isn't expressible (time_race, unknown rule type).
            gid = spec["group_id"]
            sheet_row = i + 2  # row 1 = headers
            points_formula = None
            if gid in rule_by_group:
                points_formula = _points_formula_from_rule(
                    rule_by_group[gid], field_cols_by_group.get(gid, {}), sheet_row
                )
            if points_formula is not None:
                row[cursor] = points_formula
                group_has_formula[gid] = True
            elif t["score"] is not None and t["score"].total is not None:
                row[cursor] = t["score"].total
                # If we have a rule but it's not expressible (time_race),
                # mark this group explicitly as non-formula so callers
                # don't set the points_formula flag.
                group_has_formula.setdefault(gid, False)
            cursor += 1
        values.append(row)

    # For groups that never got a Points formula (because no rule, or
    # rule is time_race / unsupported), leave group_has_formula at False.
    for spec in group_specs:
        gid = spec["group_id"]
        if gid and gid not in group_has_formula:
            group_has_formula[gid] = False

    return values, group_has_formula


def publish_local_configs_to_spreadsheet(
    competition_id: int,
    spreadsheet_id: str,
    *,
    build_summary_tabs: bool = True,
) -> dict:
    """Promote a competition's local-only SheetConfigs to a real Google
    Sheet.

    For each SheetConfig whose spreadsheet_id starts with "local:":
      1. Build the full per-CP grid (headers + team rows + any existing
         ScoreEntry data + check-in timestamps) in memory.
      2. Create (or reuse) the remote tab and write the grid in one
         batched update. Two Sheets API calls per CP.
      3. Rebind SheetConfig.spreadsheet_id from "local:N" to
         spreadsheet_id, committed per-CP so partial progress survives a
         mid-batch failure.

    After the per-CP loop, (re)build the Teams / Arrivals / Score summary
    tabs so the spreadsheet is a self-contained backup: Arrivals and
    Score use formula-based lookups (=INDEX(...;MATCH(...))) against the
    per-CP tabs, so manual edits on the CP tabs propagate without our
    system. Future score submissions also replicate into the per-CP tabs
    via the existing update_checkpoint_scores hook because every
    SheetConfig now points at the real spreadsheet.

    All Google calls route through the throttled SheetsClient (40 calls
    per 60s window, 3 retries on 429), so a publish stays under quota
    even on a large competition. Per-CP failures are caught and recorded
    in `errors` so one bad tab doesn't abort the whole batch.

    Returns: {"published": int, "skipped": int, "errors": [str],
              "summary_tabs": [str], "spreadsheet_name": str}
    """
    result: dict = {
        "published": 0,
        "skipped": 0,
        "errors": [],
        "summary_tabs": [],
        "spreadsheet_name": None,
    }

    if not sheets_sync_enabled():
        result["errors"].append("Sheets sync is disabled.")
        return result

    if not spreadsheet_id or spreadsheet_id.startswith("local:"):
        result["errors"].append("Target spreadsheet_id must be a real Google Sheets ID.")
        return result

    configs = (
        SheetConfig.query.filter(SheetConfig.competition_id == competition_id)
        .filter(SheetConfig.tab_type == "checkpoint")
        .filter(SheetConfig.spreadsheet_id.like("local:%"))
        .order_by(SheetConfig.tab_name.asc())
        .all()
    )
    if not configs:
        result["errors"].append("No local SheetConfigs to publish for this competition.")
        return result

    try:
        client = get_sheets_client(current_app)
    except Exception as exc:
        result["errors"].append(f"Sheets client init: {exc}")
        return result

    try:
        ss = client._call(client.gc.open_by_key, spreadsheet_id)
        spreadsheet_name = getattr(ss, "title", None) or "Google Sheet"
        result["spreadsheet_name"] = spreadsheet_name
    except Exception as exc:
        result["errors"].append(f"Could not open target spreadsheet: {exc}")
        return result

    for cfg in configs:
        try:
            grid, group_has_formula = _build_local_cp_grid(cfg, competition_id)
            if not grid or not grid[0]:
                result["skipped"] += 1
                continue
            tab_name = cfg.tab_name
            n_rows = max(len(grid) + 10, 50)
            n_cols = max(len(grid[0]) + 5, 26)

            # Create or reuse the remote tab. gspread raises an APIError
            # whose message contains "already exists" when the tab name
            # collides; we treat that as "reuse" rather than failure so
            # partial reruns of publish are safe.
            tab_existed = False
            try:
                client.add_tab(spreadsheet_id, tab_name, rows=n_rows, cols=n_cols)
            except Exception as exc:
                msg = str(exc).lower()
                if "already exists" in msg or "duplicate" in msg:
                    tab_existed = True
                else:
                    raise

            ws = client._call(ss.worksheet, tab_name)
            if tab_existed:
                # Clear before re-write so we don't keep stale rows from
                # a previous publish or a manual edit that shifted columns.
                client._call(ws.clear)

            client._call(
                ws.update,
                range_name="A1",
                values=grid,
                value_input_option="USER_ENTERED",
            )

            cfg.spreadsheet_id = spreadsheet_id
            cfg.spreadsheet_name = spreadsheet_name
            # Persist per-group flag so update_checkpoint_scores_sync
            # knows which Points cells are formula-driven and must not
            # be overwritten by future raw writes.
            if group_has_formula:
                new_cfg_blob = dict(cfg.config or {})
                groups_blob = list(new_cfg_blob.get("groups") or [])
                rewritten = []
                for g in groups_blob:
                    if not isinstance(g, dict):
                        rewritten.append(g)
                        continue
                    g2 = dict(g)
                    gid = g2.get("group_id")
                    if gid is not None and group_has_formula.get(gid):
                        g2["points_formula"] = True
                    else:
                        g2.pop("points_formula", None)
                    rewritten.append(g2)
                new_cfg_blob["groups"] = rewritten
                cfg.config = new_cfg_blob
            db.session.commit()
            result["published"] += 1
        except Exception as exc:
            db.session.rollback()
            result["errors"].append(f"{cfg.tab_name}: {exc}")
            try:
                current_app.logger.exception("Publish failed for tab %s", cfg.tab_name)
            except Exception:
                pass

    if build_summary_tabs and result["published"] > 0:
        lang = load_lang()
        for label, fn, tab_label in [
            ("teams", build_teams_tab, lang.get("teams_tab") or "Teams"),
            ("arrivals", build_arrivals_tab, lang.get("arrivals_tab") or "Arrivals"),
            ("score", build_score_tab, lang.get("score_tab") or "Score"),
        ]:
            try:
                err = fn(
                    spreadsheet_id,
                    tab_label,
                    competition_id=competition_id,
                )
                if err:
                    result["errors"].append(f"{label}: {err}")
                else:
                    result["summary_tabs"].append(tab_label)
            except Exception as exc:
                result["errors"].append(f"{label} build raised: {exc}")
                try:
                    current_app.logger.exception("Summary tab %s build failed", label)
                except Exception:
                    pass

    return result
