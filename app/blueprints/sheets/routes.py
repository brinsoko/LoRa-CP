# app/blueprints/sheets/routes.py
from __future__ import annotations

import json
from typing import List

from flask import Blueprint, render_template, request, redirect, url_for, flash, current_app
from flask_babel import gettext as _

from app.extensions import db
from app.models import SheetConfig, Checkpoint, CheckpointGroup, Team, TeamGroup, CheckpointGroupLink
from sqlalchemy import func
from app.utils.perms import roles_required
from app.utils.competition import get_current_competition_id
from app.utils.sheets_client import SheetsClient
from app.utils.sheets_sync import sync_all_checkpoint_tabs
from app.utils.sheets_sync import build_arrivals_tab, build_teams_tab, build_score_tab
from app.utils.sheets_sync import wizard_build_checkpoint_tabs, wizard_create_checkpoint_configs
from app.utils.lang_store import load_lang, save_lang
from app.utils.sheets_settings import (
    load_settings as load_sheet_settings,
    save_settings as save_sheet_settings,
    sheets_sync_enabled,
)

sheets_bp = Blueprint("sheets_admin", __name__, template_folder="../../templates")


def _get_sheets_client() -> SheetsClient:
    cfg = current_app.config
    return SheetsClient(
        service_account_file=cfg.get("GOOGLE_SERVICE_ACCOUNT_FILE"),
        service_account_json=cfg.get("GOOGLE_SERVICE_ACCOUNT_JSON"),
    )


def _require_competition():
    comp_id = get_current_competition_id()
    if not comp_id:
        flash(_("Select a competition first."), "warning")
        return None, redirect(url_for("main.select_competition"))
    return comp_id, None


def _require_sheets_enabled():
    if not sheets_sync_enabled():
        flash(_("Sheets sync is disabled."), "warning")
        return redirect(url_for("sheets_admin.list_sheets"))
    return None


def _local_spreadsheet_id(comp_id: int) -> str:
    return f"local:{comp_id}"


def _parse_group_fields(raw: str) -> List[dict]:
    """Parse textarea lines of format: GroupName|field1,field2"""
    result: List[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        if "|" in line:
            name, fields_raw = line.split("|", 1)
            fields = [f.strip() for f in fields_raw.split(",") if f.strip()]
        else:
            name, fields = line, []
        result.append({"name": name.strip(), "fields": fields})
    return result


@sheets_bp.route("/", methods=["GET"])
@roles_required("admin")
def list_sheets():
    comp_id, redirect_resp = _require_competition()
    if redirect_resp:
        return redirect_resp
    lang = load_lang()
    sheets_settings = load_sheet_settings()
    configs = (
        SheetConfig.query
        .filter(SheetConfig.competition_id == comp_id)
        .order_by(SheetConfig.created_at.desc())
        .all()
    )
    checkpoints = (
        Checkpoint.query
        .filter(Checkpoint.competition_id == comp_id)
        .order_by(Checkpoint.name.asc())
        .all()
    )
    groups = (
        CheckpointGroup.query
        .options(db.joinedload(CheckpointGroup.checkpoint_links).joinedload(CheckpointGroupLink.checkpoint))
        .filter(CheckpointGroup.competition_id == comp_id)
        .order_by(CheckpointGroup.position.asc().nulls_last(), CheckpointGroup.name.asc())
        .all()
    )
    return render_template(
        "sheets_admin.html",
        configs=configs,
        checkpoints=checkpoints,
        groups=groups,
        lang=lang,
        sheets_settings=sheets_settings,
    )


@sheets_bp.route("/lang", methods=["GET"])
@roles_required("admin")
def lang_settings():
    lang = load_lang()
    return render_template("sheets_lang.html", lang=lang)


@sheets_bp.route("/save-lang", methods=["POST"])
@roles_required("admin")
def save_lang_settings():
    data = {
        "arrived_header": (request.form.get("arrived_header") or "").strip() or None,
        "points_header": (request.form.get("points_header") or "").strip() or None,
        "dead_time_header": (request.form.get("dead_time_header") or "").strip() or None,
        "time_header": (request.form.get("time_header") or "").strip() or None,
        "teams_tab": (request.form.get("teams_tab") or "").strip() or None,
        "teams_number_header": (request.form.get("teams_number_header") or "").strip() or None,
        "teams_name_header": (request.form.get("teams_name_header") or "").strip() or None,
        "teams_org_header": (request.form.get("teams_org_header") or "").strip() or None,
        "teams_points_header": (request.form.get("teams_points_header") or "").strip() or None,
        "arrivals_tab": (request.form.get("arrivals_tab") or "").strip() or None,
        "score_tab": (request.form.get("score_tab") or "").strip() or None,
        "score_group_header": (request.form.get("score_group_header") or "").strip() or None,
        "score_number_header": (request.form.get("score_number_header") or "").strip() or None,
        "score_team_header": (request.form.get("score_team_header") or "").strip() or None,
        "score_org_header": (request.form.get("score_org_header") or "").strip() or None,
        "score_dead_time_sum_header": (request.form.get("score_dead_time_sum_header") or "").strip() or None,
        "score_total_header": (request.form.get("score_total_header") or "").strip() or None,
        "score_org_section_header": (request.form.get("score_org_section_header") or "").strip() or None,
        "score_org_teams_header": (request.form.get("score_org_teams_header") or "").strip() or None,
        "score_org_numbers_header": (request.form.get("score_org_numbers_header") or "").strip() or None,
        "score_org_count_header": (request.form.get("score_org_count_header") or "").strip() or None,
        "score_org_total_header": (request.form.get("score_org_total_header") or "").strip() or None,
    }
    save_lang({k: v for k, v in data.items() if v})
    flash(_("Language pack saved."), "success")
    return redirect(url_for("sheets_admin.list_sheets"))


@sheets_bp.route("/save-settings", methods=["POST"])
@roles_required("admin")
def save_sheets_settings():
    sync_enabled = bool(request.form.get("sheets_sync_enabled"))
    save_sheet_settings({"sync_enabled": sync_enabled})
    flash(_("Sheets settings saved."), "success")
    return redirect(url_for("sheets_admin.list_sheets"))


@sheets_bp.route("/build-arrivals", methods=["POST"])
@roles_required("admin")
def build_arrivals():
    redirect_resp = _require_sheets_enabled()
    if redirect_resp:
        return redirect_resp
    comp_id, redirect_resp = _require_competition()
    if redirect_resp:
        return redirect_resp
    """Build/update an arrivals matrix tab with formulas pointing to checkpoint tabs."""
    spreadsheet_id = (request.form.get("spreadsheet_id") or "").strip()
    lang = load_lang()
    tab_name = (request.form.get("tab_name") or lang.get("arrivals_tab") or "Arrivals").strip()
    group_order_raw = request.form.get("group_order") or ""
    group_order = [g.strip() for g in group_order_raw.split(",") if g.strip()] if group_order_raw else None
    cp_order_raw = request.form.get("checkpoint_order") or ""
    cp_order = [c.strip() for c in cp_order_raw.split(",") if c.strip()] if cp_order_raw else None
    per_group_cp_order_raw = (request.form.get("per_group_cp_order") or "").strip()
    per_group_cp_order = {}
    if per_group_cp_order_raw:
        try:
            import json
            per_group_cp_order = json.loads(per_group_cp_order_raw)
        except Exception:
            per_group_cp_order = {}

    if not spreadsheet_id:
        flash(_("Spreadsheet ID is required to build arrivals."), "warning")
        return redirect(url_for("sheets_admin.list_sheets"))

    try:
        err = build_arrivals_tab(
            spreadsheet_id,
            tab_name,
            competition_id=comp_id,
            group_order_override=group_order,
            checkpoint_order_override=cp_order,
            per_group_checkpoint_order=per_group_cp_order or None,
        )
        if err:
            flash(err, "warning")
            return redirect(url_for("sheets_admin.list_sheets"))
    except Exception as exc:
        current_app.logger.exception("Failed to build arrivals tab")
        flash(_("Failed to build arrivals tab: %(error)s", error=exc), "warning")
        return redirect(url_for("sheets_admin.list_sheets"))

    flash(_("Arrivals tab '%(tab)s' updated.", tab=tab_name), "success")
    return redirect(url_for("sheets_admin.list_sheets"))


@sheets_bp.route("/build-teams", methods=["POST"])
@roles_required("admin")
def build_teams():
    redirect_resp = _require_sheets_enabled()
    if redirect_resp:
        return redirect_resp
    comp_id, redirect_resp = _require_competition()
    if redirect_resp:
        return redirect_resp
    spreadsheet_id = (request.form.get("spreadsheet_id") or "").strip()
    lang = load_lang()
    tab_name = (request.form.get("tab_name") or lang.get("teams_tab") or "Teams").strip()
    headers_raw = (request.form.get("teams_headers") or "").strip()
    headers = [h.strip() for h in headers_raw.split(",") if h.strip()] if headers_raw else None
    if headers is not None and not headers:
        headers = None
    group_order_raw = request.form.get("group_order") or ""
    group_order = [g.strip() for g in group_order_raw.split(",") if g.strip()] if group_order_raw else None
    if not spreadsheet_id:
        flash(_("Spreadsheet ID is required."), "warning")
        return redirect(url_for("sheets_admin.list_sheets"))
    try:
        err = build_teams_tab(
            spreadsheet_id,
            tab_name,
            headers=headers,
            group_order_override=group_order,
            competition_id=comp_id,
        )
        if err:
            flash(err, "warning")
            return redirect(url_for("sheets_admin.list_sheets"))
    except Exception as exc:
        current_app.logger.exception("Failed to build teams tab")
        flash(_("Failed to build teams tab: %(error)s", error=exc), "warning")
        return redirect(url_for("sheets_admin.list_sheets"))
    flash(_("Teams tab '%(tab)s' updated.", tab=tab_name), "success")
    return redirect(url_for("sheets_admin.list_sheets"))


@sheets_bp.route("/build-score", methods=["POST"])
@roles_required("admin")
def build_score():
    redirect_resp = _require_sheets_enabled()
    if redirect_resp:
        return redirect_resp
    comp_id, redirect_resp = _require_competition()
    if redirect_resp:
        return redirect_resp
    spreadsheet_id = (request.form.get("spreadsheet_id") or "").strip()
    lang = load_lang()
    tab_name = (request.form.get("tab_name") or lang.get("score_tab") or "Score").strip()
    include_dead_time_sum = bool(request.form.get("include_dead_time_sum"))
    group_order_raw = request.form.get("group_order") or ""
    group_order = [g.strip() for g in group_order_raw.split(",") if g.strip()] if group_order_raw else None
    cp_order_raw = request.form.get("checkpoint_order") or ""
    cp_order = [c.strip() for c in cp_order_raw.split(",") if c.strip()] if cp_order_raw else None
    per_group_cp_order_raw = (request.form.get("per_group_cp_order") or "").strip()
    if not spreadsheet_id:
        flash(_("Spreadsheet ID is required."), "warning")
        return redirect(url_for("sheets_admin.list_sheets"))
    try:
        per_group_cp_order = {}
        if per_group_cp_order_raw:
            try:
                import json
                per_group_cp_order = json.loads(per_group_cp_order_raw)
            except Exception:
                per_group_cp_order = {}
        err = build_score_tab(
            spreadsheet_id,
            tab_name,
            include_dead_time_sum=include_dead_time_sum,
            group_order_override=group_order,
            checkpoint_order_override=cp_order,
            per_group_checkpoint_order=per_group_cp_order or None,
            competition_id=comp_id,
        )
        if err:
            flash(err, "warning")
            return redirect(url_for("sheets_admin.list_sheets"))
    except Exception as exc:
        current_app.logger.exception("Failed to build score tab")
        flash(_("Failed to build score tab: %(error)s", error=exc), "warning")
        return redirect(url_for("sheets_admin.list_sheets"))
    flash(_("Score tab '%(tab)s' updated.", tab=tab_name), "success")
    return redirect(url_for("sheets_admin.list_sheets"))


@sheets_bp.route("/prune-missing", methods=["POST"])
@roles_required("admin")
def prune_missing():
    redirect_resp = _require_sheets_enabled()
    if redirect_resp:
        return redirect_resp
    comp_id, redirect_resp = _require_competition()
    if redirect_resp:
        return redirect_resp
    configs = SheetConfig.query.filter(SheetConfig.competition_id == comp_id).all()
    if not configs:
        flash(_("No configs to prune."), "info")
        return redirect(url_for("sheets_admin.list_sheets"))

    removed = 0
    client = None
    try:
        client = _get_sheets_client()
    except Exception as exc:
        flash(_("Could not init Sheets client: %(error)s", error=exc), "warning")
        return redirect(url_for("sheets_admin.list_sheets"))

    by_sheet: dict[str, list[SheetConfig]] = {}
    for cfg in configs:
        by_sheet.setdefault(cfg.spreadsheet_id, []).append(cfg)

    for sheet_id, cfgs in by_sheet.items():
        try:
            ss = client.gc.open_by_key(sheet_id)
            titles = {ws.title for ws in ss.worksheets()}
        except Exception:
            # if we cannot open, skip deleting to avoid accidental loss
            continue
        for cfg in cfgs:
            if cfg.tab_name not in titles:
                db.session.delete(cfg)
                removed += 1

    if removed:
        db.session.commit()
        flash(_("Pruned %(count)s stale config(s) (tabs no longer exist).", count=removed), "success")
    else:
        flash(_("No stale configs found."), "info")

    return redirect(url_for("sheets_admin.list_sheets"))


@sheets_bp.route("/wizard/checkpoints", methods=["POST"])
@roles_required("admin")
def wizard_checkpoints():
    comp_id, redirect_resp = _require_competition()
    if redirect_resp:
        return redirect_resp
    local_only = bool(request.form.get("local_only"))
    spreadsheet_id = (request.form.get("spreadsheet_id") or "").strip()
    use_sheets = sheets_sync_enabled() and not local_only
    lang = load_lang()
    arrived_header = (request.form.get("arrived_header") or lang.get("arrived_header") or "Arrived to CP").strip()
    points_header = (request.form.get("points_header") or lang.get("points_header") or "Points").strip()
    dead_time_header = (request.form.get("dead_time_header") or lang.get("dead_time_header") or "Dead Time [min]").strip()
    time_header = (request.form.get("time_header") or lang.get("time_header") or "Čas").strip()
    checkpoints = (
        Checkpoint.query
        .filter(Checkpoint.competition_id == comp_id)
        .order_by(Checkpoint.name.asc())
        .all()
    )
    group_order_raw = request.form.get("group_order") or ""
    checkpoint_order_raw = (request.form.get("checkpoint_order") or "").strip()
    checkpoint_order = [c.strip() for c in checkpoint_order_raw.split(",") if c.strip()] if checkpoint_order_raw else None
    per_group_cp_order_raw = (request.form.get("per_group_cp_order") or "").strip()
    per_group_cp_order = {}
    if per_group_cp_order_raw:
        try:
            per_group_cp_order = json.loads(per_group_cp_order_raw)
        except Exception:
            per_group_cp_order = {}
    per_cp_fields = {}
    per_cp_dead_time = {}
    per_cp_groups = {}
    per_cp_tabname = {}
    per_cp_create = set()
    per_cp_record_time = set()
    for key, val in request.form.items():
        if key.startswith("create_cp_"):
            cp_id = key.replace("create_cp_", "")
            try:
                cp_id_int = int(cp_id)
            except Exception:
                continue
            if val == "1":
                per_cp_create.add(cp_id_int)
        if key.startswith("tab_name_cp_"):
            cp_id = key.replace("tab_name_cp_", "")
            try:
                cp_id_int = int(cp_id)
            except Exception:
                continue
            if val.strip():
                per_cp_tabname[cp_id_int] = val.strip()
        if key.startswith("extra_fields_cp_"):
            cp_id = key.replace("extra_fields_cp_", "")
            try:
                cp_id_int = int(cp_id)
            except Exception:
                continue
            if val.strip():
                per_cp_fields[cp_id_int] = [x.strip() for x in val.split(",") if x.strip()]
        if key.startswith("dead_time_cp_"):
            cp_id = key.replace("dead_time_cp_", "")
            try:
                cp_id_int = int(cp_id)
            except Exception:
                continue
            per_cp_dead_time[cp_id_int] = (val == "1")
        if key.startswith("record_time_cp_"):
            cp_id = key.replace("record_time_cp_", "")
            try:
                cp_id_int = int(cp_id)
            except Exception:
                continue
            if val == "1":
                per_cp_record_time.add(cp_id_int)
    # checkboxes for groups per checkpoint
    for ck in request.form:
        if ck.startswith("group_ids_cp_"):
            cp_id = ck.replace("group_ids_cp_", "")
            try:
                cp_id_int = int(cp_id)
            except Exception:
                continue
            selected = request.form.getlist(f"group_ids_cp_{cp_id_int}")
            ids = []
            for s in selected:
                try:
                    ids.append(int(s))
                except Exception:
                    continue
            if ids:
                per_cp_groups[cp_id_int] = ids

    if not spreadsheet_id and not use_sheets:
        spreadsheet_id = _local_spreadsheet_id(comp_id)
    elif not spreadsheet_id and use_sheets:
        flash(_("Spreadsheet ID is required."), "warning")
        return redirect(url_for("sheets_admin.list_sheets"))

    selected_count = len(per_cp_create) if per_cp_create else len(checkpoints)
    if selected_count == 0:
        flash(_("Select at least one checkpoint to create."), "warning")
        return redirect(url_for("sheets_admin.list_sheets"))

    try:
        if not use_sheets:
            created, skipped = wizard_create_checkpoint_configs(
                spreadsheet_id=spreadsheet_id,
                spreadsheet_name="Local",
                arrived_header=arrived_header,
                points_header=points_header,
                dead_time_header=dead_time_header,
                time_header=time_header,
                group_order=[g.strip() for g in group_order_raw.split(",") if g.strip()] or None,
                competition_id=comp_id,
                per_checkpoint_extra_fields=per_cp_fields,
                per_checkpoint_dead_time=per_cp_dead_time or None,
                per_checkpoint_groups=per_cp_groups or None,
                per_checkpoint_tabnames=per_cp_tabname or None,
                create_only=per_cp_create or None,
                checkpoint_order_override=checkpoint_order,
                per_group_checkpoint_order=per_group_cp_order or None,
                record_time_cp=per_cp_record_time or None,
            )
        else:
            created, skipped = wizard_build_checkpoint_tabs(
                spreadsheet_id=spreadsheet_id,
                arrived_header=arrived_header,
                points_header=points_header,
                dead_time_header=dead_time_header,
                time_header=time_header,
                group_order=[g.strip() for g in group_order_raw.split(",") if g.strip()] or None,
                competition_id=comp_id,
                per_checkpoint_extra_fields=per_cp_fields,
                per_checkpoint_dead_time=per_cp_dead_time or None,
                per_checkpoint_groups=per_cp_groups or None,
                per_checkpoint_tabnames=per_cp_tabname or None,
                create_only=per_cp_create or None,
                checkpoint_order_override=checkpoint_order,
                per_group_checkpoint_order=per_group_cp_order or None,
                record_time_cp=per_cp_record_time or None,
                pause_every=8,  # throttle to avoid 429s (~5 calls per tab => ~40/min)
                pause_seconds=65,
            )
    except Exception as exc:
        current_app.logger.exception("Wizard failed")
        flash(_("Wizard failed: %(error)s", error=exc), "warning")
        return redirect(url_for("sheets_admin.list_sheets"))

    if not use_sheets:
        flash(_("Wizard completed. Created %(created)s local configs, skipped %(skipped)s existing.", created=created, skipped=skipped), "success")
    else:
        flash(_("Wizard completed. Created %(created)s tabs, skipped %(skipped)s existing.", created=created, skipped=skipped), "success")
    return redirect(url_for("sheets_admin.list_sheets"))


@sheets_bp.route("/sync-team-numbers/<int:config_id>", methods=["POST"])
@roles_required("admin")
def sync_team_numbers(config_id: int):
    redirect_resp = _require_sheets_enabled()
    if redirect_resp:
        return redirect_resp
    comp_id, redirect_resp = _require_competition()
    if redirect_resp:
        return redirect_resp
    cfg = (
        SheetConfig.query
        .filter(SheetConfig.competition_id == comp_id, SheetConfig.id == config_id)
        .first()
    )
    if not cfg:
        flash(_("Config not found."), "warning")
        return redirect(url_for("sheets_admin.list_sheets"))
    if not cfg.config or not cfg.config.get("groups"):
        flash(_("Config is missing groups; cannot sync."), "warning")
        return redirect(url_for("sheets_admin.list_sheets"))

    try:
        sync_all_checkpoint_tabs(competition_id=comp_id)
    except Exception as exc:
        current_app.logger.exception("Failed to sync team numbers")
        flash(_("Failed to sync team numbers: %(error)s", error=exc), "warning")
        return redirect(url_for("sheets_admin.list_sheets"))

    flash(_("Synced team numbers for checkpoint tabs."), "success")
    return redirect(url_for("sheets_admin.list_sheets"))


@sheets_bp.route("/add-tab", methods=["POST"])
@roles_required("admin")
def add_tab():
    comp_id, redirect_resp = _require_competition()
    if redirect_resp:
        return redirect_resp
    local_only = bool(request.form.get("local_only"))
    spreadsheet_id = (request.form.get("spreadsheet_id") or "").strip()
    use_sheets = sheets_sync_enabled() and not local_only
    tab_title = (request.form.get("tab_title") or "").strip()
    checkpoint_id = request.form.get("checkpoint_id", type=int)
    lang = load_lang()
    arrived_header = (request.form.get("arrived_header") or lang.get("arrived_header") or "Arrived to CP").strip()
    points_header = (request.form.get("points_header") or lang.get("points_header") or "Points").strip()
    dead_time_enabled = bool(request.form.get("dead_time"))
    dead_time_header = (request.form.get("dead_time_header") or lang.get("dead_time_header") or "Dead Time [min]").strip()
    include_time = bool(request.form.get("include_time"))
    time_header = (request.form.get("time_header") or lang.get("time_header") or "Čas").strip()
    groups_raw = request.form.get("groups_raw") or ""
    tab_type = "checkpoint"

    if not tab_title:
        flash(_("Tab title is required."), "warning")
        return redirect(url_for("sheets_admin.list_sheets"))
    if not spreadsheet_id and not use_sheets:
        spreadsheet_id = _local_spreadsheet_id(comp_id)
    elif not spreadsheet_id and use_sheets:
        flash(_("Spreadsheet ID is required."), "warning")
        return redirect(url_for("sheets_admin.list_sheets"))

    groups = _parse_group_fields(groups_raw)
    if not groups:
        flash(_("At least one group line is required."), "warning")
        return redirect(url_for("sheets_admin.list_sheets"))

    # Build headers horizontally for all groups
    headers: List[str] = []
    group_start_cols: List[int] = []
    current_col = 1
    for grp in groups:
        group_start_cols.append(current_col)
        headers.append(grp["name"])
        if dead_time_enabled:
            headers.append(dead_time_header)
        if include_time:
            headers.append(time_header)
        headers.extend(grp.get("fields", []))
        headers.append(points_header)
        current_col += 1 + (1 if dead_time_enabled else 0) + (1 if include_time else 0) + len(grp.get("fields", [])) + 1

    ws_title = None
    if use_sheets:
        try:
            client = _get_sheets_client()
            ws = client.add_tab(spreadsheet_id, tab_title)
            client.set_header_row(spreadsheet_id, tab_title, headers)

            # Populate team numbers under each group header if groups exist
            for grp, start_col in zip(groups, group_start_cols):
                db_group = (
                    CheckpointGroup.query
                    .filter(
                        CheckpointGroup.competition_id == comp_id,
                        func.lower(CheckpointGroup.name) == grp["name"].strip().lower(),
                    )
                    .first()
                )
                if not db_group:
                    continue
                nums = (
                    db.session.query(Team.number)
                    .join(TeamGroup, TeamGroup.team_id == Team.id)
                    .filter(TeamGroup.group_id == db_group.id)
                    .filter(Team.number.isnot(None))
                    .order_by(Team.number.asc())
                    .all()
                )
                values = [n[0] for n in nums if n[0] is not None]
                if values:
                    client.update_column(spreadsheet_id, tab_title, start_col, 2, values)
            ws_title = ws.spreadsheet.title
        except Exception as exc:
            current_app.logger.exception("Failed to add tab")
            msg = _("Could not add tab: %(error)s", error=exc)
            if "PermissionError" in type(exc).__name__ or "permission" in str(exc).lower():
                msg += " - " + _("Check that the spreadsheet ID is correct and that the service account email has Editor access to it.")
            flash(msg, "warning")
            return redirect(url_for("sheets_admin.list_sheets"))

    record = SheetConfig(
        competition_id=comp_id,
        spreadsheet_id=spreadsheet_id,
        spreadsheet_name=ws_title or "Local",
        tab_name=tab_title,
        tab_type=tab_type,
        checkpoint_id=checkpoint_id,
        config={
            "arrived_header": arrived_header,
            "dead_time_enabled": dead_time_enabled,
            "dead_time_header": dead_time_header,
            "time_enabled": include_time,
            "time_header": time_header,
            "points_header": points_header,
            "groups": groups,
        },
    )
    db.session.add(record)
    db.session.commit()
    if not use_sheets:
        flash(_("Added local tab '%(tab)s'.", tab=tab_title), "success")
    else:
        flash(_("Added tab '%(tab)s' to spreadsheet.", tab=tab_title), "success")
    return redirect(url_for("sheets_admin.list_sheets"))
