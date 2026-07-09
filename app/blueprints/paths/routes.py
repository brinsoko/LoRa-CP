# app/blueprints/paths/routes.py
from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _

from app.utils.frontend_api import api_json
from app.utils.perms import roles_required

paths_bp = Blueprint("paths", __name__, template_folder="../../templates")


def _parse_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_checkpoint_ids(values) -> list[int]:
    ids: list[int] = []
    for value in values or []:
        num = _parse_int(value)
        if num and num > 0:
            ids.append(num)
    return ids


def _fetch_checkpoints() -> list[dict]:
    resp, payload = api_json("GET", "/api/checkpoints")
    if resp.status_code != 200:
        flash(_("Could not load checkpoints."), "warning")
        return []
    return payload.get("checkpoints", [])


def _partition_checkpoints(all_checkpoints: list[dict], ordered_ids: list[int]) -> tuple[list[dict], list[dict]]:
    """Split into (selected in order, available). A checkpoint may appear
    more than once in ordered_ids (revisit paths), so 'available' keeps
    every checkpoint; re-adding an already-used one is legal."""
    lookup = {}
    for cp in all_checkpoints:
        cp_id = _parse_int(cp.get("id"))
        if cp_id is not None:
            lookup[cp_id] = cp
    selected = [lookup[cid] for cid in ordered_ids if cid in lookup]
    return selected, list(all_checkpoints)


@paths_bp.route("/", methods=["GET"])
@roles_required("judge", "admin")
def list_paths():
    resp, payload = api_json("GET", "/api/paths")
    if resp.status_code != 200:
        flash(_("Could not load paths."), "warning")
        paths = []
    else:
        paths = payload.get("paths", [])
    return render_template("paths_list.html", paths=paths)


def _render_form(mode: str, path: dict | None, selected: list[dict], available: list[dict]):
    return render_template(
        "path_edit.html",
        mode=mode,
        p=path,
        selected_checkpoints=selected,
        available_checkpoints=available,
    )


@paths_bp.route("/add", methods=["GET", "POST"])
@roles_required("judge", "admin")
def add_path():
    checkpoints = _fetch_checkpoints()
    selected_ids = _parse_checkpoint_ids(request.form.getlist("checkpoint_ids")) if request.method == "POST" else []
    selected_items, available_items = _partition_checkpoints(checkpoints, selected_ids)

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        notes = (request.form.get("notes") or "").strip() or None
        if not name:
            flash(_("Path name is required."), "warning")
            return _render_form("add", None, selected_items, available_items)

        resp, payload = api_json(
            "POST",
            "/api/paths",
            json={"name": name, "notes": notes, "checkpoint_ids": selected_ids},
        )
        if resp.status_code == 201:
            flash(_("Path created."), "success")
            return redirect(url_for("paths.list_paths"))
        flash(payload.get("detail") or payload.get("error") or _("Could not create path."), "warning")

    return _render_form("add", None, selected_items, available_items)


@paths_bp.route("/<int:path_id>/edit", methods=["GET", "POST"])
@roles_required("judge", "admin")
def edit_path(path_id: int):
    resp, path = api_json("GET", f"/api/paths/{path_id}")
    if resp.status_code != 200:
        flash(_("Path not found."), "warning")
        return redirect(url_for("paths.list_paths"))

    checkpoints = _fetch_checkpoints()
    existing_ids = [s.get("checkpoint_id") for s in path.get("stops", [])]
    selected_ids = (
        _parse_checkpoint_ids(request.form.getlist("checkpoint_ids")) if request.method == "POST" else existing_ids
    )
    selected_items, available_items = _partition_checkpoints(checkpoints, selected_ids)

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        notes = (request.form.get("notes") or "").strip() or None
        if not name:
            flash(_("Path name is required."), "warning")
            return _render_form("edit", path, selected_items, available_items)

        resp, payload = api_json(
            "PATCH",
            f"/api/paths/{path_id}",
            json={"name": name, "notes": notes, "checkpoint_ids": selected_ids},
        )
        if resp.status_code == 200:
            flash(_("Path updated."), "success")
            return redirect(url_for("paths.list_paths"))
        flash(payload.get("detail") or payload.get("error") or _("Could not update path."), "warning")
        path["name"] = name
        path["notes"] = notes

    return _render_form("edit", path, selected_items, available_items)


@paths_bp.route("/<int:path_id>/duplicate", methods=["POST"])
@roles_required("judge", "admin")
def duplicate_path(path_id: int):
    reversed_copy = request.form.get("reversed") in ("on", "1", "true", "True")
    resp, payload = api_json(
        "POST",
        f"/api/paths/{path_id}/duplicate",
        json={"reversed": reversed_copy},
    )
    if resp.status_code == 201:
        flash(
            _("Reversed copy created.") if reversed_copy else _("Path duplicated."),
            "success",
        )
    else:
        flash(payload.get("detail") or payload.get("error") or _("Could not duplicate path."), "warning")
    return redirect(url_for("paths.list_paths"))


@paths_bp.route("/<int:path_id>/delete", methods=["POST"])
@roles_required("admin")
def delete_path(path_id: int):
    resp, payload = api_json("DELETE", f"/api/paths/{path_id}")
    if resp.status_code == 200:
        flash(_("Path deleted."), "success")
    else:
        flash(payload.get("detail") or payload.get("error") or _("Could not delete path."), "warning")
    return redirect(url_for("paths.list_paths"))
