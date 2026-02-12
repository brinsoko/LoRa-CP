from __future__ import annotations

from typing import List
import time
from datetime import datetime
from app.utils.lang_store import load_lang
import gspread
from gspread.exceptions import APIError

from flask import current_app
from sqlalchemy import func

from app.extensions import db
from app.models import SheetConfig, CheckpointGroup, Team, TeamGroup, Checkpoint, CheckpointGroupLink
from app.utils.competition import get_competition_group_order
from app.utils.sheets_client import SheetsClient
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
        groups = (
            CheckpointGroup.query
            .filter(CheckpointGroup.competition_id == competition_id)
            .all()
        )
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
        SheetConfig.query
        .filter(SheetConfig.spreadsheet_id == spreadsheet_id)
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
    groups = CheckpointGroup.query.options(db.joinedload(CheckpointGroup.checkpoint_links).joinedload(CheckpointGroupLink.checkpoint)).all()
    for g in groups:
        # order by position asc
        ordered = sorted(g.checkpoint_links, key=lambda l: l.position if l.position is not None else 0)
        orders[g.name] = [l.checkpoint.name for l in ordered if l.checkpoint]
    return orders


def _sort_groups(groups: list[CheckpointGroup], order: list[str]) -> list[CheckpointGroup]:
    order_norm = [g.lower().strip() for g in order]
    def key(g: CheckpointGroup):
        norm = g.name.lower().strip()
        return (order_norm.index(norm) if norm in order_norm else len(order_norm), g.name)
    return sorted(groups, key=key)


def _group_start_cols_from_config(cfg: dict) -> List[int]:
    cols = []
    current = 1
    dead_time_enabled = bool(cfg.get("dead_time_enabled"))
    time_enabled = bool(cfg.get("time_enabled"))
    for grp in cfg.get("groups", []):
        cols.append(current)
        current += 1 + (1 if dead_time_enabled else 0) + (1 if time_enabled else 0) + len(grp.get("fields", [])) + 1
    return cols


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

    cfg_app = current_app.config
    try:
        client = SheetsClient(
            service_account_file=cfg_app.get("GOOGLE_SERVICE_ACCOUNT_FILE"),
            service_account_json=cfg_app.get("GOOGLE_SERVICE_ACCOUNT_JSON"),
        )
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

        for grp, start_col in zip(groups, group_cols):
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
    """Set arrived checkbox TRUE for the given team/checkpoint in any linked checkpoint tab."""
    if not sheets_sync_enabled():
        return
    team = Team.query.get(team_id)
    checkpoint = Checkpoint.query.get(checkpoint_id)
    if not team or not checkpoint:
        return

    configs = (
        SheetConfig.query
        .filter(SheetConfig.tab_type == "checkpoint")
        .filter(SheetConfig.checkpoint_id == checkpoint_id)
        .filter(SheetConfig.competition_id == checkpoint.competition_id)
        .all()
    )
    if not configs:
        return

    try:
        client = SheetsClient(
            service_account_file=current_app.config.get("GOOGLE_SERVICE_ACCOUNT_FILE"),
            service_account_json=current_app.config.get("GOOGLE_SERVICE_ACCOUNT_JSON"),
        )
    except Exception as exc:
        current_app.logger.warning("Sheets arrival sync skipped: %s", exc)
        return

    # Team may belong to multiple groups; mark in each matching block.
    group_cache: dict[int, list[CheckpointGroup]] = {}
    for cfg in configs:
        group_defs = (cfg.config or {}).get("groups", [])
        group_cols = _group_start_cols_from_config(cfg.config or {})
        time_enabled = bool((cfg.config or {}).get("time_enabled"))
        for grp_def, start_col in zip(group_defs, group_cols):
            db_group = _resolve_group_from_cfg(cfg.competition_id, grp_def, group_cache)
            if not db_group:
                continue
            # Is team in this group?
            belongs = (
                TeamGroup.query
                .filter(TeamGroup.team_id == team.id, TeamGroup.group_id == db_group.id)
                .first()
            )
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


def update_checkpoint_scores(team_id: int, checkpoint_id: int, group_name: str, values: dict, scored_at: datetime | None = None):
    """Update score-related fields for a team in a checkpoint tab based on config layout."""
    if not sheets_sync_enabled():
        return
    team = Team.query.get(team_id)
    checkpoint = Checkpoint.query.get(checkpoint_id)
    if not team or not checkpoint:
        return

    configs = (
        SheetConfig.query
        .filter(SheetConfig.tab_type == "checkpoint")
        .filter(SheetConfig.checkpoint_id == checkpoint_id)
        .filter(SheetConfig.competition_id == checkpoint.competition_id)
        .all()
    )
    if not configs:
        return

    try:
        client = SheetsClient(
            service_account_file=current_app.config.get("GOOGLE_SERVICE_ACCOUNT_FILE"),
            service_account_json=current_app.config.get("GOOGLE_SERVICE_ACCOUNT_JSON"),
        )
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

        for grp_def, start_col in zip(group_defs, group_cols):
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
                    client.update_cell(cfg.spreadsheet_id, cfg.tab_name, row, col, values.get(dead_time_header, values.get("dead_time")))
                col += 1
            if time_enabled:
                if time_header in values or "time" in values:
                    client.update_cell(cfg.spreadsheet_id, cfg.tab_name, row, col, values.get(time_header, values.get("time")))
                elif scored_at:
                    client.update_cell(cfg.spreadsheet_id, cfg.tab_name, row, col, scored_at.strftime("%Y-%m-%d %H:%M:%S"))
                col += 1

            for field_name in (grp_def.get("fields") or []):
                if field_name in values:
                    client.update_cell(cfg.spreadsheet_id, cfg.tab_name, row, col, values.get(field_name))
                col += 1

            if points_header in values or "points" in values:
                client.update_cell(cfg.spreadsheet_id, cfg.tab_name, row, col, values.get(points_header, values.get("points")))


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
    per_group_cp_order = per_group_checkpoint_order or _get_group_checkpoint_order_from_db()

    cp_configs = (
        SheetConfig.query
        .filter(SheetConfig.spreadsheet_id == spreadsheet_id)
        .filter(SheetConfig.tab_type == "checkpoint")
        .order_by(SheetConfig.tab_name.asc())
        .all()
    )
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

        header_row = [g.name] + [cfg.tab_name for cfg in relevant]
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
                            f"); \"\")"
                        )
                    else:
                        formula = "\"\""
                except Exception:
                    formula = "\"\""
                row.append(formula)
            values.append(row)
        values.append([])
        values.append([])

    client = SheetsClient(
        service_account_file=current_app.config.get("GOOGLE_SERVICE_ACCOUNT_FILE"),
        service_account_json=current_app.config.get("GOOGLE_SERVICE_ACCOUNT_JSON"),
    )
    ss = client.gc.open_by_key(spreadsheet_id)
    try:
        ws = ss.worksheet(tab_name)
        ws.clear()
    except Exception:
        ws = ss.add_worksheet(title=tab_name, rows=500, cols=100)
    ws.update("A1", values, value_input_option="USER_ENTERED")
    # Apply conditional formatting: TRUE green, FALSE red
    try:
        last_row = len(values)
        last_col = max((len(r) for r in values), default=1)
        requests = [
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [{
                            "sheetId": ws.id,
                            "startRowIndex": 1,
                            "endRowIndex": last_row,
                            "startColumnIndex": 1,
                            "endColumnIndex": last_col,
                        }],
                        "booleanRule": {
                            "condition": {"type": "BOOLEAN"},
                            "format": {"backgroundColor": {"red": 0.8, "green": 1, "blue": 0.8}},
                        }
                    },
                    "index": 0,
                }
            },
            {
                "addConditionalFormatRule": {
                    "rule": {
                        "ranges": [{
                            "sheetId": ws.id,
                            "startRowIndex": 1,
                            "endRowIndex": last_row,
                            "startColumnIndex": 1,
                            "endColumnIndex": last_col,
                        }],
                        "booleanRule": {
                            "condition": {
                                "type": "CUSTOM_FORMULA",
                                "values": [{"userEnteredValue": "=FALSE"}],
                            },
                            "format": {"backgroundColor": {"red": 1, "green": 0.8, "blue": 0.8}},
                        },
                    },
                    "index": 1,
                }
            },
        ]
        ss.batch_update({"requests": requests})
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
            Team.query
            .join(TeamGroup, TeamGroup.team_id == Team.id)
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
        header.extend([block["name"]] + [""] * (col_count - 1))
        subheader.extend(headers)

    values = [header, subheader]
    for i in range(max_rows):
        row = []
        for block in group_blocks:
            if i < len(block["rows"]):
                # pad to col_count
                row.extend(block["rows"][i] + [""] * (col_count - len(block["rows"][i])))
            else:
                row.extend([""] * col_count)
        values.append(row)

    client = SheetsClient(
        service_account_file=current_app.config.get("GOOGLE_SERVICE_ACCOUNT_FILE"),
        service_account_json=current_app.config.get("GOOGLE_SERVICE_ACCOUNT_JSON"),
    )
    ss = client.gc.open_by_key(spreadsheet_id)
    try:
        ws = ss.worksheet(tab_name)
        ws.clear()
    except Exception:
        ws = ss.add_worksheet(title=tab_name, rows=500, cols=20)
    ws.update("A1", values, value_input_option="USER_ENTERED")


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
        SheetConfig.query
        .filter(SheetConfig.spreadsheet_id == spreadsheet_id)
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

    values = []
    blocks = []  # track ranges for org summary

    for g in groups:
        teams = (
            Team.query
            .join(TeamGroup, TeamGroup.team_id == Team.id)
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
        header.extend([cfg.tab_name for cfg in relevant])
        if include_dead_time_sum:
            header.append(lang.get("score_dead_time_sum_header", "Mrtvi čas (sum)"))
        header.append(lang.get("score_total_header", "Skupaj točke"))
        start_row = len(values) + 1  # header row index (1-based)
        values.append(header)

        # For each team, compute row with same column positions
        for t in teams:
            row_idx = len(values) + 1
            row = [g.name, t.number or "", t.name, t.organization or ""]
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

            if include_dead_time_sum:
                if dead_time_formulas and any(f not in ("=0", "0") for f in dead_time_formulas):
                    dt_total_formula = f"=SUM({';'.join(_strip_eq(f) for f in dead_time_formulas)})"
                else:
                    dt_total_formula = "=0"
                row.append(dt_total_formula)

            if cp_formulas:
                pts_total_formula = f"=SUM({';'.join(_strip_eq(f) for f in cp_formulas)})"
            else:
                pts_total_formula = "=0"
            row.append(pts_total_formula)
            values.append(row)
        # record block range for org summary (data rows only)
        data_start = start_row + 1
        data_end = start_row + len(teams)
        total_col_idx = len(header)
        blocks.append({
            "num_col": 2,
            "name_col": 3,
            "org_col": 4,
            "total_col": total_col_idx,
            "start_row": data_start,
            "end_row": data_end,
        })
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
        values.append([
            lang.get("score_org_section_header", "Organizacija"),
            lang.get("score_org_teams_header", "Ekipe"),
            lang.get("score_org_numbers_header", "Številke"),
            lang.get("score_org_count_header", "Št ekip"),
            lang.get("score_org_total_header", "Skupaj točke (org)"),
        ])
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
                name_filters.append(f"FILTER({ncol}{b['start_row']}:{ncol}{b['end_row']}; {ocol}{b['start_row']}:{ocol}{b['end_row']}=A{row_idx})")
                num_filters.append(f"FILTER({tcol}{b['start_row']}:{tcol}{b['end_row']}; {ocol}{b['start_row']}:{ocol}{b['end_row']}=A{row_idx})")
                total_filters.append(f"FILTER({pcol}{b['start_row']}:{pcol}{b['end_row']}; {ocol}{b['start_row']}:{ocol}{b['end_row']}=A{row_idx})")
            # assemble base arrays
            names_raw = "{" + "; ".join(name_filters) + "}"
            nums_raw = "{" + "; ".join(num_filters) + "}"
            totals_raw = "{" + "; ".join(total_filters) + "}"
            # wrap with IFERROR to avoid #N/A when filters have no matches
            names_expr = f"IFERROR({names_raw}; \"\")"
            nums_expr = f"IFERROR({nums_raw}; \"\")"
            totals_expr = f"IFERROR({totals_raw}; 0)"
            count_expr = f"=SUMPRODUCT(N(LEN({nums_expr})>0))"
            org_row = [
                org,
                f"=TEXTJOIN(\", \"; TRUE; {names_expr})",
                f"=TEXTJOIN(\", \"; TRUE; {nums_expr})",
                count_expr,
                f"=SUM({totals_expr})",
            ]
            values.append(org_row)

    client = SheetsClient(
        service_account_file=current_app.config.get("GOOGLE_SERVICE_ACCOUNT_FILE"),
        service_account_json=current_app.config.get("GOOGLE_SERVICE_ACCOUNT_JSON"),
    )
    ss = client.gc.open_by_key(spreadsheet_id)
    try:
        ws = ss.worksheet(tab_name)
        ws.clear()
    except Exception:
        ws = ss.add_worksheet(title=tab_name, rows=800, cols=50)
    ws.update("A1", values, value_input_option="USER_ENTERED")


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

    def _with_retry(func, *args, retries: int = 3, delay: int = 65, **kwargs):
        for attempt in range(1, retries + 1):
            try:
                return func(*args, **kwargs)
            except APIError as exc:
                resp = getattr(exc, "response", None)
                status = getattr(resp, "status_code", None) or getattr(resp, "status", None)
                text = str(exc)
                quota = status == 429 or "429" in text or "Quota exceeded" in text
                if quota and attempt < retries:
                    time.sleep(delay)
                    continue
                raise

    client = SheetsClient(
        service_account_file=current_app.config.get("GOOGLE_SERVICE_ACCOUNT_FILE"),
        service_account_json=current_app.config.get("GOOGLE_SERVICE_ACCOUNT_JSON"),
    )

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
            raw_groups = (
                CheckpointGroup.query
                .filter(CheckpointGroup.id.in_(per_checkpoint_groups[cp.id]))
                .all()
            )
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
            current_col += 1 + (1 if dead_time_enabled else 0) + (1 if time_enabled else 0) + len(grp.get("fields", [])) + 1

        ws = _with_retry(client.add_tab, spreadsheet_id, tab_title)
        _with_retry(client.set_header_row, spreadsheet_id, tab_title, headers)

        for grp, start_col in zip(groups_def, group_start_cols):
            db_group = CheckpointGroup.query.get(grp.get("group_id"))
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
                _with_retry(client.update_column, spreadsheet_id, tab_title, start_col, 2, values)

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
            raw_groups = (
                CheckpointGroup.query
                .filter(CheckpointGroup.id.in_(per_checkpoint_groups[cp.id]))
                .all()
            )
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
