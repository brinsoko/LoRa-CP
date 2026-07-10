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
    ScoreEntry,
    SheetConfig,
    Team,
    TeamGroup,
)
from app.utils.competition import get_competition_group_order
from app.utils.export_safety import escape_formula_cell
from app.utils.lang_store import load_lang
from app.utils.paths import resolve_route_ids
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
    """Return directed checkpoint-name order per group from its path.

    Honors the group's traversal direction: reversed groups get their
    sheet columns in the order they actually run the course.
    """
    orders: dict[str, list[str]] = {}
    groups = CheckpointGroup.query.all()
    name_by_id = {cp.id: cp.name for cp in Checkpoint.query.all()}
    for g in groups:
        route = resolve_route_ids(g)
        orders[g.name] = [name_by_id[cid] for cid in route if cid in name_by_id]
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


def _segment_time_lookup(cfg, group_name: str, row_idx: int) -> str:
    """INDEX/MATCH formula returning the team's arrival time on a CP tab,
    or '=""' when the tab/columns can't be resolved. cfg is resolved by
    checkpoint_id upstream so renamed tabs keep working."""
    from gspread.utils import rowcol_to_a1

    if cfg is None:
        return '=""'
    tab_name = cfg.tab_name
    time_col = _time_col_for_group_in_cp(cfg.config or {}, group_name)
    team_col = _team_col_for_group_in_cp(cfg.config or {}, group_name)
    if time_col is None or team_col is None:
        return '=""'
    tcl = rowcol_to_a1(1, time_col).rstrip("1")
    acl = rowcol_to_a1(1, team_col).rstrip("1")
    return (
        f"=IFERROR(INDEX('{tab_name}'!{tcl}:{tcl}; "
        f"MATCH(B{row_idx}; '{tab_name}'!{acl}:{acl}; 0)); \"\")"
    )


def _build_group_scoring_formulas(
    *,
    group,
    scoring,
    route: list[int],
    cp_id_to_name: dict[int, str],
    relevant_cfgs: list,
    row_idx: int,
    dead_time_sum_expr: str,
    found_eligible_names: set[str] | None = None,
) -> tuple[str, str]:
    """Per-team Časovnica + Found-points formulas from GroupScoring.

    Časovnica uses the route's directed start/finish and the STEPPED
    penalty from the decisions log: deduct penalty_points per FULL
    penalty_minutes block over the threshold (FLOOR), dead time
    subtracted first, floored at min_points. Found points count arrivals
    on counts_for_found checkpoints of the route.

    Returns (casovnica_formula, found_formula); "=0" placeholders when a
    rule can't be expressed so the Total column still adds cleanly.
    """
    found_eligible_names = set(found_eligible_names or ())
    from gspread.utils import rowcol_to_a1

    def _col_letter(col: int) -> str:
        return rowcol_to_a1(1, col).rstrip("1")

    cas_formula = "=0"
    if (
        scoring is not None
        and route
        and scoring.race_max_points is not None
        and scoring.race_threshold_minutes is not None
        and scoring.race_penalty_minutes
        and scoring.race_penalty_points is not None
    ):
        cfg_by_cp = {c.checkpoint_id: c for c in relevant_cfgs if c.checkpoint_id}
        start_cfg = cfg_by_cp.get(route[0]) or next(
            (c for c in relevant_cfgs if c.tab_name == cp_id_to_name.get(route[0])), None
        )
        end_cfg = cfg_by_cp.get(route[-1]) or next(
            (c for c in relevant_cfgs if c.tab_name == cp_id_to_name.get(route[-1])), None
        )
        start_cp_name = start_cfg.tab_name if start_cfg else None
        end_cp_name = end_cfg.tab_name if end_cfg else None
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
                end_lookup = f"INDEX('{end_cp_name}'!{e_t}:{e_t}; MATCH(B{row_idx}; '{end_cp_name}'!{e_a}:{e_a}; 0))"
                max_p = _fmt_num(scoring.race_max_points)
                threshold = _fmt_num(scoring.race_threshold_minutes)
                penalty_p = _fmt_num(scoring.race_penalty_points)
                penalty_m = _fmt_num(scoring.race_penalty_minutes)
                min_p = _fmt_num(scoring.race_min_points or 0)
                cas_formula = (
                    f'=IFERROR(IF({end_lookup}=""; 0; '
                    f"MAX({max_p}-FLOOR(MAX(0; ({end_lookup}-{start_lookup})*1440-({dead_time_sum_expr})-{threshold})"
                    f"/{penalty_m})*{penalty_p}; {min_p})); 0)"
                )

    found_formula = "=0"
    if scoring is not None and scoring.found_points_per is not None:
        route_names = {cp_id_to_name.get(cp_id) for cp_id in route}
        per_cp_terms: list[str] = []
        for cfg in relevant_cfgs:
            if cfg.tab_name not in found_eligible_names:
                continue
            if route_names and cfg.tab_name not in route_names:
                continue
            t_col = _time_col_for_group_in_cp(cfg.config or {}, group.name)
            a_col = _team_col_for_group_in_cp(cfg.config or {}, group.name)
            if t_col is None or a_col is None:
                continue
            tcl = _col_letter(t_col)
            acl = _col_letter(a_col)
            lookup = f"INDEX('{cfg.tab_name}'!{tcl}:{tcl}; MATCH(B{row_idx}; '{cfg.tab_name}'!{acl}:{acl}; 0))"
            per_cp_terms.append(f'IFERROR(IF({lookup}<>""; 1; 0); 0)')
        if per_cp_terms:
            found_formula = f"={_fmt_num(scoring.found_points_per)}*({'+'.join(per_cp_terms)})"

    return cas_formula, found_formula


def _persist_row_maps(cfg: SheetConfig, row_maps_by_index: dict[int, dict[str, int]]) -> None:
    """Cache team_id -> sheet row per group block in cfg.config, written
    at the moment the tab's team column physically gets that order."""
    if not row_maps_by_index:
        return
    config = dict(cfg.config or {})
    groups_blob = [dict(g) if isinstance(g, dict) else g for g in config.get("groups") or []]
    changed = False
    for grp_index, row_map in row_maps_by_index.items():
        if grp_index < len(groups_blob) and isinstance(groups_blob[grp_index], dict):
            groups_blob[grp_index]["row_map"] = row_map
            changed = True
    if changed:
        config["groups"] = groups_blob
        cfg.config = config
        db.session.commit()


def upsert_summary_config(
    competition_id: int, spreadsheet_id: str, spreadsheet_name: str | None, tab_name: str, tab_type: str
) -> None:
    """Record (or repoint) a SheetConfig row for a published summary tab.

    enqueue_summary_rebuilds finds summary tabs to auto-refresh by
    querying SheetConfig for tab_type in (teams, arrivals, total). Those
    rows are never created by the checkpoint wizard (which only writes
    tab_type='checkpoint'), so without this the roster-change auto-refresh
    silently does nothing. One row per (competition, tab_type); a later
    publish to a different sheet repoints it."""
    if (spreadsheet_id or "").startswith("local:"):
        return
    existing = SheetConfig.query.filter_by(competition_id=competition_id, tab_type=tab_type).first()
    if existing is not None:
        existing.spreadsheet_id = spreadsheet_id
        existing.spreadsheet_name = spreadsheet_name or existing.spreadsheet_name
        existing.tab_name = tab_name
    else:
        db.session.add(
            SheetConfig(
                competition_id=competition_id,
                spreadsheet_id=spreadsheet_id,
                spreadsheet_name=spreadsheet_name or "Google Sheet",
                tab_name=tab_name,
                tab_type=tab_type,
            )
        )
    db.session.commit()


def _team_row_for_group(grp_def: dict, db_group, comp_id: int, team_id: int) -> int | None:
    """Row of a team inside a group block on a CP tab.

    Prefers the row_map cached in the config (written whenever the tab's
    team column is rebuilt), so a roster change between enqueue and write
    can't land data on a shifted row. Falls back to the legacy
    recomputed-roster-order index for tabs published before the map
    existed.
    """
    row_map = (grp_def or {}).get("row_map") or {}
    cached = row_map.get(str(team_id))
    if cached:
        try:
            return int(cached)
        except (TypeError, ValueError):
            pass
    nums = (
        db.session.query(Team.id)
        .join(TeamGroup, TeamGroup.team_id == Team.id)
        .filter(TeamGroup.group_id == db_group.id, Team.competition_id == comp_id)
        .order_by(Team.number.asc().nulls_last(), Team.name.asc())
        .all()
    )
    ordered = [n[0] for n in nums]
    try:
        return 2 + ordered.index(team_id)  # header at row 1
    except ValueError:
        return None


def sync_all_checkpoint_tabs(competition_id: int | None = None):
    """Refresh team numbers across every checkpoint-type tab.

    Batches all column writes per CP into one Sheets API call (one
    ws.batch_update per tab) so a large competition can finish well
    inside the gunicorn worker timeout. The earlier implementation did
    one update_column per (CP × group), which on a 15-CP × 5-group
    competition hit the 40-calls/60s throttle and forced a 60-second
    sleep mid-request — the worker then timed out at 30 s and returned
    500. Batched, the same work is ~15 API calls total and finishes in
    a few seconds.
    """
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

        columns_for_this_cp: list[dict] = []
        row_maps_by_index: dict[int, dict[str, int]] = {}
        for grp_index, (grp, start_col) in enumerate(zip(groups, group_cols, strict=False)):
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
                columns_for_this_cp.append({"col": start_col, "start_row": 2, "values": values})
                row_maps_by_index[grp_index] = {str(n[0]): 2 + idx for idx, n in enumerate(nums)}

        if columns_for_this_cp:
            try:
                client.batch_update_columns(cfg.spreadsheet_id, cfg.tab_name, columns_for_this_cp)
                _persist_row_maps(cfg, row_maps_by_index)
            except Exception as exc:
                if _is_missing_worksheet(exc):
                    # The Sheets tab this SheetConfig binds to doesn't exist
                    # on the remote spreadsheet anymore — either it was
                    # never created, the operator deleted it, or a prior
                    # half-finished publish left an orphan binding.
                    # Recreate it inline (the moral equivalent of a
                    # single-tab publish) so the next sync isn't a no-op.
                    try:
                        _heal_missing_checkpoint_tab(client, cfg, comp_id)
                        current_app.logger.info(
                            "sync_all_checkpoint_tabs: auto-healed missing tab %r on %s",
                            cfg.tab_name,
                            cfg.spreadsheet_id,
                        )
                        continue
                    except Exception as heal_exc:
                        try:
                            current_app.logger.warning(
                                "sync_all_checkpoint_tabs: auto-heal of %r failed: %s",
                                cfg.tab_name,
                                heal_exc,
                            )
                        except Exception:
                            pass
                        continue
                # Non-404 failures: log + skip as before. A single bad tab
                # shouldn't abort the whole sync.
                try:
                    current_app.logger.warning("sync_all_checkpoint_tabs: %s failed: %s", cfg.tab_name, exc)
                except Exception:
                    pass


def _is_missing_worksheet(exc: Exception) -> bool:
    """True if `exc` looks like a Google "worksheet not found" / 404."""
    # gspread raises WorksheetNotFound for `ss.worksheet("name")` when the
    # tab doesn't exist; deeper API failures land as APIError with the raw
    # requests.Response attached. Both reach here through `client._call`.
    try:
        from gspread.exceptions import WorksheetNotFound

        if isinstance(exc, WorksheetNotFound):
            return True
    except Exception:
        pass
    resp = getattr(exc, "response", None)
    status = getattr(resp, "status_code", None) or getattr(resp, "status", None)
    if status == 404:
        return True
    # Fallback: the warning in the wild shows up as "<Response [404]>"
    # because Response.__repr__ leaks through. Catch that too so the heal
    # path works even when the exception type drifts between gspread
    # versions.
    return "404" in str(exc) and "Response" in str(exc)


class TabAlreadyPresent(Exception):
    """Heal aborted: the tab is reported as missing on read but present
    on write. The operator likely points cfg.spreadsheet_id at the wrong
    spreadsheet, or the tab has subtly different content from the DB —
    either way, blindly overwriting the live cells is the wrong action."""


def _heal_missing_checkpoint_tab(client, cfg: SheetConfig, competition_id: int) -> None:
    """Recreate a missing per-CP worksheet from its SheetConfig.

    Refuses to overwrite if the tab turns out to exist after all. That
    case shouldn't happen in normal use (the caller only enters here on
    a verified 404), but in the wild we've seen it when the DB's
    cfg.spreadsheet_id pointed at the wrong spreadsheet — gspread's
    metadata-lookup 404s for the tab name we asked, but add_tab against
    that same spreadsheet succeeds-or-collides depending on which side
    of the mismatch the operator put their real tabs. Bailing out
    preserves any manual data and lets the operator fix the binding.
    """
    grid, group_has_formula, row_maps_by_index = _build_local_cp_grid(cfg, competition_id)
    if not grid or not grid[0]:
        raise RuntimeError(f"empty grid for tab {cfg.tab_name!r} — nothing to write")
    n_rows = max(len(grid) + 10, 50)
    n_cols = max(len(grid[0]) + 5, 26)

    try:
        client.add_tab(cfg.spreadsheet_id, cfg.tab_name, rows=n_rows, cols=n_cols)
    except Exception as add_exc:
        msg = str(add_exc).lower()
        if "already exists" in msg or "duplicate" in msg or "worksheet_title_taken" in msg:
            # The tab IS there. Don't clobber.
            raise TabAlreadyPresent(
                f"tab {cfg.tab_name!r} already exists on spreadsheet "
                f"{cfg.spreadsheet_id!r} — refusing to overwrite. "
                "Run scripts/diagnose_sheet_configs.py to inspect the binding."
            ) from add_exc
        raise

    # add_tab created the tab fresh, so we know A1 is empty — safe to
    # write the full grid (headers + team rows + existing scores).
    ss = client._call(client.gc.open_by_key, cfg.spreadsheet_id)
    ws = client._call(ss.worksheet, cfg.tab_name)
    client._call(
        ws.update,
        range_name="A1",
        values=grid,
        value_input_option="USER_ENTERED",
    )
    _persist_row_maps(cfg, row_maps_by_index)
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


def mark_arrival_checkbox(team_id: int, checkpoint_id: int, arrived_at: datetime | None = None):
    """Public entrypoint: records the write in the durable outbox (the
    dedicated sheets-worker drains it), or runs synchronously when there
    is no app context / SHEETS_SYNC_INLINE is set (tests, CLI scripts)."""
    try:
        app = current_app._get_current_object()
    except RuntimeError:
        return mark_arrival_checkbox_sync(team_id, checkpoint_id, arrived_at)
    if app.config.get("SHEETS_SYNC_INLINE"):
        return mark_arrival_checkbox_sync(team_id, checkpoint_id, arrived_at)
    from app.models import Checkpoint as _CP
    from app.utils.sheets_outbox import enqueue_job

    checkpoint = db.session.get(_CP, checkpoint_id)
    if checkpoint is None:
        return
    # No commit here: enqueue_job rides the caller's transaction (see its
    # docstring) so the job and the domain change commit together. A
    # commit at this point would flush the caller's still-open work
    # mid-operation and break bulk saves.
    enqueue_job(
        "arrival",
        checkpoint.competition_id,
        {
            "team_id": team_id,
            "checkpoint_id": checkpoint_id,
            "arrived_at": arrived_at.isoformat() if arrived_at else None,
        },
        f"arrival:{team_id}:{checkpoint_id}",
    )


def mark_arrival_checkbox_sync(team_id: int, checkpoint_id: int, arrived_at: datetime | None = None):
    """Write the arrival timestamp into every linked CP tab in one batch per config.

    Batching strategy
    -----------------
    A team can belong to multiple groups, and a single SheetConfig can carry
    several group blocks. Pre-batching we issued one ``client.update_cell``
    per (cfg, group) — up to N calls per arrival on a multi-group CP tab.

    We now accumulate every (column, row, timestamp) write per config into a
    list of single-cell column specs and fire ONE ``batch_update_columns``
    call per cfg. On a 100-team × 15-CP race with ~2 groups per CP this
    drops mark-arrival traffic from ~30 to ~15 calls per arrival burst,
    staying comfortably under the 40-calls/60s Sheets throttle.

    Public signature unchanged so the outbox worker dispatch benefits
    automatically.
    """
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

    ts = arrived_at or datetime.now()
    ts_str = ts.strftime("%Y-%m-%d %H:%M:%S")

    # Team may belong to multiple groups; mark in each matching block.
    group_cache: dict[int, list[CheckpointGroup]] = {}
    for cfg in configs:
        group_defs = (cfg.config or {}).get("groups", [])
        group_cols = _group_start_cols_from_config(cfg.config or {})
        time_enabled = bool((cfg.config or {}).get("time_enabled"))
        if not time_enabled:
            # Nothing to write on this cfg — arrivals are recorded as a
            # timestamp in the time column. Skip without queueing a batch.
            continue
        dead_time_enabled = bool((cfg.config or {}).get("dead_time_enabled"))
        column_writes: list[dict] = []
        for grp_def, start_col in zip(group_defs, group_cols, strict=False):
            db_group = _resolve_group_from_cfg(cfg.competition_id, grp_def, group_cache)
            if not db_group:
                continue
            # Is team in this group?
            belongs = TeamGroup.query.filter(TeamGroup.team_id == team.id, TeamGroup.group_id == db_group.id).first()
            if not belongs:
                continue
            row = _team_row_for_group(grp_def, db_group, cfg.competition_id, team.id)
            if row is None:
                continue
            time_col = start_col + 1 + (1 if dead_time_enabled else 0)
            column_writes.append({"col": time_col, "start_row": row, "values": [ts_str]})

        if not column_writes:
            continue
        try:
            client.batch_update_columns(cfg.spreadsheet_id, cfg.tab_name, column_writes)
        except Exception as exc:
            current_app.logger.warning("Could not update arrival checkbox: %s", exc)


def update_checkpoint_scores(
    team_id: int, checkpoint_id: int, group_name: str, values: dict, scored_at: datetime | None = None
):
    """Public entrypoint; see mark_arrival_checkbox for the dispatch policy."""
    try:
        app = current_app._get_current_object()
    except RuntimeError:
        return update_checkpoint_scores_sync(team_id, checkpoint_id, group_name, values, scored_at)
    if app.config.get("SHEETS_SYNC_INLINE"):
        return update_checkpoint_scores_sync(team_id, checkpoint_id, group_name, values, scored_at)
    from app.models import Checkpoint as _CP
    from app.utils.sheets_outbox import enqueue_job

    checkpoint = db.session.get(_CP, checkpoint_id)
    if checkpoint is None:
        return
    # No commit here (see mark_arrival_checkbox): the job rides the
    # caller's transaction. group_name is part of the dedup key so
    # concurrent writes for the same team+checkpoint in different groups
    # don't coalesce into one and drop a group's scores.
    enqueue_job(
        "scores",
        checkpoint.competition_id,
        {
            "team_id": team_id,
            "checkpoint_id": checkpoint_id,
            "group_name": group_name,
            "values": values,
            "scored_at": scored_at.isoformat() if scored_at else None,
        },
        f"scores:{team_id}:{checkpoint_id}:{group_name}",
    )


def update_checkpoint_scores_sync(
    team_id: int, checkpoint_id: int, group_name: str, values: dict, scored_at: datetime | None = None
):
    """Update score-related fields for a team in a checkpoint tab based on config layout.

    Batching strategy
    -----------------
    Pre-batching, each (cfg, group) match issued one ``update_cell`` per
    enabled field — dead_time, time, each per-group ``fields`` entry, and
    points. On a 100-team × 15-CP × 2-group race this added up to ~6000
    Sheets API calls and tripped the 40/60s throttle (the worker queue
    grew to ~9 minutes of backlog).

    The new shape walks the cfg in the same order but accumulates every
    write into a list of single-cell column specs, then fires ONE
    ``batch_update_columns`` call per cfg. On the same race the count
    drops to ~1500 calls (4× reduction), well inside the throttle window.

    All semantics preserved:
      * ``dead_time_enabled`` / ``time_enabled`` flags still gate the
        respective writes and the column-shift bookkeeping.
      * Per-group ``points_formula=True`` still skips the Points cell so
        a published formula is never clobbered by a raw value.
      * The ``scored_at`` fallback for the time cell still applies when
        no explicit time value is in ``values``.

    Public signature unchanged so the outbox worker dispatch benefits
    automatically (it just calls into this _sync variant).
    """
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

        column_writes: list[dict] = []

        for grp_def, start_col in zip(group_defs, group_cols, strict=False):
            grp_name = (grp_def.get("name") or "").strip()
            if _norm_name(grp_name) != _norm_name(group_name):
                continue

            db_group = _resolve_group_from_cfg(cfg.competition_id, grp_def, group_cache)
            if not db_group:
                continue

            row = _team_row_for_group(grp_def, db_group, cfg.competition_id, team.id)
            if row is None:
                continue

            col = start_col + 1
            if dead_time_enabled:
                if dead_time_header in values or "dead_time" in values:
                    column_writes.append(
                        {
                            "col": col,
                            "start_row": row,
                            "values": [values.get(dead_time_header, values.get("dead_time"))],
                        }
                    )
                col += 1
            if time_enabled:
                if time_header in values or "time" in values:
                    column_writes.append(
                        {
                            "col": col,
                            "start_row": row,
                            "values": [values.get(time_header, values.get("time"))],
                        }
                    )
                elif scored_at:
                    column_writes.append(
                        {
                            "col": col,
                            "start_row": row,
                            "values": [scored_at.strftime("%Y-%m-%d %H:%M:%S")],
                        }
                    )
                col += 1

            for field_name in grp_def.get("fields") or []:
                if field_name in values:
                    column_writes.append({"col": col, "start_row": row, "values": [values.get(field_name)]})
                col += 1

            # Points cell: when this group's config flags points_formula
            # (set by publish_local_configs_to_spreadsheet after embedding
            # a per-row formula), skip the write so we don't clobber the
            # formula with a raw number. The spreadsheet then recomputes
            # Points from the raw field cells we just wrote.
            if not grp_def.get("points_formula"):
                if points_header in values or "points" in values:
                    column_writes.append(
                        {
                            "col": col,
                            "start_row": row,
                            "values": [values.get(points_header, values.get("points"))],
                        }
                    )

        if not column_writes:
            continue
        try:
            client.batch_update_columns(cfg.spreadsheet_id, cfg.tab_name, column_writes)
        except Exception as exc:
            current_app.logger.warning("Could not update checkpoint scores: %s", exc)


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
            # Members joined comma-separated in one cell so the sheet row
            # still reads as one team. Count column gives at-a-glance
            # roster verification. This data lives only on the Teams tab;
            # per-CP and Score tabs are untouched.
            ordered_members = sorted(t.members or [], key=lambda m: m.position)
            member_names = ", ".join(m.name for m in ordered_members)
            member_count = len(ordered_members)
            rows.append(
                [
                    t.number or "",
                    t.name,
                    t.organization or "",
                    member_names,
                    member_count,
                    "",  # last col reserved for points
                ]
            )
        max_rows = max(max_rows, len(rows))
        group_blocks.append({"name": g.name, "rows": rows})

    # Build grid horizontally: each group = headers
    if not headers:
        headers = [
            lang.get("teams_number_header", "Številka"),
            lang.get("teams_name_header", "Ime ekipe"),
            lang.get("teams_org_header", "Rod/Org"),
            lang.get("teams_members_header", "Člani"),
            lang.get("teams_members_count_header", "Št. članov"),
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
        return "No team data to write. Check that the competition has groups with teams assigned."

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

    # Sheet independence: emit Časovnica (race time rule), Found-points
    # and per-segment columns as formulas over the CP tabs' Time cells,
    # so the spreadsheet computes the final score without our system,
    # and hand-patched arrival times on CP tabs flow through everything.
    from app.models import Checkpoint as _CP
    from app.models import GroupScoring as _GS
    from app.utils.paths import resolve_route_ids
    from app.utils.scoring import resolve_group_segments

    scoring_by_group: dict[int, _GS] = {}
    if competition_id is not None:
        for row in _GS.query.filter_by(competition_id=competition_id).all():
            scoring_by_group[row.group_id] = row
    cp_id_to_name = (
        {c.id: c.name for c in _CP.query.filter(_CP.competition_id == competition_id).all()}
        if competition_id is not None
        else {}
    )
    # Only counts_for_found checkpoints earn found points (virtual CPs,
    # start/finish etc. are unchecked); matches compute_group_contrib.
    found_eligible_names = (
        {
            c.name
            for c in _CP.query.filter(
                _CP.competition_id == competition_id, _CP.counts_for_found.is_(True)
            ).all()
        }
        if competition_id is not None
        else set()
    )

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
        # Timed segments: four columns each (arrival A, arrival B, diff,
        # points) per the redesign plan 3.3 contract. A/B/diff are
        # formulas over the CP tabs' Time cells so a hand-entered time
        # still computes; points rank-spread over the group's diff range.
        group_segments = resolve_group_segments(g)
        segment_col_specs = []
        base_cols = len(header)
        for seg_idx, segment in enumerate(group_segments):
            start_name = cp_id_to_name.get(segment["start_checkpoint_id"], "?")
            end_name = cp_id_to_name.get(segment["end_checkpoint_id"], "?")
            label = segment["label"]
            header.extend(
                [
                    f"{label} A",
                    f"{label} B",
                    f"{label} {lang.get('segment_minutes_header', 'čas (min)')}",
                    f"{label} {lang.get('segment_points_header', 'točke')}",
                ]
            )
            segment_col_specs.append(
                {
                    "segment": segment,
                    "start_name": start_name,
                    "end_name": end_name,
                    "a_col": base_cols + 1 + seg_idx * 4,
                    "b_col": base_cols + 2 + seg_idx * 4,
                    "diff_col": base_cols + 3 + seg_idx * 4,
                    "points_col": base_cols + 4 + seg_idx * 4,
                }
            )
        # Dedicated columns for the two category-level contributions so
        # the spreadsheet can sum the final score on its own.
        header.append(lang.get("score_casovnica_header", "Časovnica"))
        header.append(lang.get("score_found_header", "Najdene KT"))
        header.append(lang.get("score_total_header", "Skupaj točke"))
        start_row = len(values) + 1  # header row index (1-based)
        data_start = start_row + 1
        data_end = start_row + len(teams)
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

            # Dead-time total: always computed (the Časovnica formula
            # subtracts it even when the visible column is omitted), and
            # includes the team-level bonus_dead_time constant so the
            # sheet matches compute_group_contrib.
            dead_time_sum_expr = "0"
            if dead_time_formulas and any(f not in ("=0", "0") for f in dead_time_formulas):
                dead_time_sum_expr = f"SUM({';'.join(_strip_eq(f) for f in dead_time_formulas)})"
            bonus_dead = float(t.bonus_dead_time or 0)
            if bonus_dead:
                dead_time_sum_expr = f"{dead_time_sum_expr}+{_fmt_num(bonus_dead)}"
            if include_dead_time_sum:
                dt_total_formula = "=0" if dead_time_sum_expr == "0" else f"={dead_time_sum_expr}"
                row.append(dt_total_formula)

            # Timed segment cells: A/B arrival lookups, diff as an
            # in-sheet formula over the A/B cells (so hand-patched times
            # recompute), points rank-spread over the group's diff range.
            from gspread.utils import rowcol_to_a1 as _rc

            def _letter(col: int) -> str:
                return _rc(1, col).rstrip("1")

            segment_point_cells = []
            cfg_by_cp_id = {c.checkpoint_id: c for c in relevant if c.checkpoint_id}
            for spec in segment_col_specs:
                a_lookup = _segment_time_lookup(
                    cfg_by_cp_id.get(spec["segment"]["start_checkpoint_id"]), g.name, row_idx
                )
                b_lookup = _segment_time_lookup(
                    cfg_by_cp_id.get(spec["segment"]["end_checkpoint_id"]), g.name, row_idx
                )
                a_cell = f"{_letter(spec['a_col'])}{row_idx}"
                b_cell = f"{_letter(spec['b_col'])}{row_idx}"
                diff_cell = f"{_letter(spec['diff_col'])}{row_idx}"
                diff_col_letter = _letter(spec["diff_col"])
                rng = f"{diff_col_letter}{data_start}:{diff_col_letter}{data_end}"
                maxp = _fmt_num(spec["segment"]["max_points"])
                minp = _fmt_num(spec["segment"]["min_points"])
                row.append(a_lookup)
                row.append(b_lookup)
                row.append(
                    f'=IF(OR({a_cell}=""; {b_cell}=""); ""; ({b_cell}-{a_cell})*1440)'
                )
                row.append(
                    f'=IF({diff_cell}=""; 0; IF(MAX({rng})=MIN({rng}); {maxp}; '
                    f"MAX({maxp}-({diff_cell}-MIN({rng}))/(MAX({rng})-MIN({rng}))*({maxp}-({minp})); {minp})))"
                )
                segment_point_cells.append(f"{_letter(spec['points_col'])}{row_idx}")

            # Časovnica + Found formulas from the category rules, derived
            # from the per-CP tabs' Time columns, computed entirely from
            # cells on this spreadsheet so the system can be offline and
            # the Score tab still produces the correct final total.
            cas_formula, found_formula = _build_group_scoring_formulas(
                group=g,
                scoring=scoring_by_group.get(g.id),
                route=resolve_route_ids(g),
                cp_id_to_name=cp_id_to_name,
                relevant_cfgs=relevant,
                row_idx=row_idx,
                dead_time_sum_expr=dead_time_sum_expr,
                found_eligible_names=found_eligible_names,
            )
            row.append(cas_formula)
            row.append(found_formula)

            # Total = per-CP points + segment points + časovnica + found,
            # all computed from cells on the spreadsheet.
            total_pieces = [_strip_eq(f) for f in cp_formulas]
            total_pieces.extend(segment_point_cells)
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
    orgs_query = (
        db.session.query(Team.organization)
        .filter(Team.organization.isnot(None))
        .filter(func.trim(Team.organization) != "")
    )
    if competition_id is not None:
        orgs_query = orgs_query.filter(Team.competition_id == competition_id)
    orgs = orgs_query.distinct().order_by(Team.organization.asc()).all()
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


def build_public_summary_tab(
    spreadsheet_id: str,
    tab_name: str,
    *,
    competition_id: int | None = None,
):
    """Build a public-facing summary tab on the spreadsheet.

    Columns: Group, Number, Team, Organization, Total points. No per-CP
    breakdown, no dead time, no time-trial split, no formulas. This is
    the version we share with the public during a race so spectators
    can see ranks without exposing scoring internals.

    Data path mirrors the on-screen /scores/view: we call
    ``_build_scores_context(competition_id, None)`` so the rows include
    the live time_race recomputations + global rule contributions, then
    sort by group order (already established in the context) and then
    by total points descending. Final values only — no formulas — so
    spectators see exactly what we see and we don't leak intermediate
    columns by accident.

    Headers come from ``load_lang()`` for the standard score-column
    keys; Slovenian defaults match the rest of the spreadsheet.

    Returns None on success, or a short error string for the caller to
    surface as a flash message. Sheets API errors propagate (the
    publisher wraps the call in a try/except so a failure here is
    non-fatal for the rest of the publish).
    """
    if not sheets_sync_enabled():
        return "Sheets sync is disabled."
    if competition_id is None:
        return "Public summary tab requires a competition_id."

    # Import here so module import time stays free of the routes layer
    # (and so we don't risk a circular import when the blueprint
    # eventually imports sheets utilities).
    from app.blueprints.scores.routes import _build_scores_context

    context = _build_scores_context(competition_id, None)
    rows = context.get("rows") or []

    lang = load_lang()
    header = [
        lang.get("score_group_header", "Skupina"),
        lang.get("score_number_header", "Številka"),
        lang.get("score_team_header", "Ekipa"),
        lang.get("score_org_header", "Organizacija"),
        lang.get("score_total_header", "Skupaj točk"),
    ]

    # _build_scores_context already sorts by (group_idx, group_name,
    # dnf-last, -total, name). That gives us "group order, ranks within
    # group" which is exactly what spectators want. We re-sort here
    # only to guarantee the ordering even if upstream changes the
    # default, and to keep the contract self-documenting.
    def _sort_key(r):
        group_name = (r.get("group") or "").strip()
        total = float(r.get("total") or 0.0)
        return (group_name, 1 if r.get("dnf") else 0, -total, (r.get("name") or "").lower())

    sorted_rows = sorted(rows, key=_sort_key)

    values: list[list] = [header]
    for r in sorted_rows:
        values.append(
            [
                escape_formula_cell(r.get("group") or ""),
                r.get("number") if r.get("number") is not None else "",
                escape_formula_cell(r.get("name") or ""),
                escape_formula_cell(r.get("organization") or ""),
                round(float(r.get("total") or 0.0), 2),
            ]
        )

    # Same guard as the other summary builders: header-only means the
    # competition has no teams yet, so a write would be misleading.
    if len(values) <= 1:
        return "No team data to write to public summary tab."

    client = get_sheets_client(current_app)
    ss = client._call(client.gc.open_by_key, spreadsheet_id)
    try:
        ws = client._call(ss.worksheet, tab_name)
        client._call(ws.clear)
    except Exception:
        ws = client._call(ss.add_worksheet, title=tab_name, rows=max(len(values) + 20, 100), cols=10)
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
    checkpoints = checkpoints_query.order_by(Checkpoint.position.asc().nulls_last(), Checkpoint.name.asc()).all()
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
            from app.utils.paths import group_ids_containing_checkpoint

            member_ids = group_ids_containing_checkpoint(cp.competition_id, cp.id)
            raw_groups = (
                CheckpointGroup.query.filter(CheckpointGroup.id.in_(member_ids)).all() if member_ids else []
            )

        def _sort_key(g):
            norm = g.name.lower().strip()
            return (group_order_norm.index(norm) if norm in group_order_norm else len(group_order_norm), g.name)

        ordered_groups = sorted(raw_groups, key=_sort_key)
        time_enabled = bool(record_time_cp and cp.id in record_time_cp)
        # Tab layout is generated from ScoreField (per-group resolution);
        # the legacy per_checkpoint_extra_fields override is honored when
        # a caller still passes it.
        from app.utils.scoring import resolve_fields as _resolve_fields

        if per_checkpoint_extra_fields and cp.id in per_checkpoint_extra_fields:
            extra_fields = per_checkpoint_extra_fields.get(cp.id, [])
            groups_def = [{"group_id": g.id, "name": g.name, "fields": list(extra_fields)} for g in ordered_groups]
        else:
            groups_def = [
                {
                    "group_id": g.id,
                    "name": g.name,
                    "fields": [f["key"] for f in _resolve_fields(cp.id, g.id)],
                }
                for g in ordered_groups
            ]
        if per_checkpoint_dead_time:
            dead_time_enabled = per_checkpoint_dead_time.get(cp.id, bool(cp.dead_time_enabled))
        else:
            dead_time_enabled = bool(cp.dead_time_enabled)
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
    checkpoints = checkpoints_query.order_by(Checkpoint.position.asc().nulls_last(), Checkpoint.name.asc()).all()
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
            from app.utils.paths import group_ids_containing_checkpoint

            member_ids = group_ids_containing_checkpoint(cp.competition_id, cp.id)
            raw_groups = (
                CheckpointGroup.query.filter(CheckpointGroup.id.in_(member_ids)).all() if member_ids else []
            )

        def _sort_key(g):
            norm = g.name.lower().strip()
            return (group_order_norm.index(norm) if norm in group_order_norm else len(group_order_norm), g.name)

        ordered_groups = sorted(raw_groups, key=_sort_key)
        time_enabled = bool(record_time_cp and cp.id in record_time_cp)
        # Tab layout is generated from ScoreField (per-group resolution);
        # the legacy per_checkpoint_extra_fields override is honored when
        # a caller still passes it.
        from app.utils.scoring import resolve_fields as _resolve_fields

        if per_checkpoint_extra_fields and cp.id in per_checkpoint_extra_fields:
            extra_fields = per_checkpoint_extra_fields.get(cp.id, [])
            groups_def = [{"group_id": g.id, "name": g.name, "fields": list(extra_fields)} for g in ordered_groups]
        else:
            groups_def = [
                {
                    "group_id": g.id,
                    "name": g.name,
                    "fields": [f["key"] for f in _resolve_fields(cp.id, g.id)],
                }
                for g in ordered_groups
            ]
        if per_checkpoint_dead_time:
            dead_time_enabled = per_checkpoint_dead_time.get(cp.id, bool(cp.dead_time_enabled))
        else:
            dead_time_enabled = bool(cp.dead_time_enabled)
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
    """Translate one field rule into a Sheets formula expression
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
        deviation_expr = f"MAX({max_p}-ABS({cell_ref}-{target})/{penalty_d}*{penalty_p}; {min_p})"
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
    legacy-shaped rule blob (built from ScoreField rows).

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
            # A counted field with no column on the tab (added after the
            # wizard ran) would silently undercount; fall back to the
            # system-written total instead.
            return None
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
) -> tuple[list[list], dict[int, bool], dict[int, dict[str, int]]]:
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
        return [headers], {}, {}

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
                    ScoreEntry.query.filter_by(competition_id=competition_id, team_id=t_id, checkpoint_id=cp_id)
                    .order_by(ScoreEntry.created_at.desc())
                    .first()
                )
                ci = Checkin.query.filter_by(competition_id=competition_id, team_id=t_id, checkpoint_id=cp_id).first()
                if ci and ci.timestamp:
                    checkin_ts = ci.timestamp
            rows.append(
                {
                    "team_id": t_id,
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
            # Legacy-shaped rule blob from the resolved ScoreField rows so
            # _points_formula_from_rule keeps working unchanged.
            from app.utils.scoring import resolve_fields as _resolve_fields

            resolved = _resolve_fields(cp_id, gid)
            if resolved:
                rule_by_group[gid] = {
                    "field_rules": {f["key"]: (f.get("rule") or {}) for f in resolved},
                    "total_fields": [f["key"] for f in resolved if f.get("counts_in_total", True)],
                }

    # team_id -> physical row per group block, persisted by callers so
    # subsequent cell writes address rows by key (redesign plan 3.4).
    row_maps_by_index: dict[int, dict[str, int]] = {}
    for spec_index, group_rows in enumerate(teams_per_group):
        row_maps_by_index[spec_index] = {
            str(row["team_id"]): i + 2 for i, row in enumerate(group_rows) if row.get("team_id")
        }

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

    return values, group_has_formula, row_maps_by_index


def publish_local_configs_to_spreadsheet(
    competition_id: int,
    spreadsheet_id: str,
    *,
    build_summary_tabs: bool = True,
) -> dict:
    """Publish a competition's SheetConfigs to a real Google Sheet.

    For each SheetConfig whose spreadsheet_id != the target (covers both
    fresh "local:N" sentinels and configs currently bound to a different
    remote sheet - so this also handles "repoint to a new sheet"):
      1. Build the full per-CP grid (headers + team rows + any existing
         ScoreEntry data + check-in timestamps) in memory.
      2. Create (or reuse) the remote tab and write the grid in one
         batched update. Two Sheets API calls per CP.
      3. Rebind SheetConfig.spreadsheet_id to the target, committed per-CP
         so partial progress survives a mid-batch failure.

    Configs already pointing at the target are skipped, so re-running
    publish with the same target is a no-op aside from rebuilding summary
    tabs.

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
        .filter(SheetConfig.spreadsheet_id != spreadsheet_id)
        .order_by(SheetConfig.tab_name.asc())
        .all()
    )
    if not configs:
        result["errors"].append("No SheetConfigs need publishing (all already point at the target).")
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
            # When the wizard runs a second time it leaves fresh "local:N"
            # rows alongside an older row that already points at the target
            # for the same tab_name. The UNIQUE(spreadsheet_id, tab_name)
            # constraint means we can't have both pointing at the target;
            # the *fresh* row is the one the operator just generated and
            # wants published, so delete the older slot-holder and let the
            # rebuild below overwrite the remote tab with the new content.
            slot_holder = (
                SheetConfig.query.filter(SheetConfig.spreadsheet_id == spreadsheet_id)
                .filter(SheetConfig.tab_name == cfg.tab_name)
                .filter(SheetConfig.id != cfg.id)
                .first()
            )
            if slot_holder is not None:
                current_app.logger.info(
                    "publish: replacing stale SheetConfig id=%s tab=%r with fresh source id=%s",
                    slot_holder.id,
                    cfg.tab_name,
                    cfg.id,
                )
                db.session.delete(slot_holder)
                db.session.flush()

            grid, group_has_formula, row_maps_by_index = _build_local_cp_grid(cfg, competition_id)
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
            _persist_row_maps(cfg, row_maps_by_index)
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
        # "Javno" (public) is the spectator-facing tab: group, number,
        # team, organization, total. Same try/except shape as the other
        # summary tabs so a failure here is logged but doesn't prevent
        # the rest of the publish from succeeding.
        # cfg_type is the SheetConfig.tab_type enqueue_summary_rebuilds
        # looks up (teams/arrivals/total); 'public' has no auto-rebuild
        # kind so it isn't recorded.
        for label, fn, tab_label, cfg_type in [
            ("teams", build_teams_tab, lang.get("teams_tab") or "Teams", "teams"),
            ("arrivals", build_arrivals_tab, lang.get("arrivals_tab") or "Arrivals", "arrivals"),
            ("score", build_score_tab, lang.get("score_tab") or "Score", "total"),
            ("public", build_public_summary_tab, lang.get("public_tab") or "Javno", None),
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
                    if cfg_type:
                        upsert_summary_config(
                            competition_id, spreadsheet_id, spreadsheet_name, tab_label, cfg_type
                        )
            except Exception as exc:
                result["errors"].append(f"{label} build raised: {exc}")
                try:
                    current_app.logger.exception("Summary tab %s build failed", label)
                except Exception:
                    pass

    return result
