# app/blueprints/groups/routes.py
from __future__ import annotations

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_babel import gettext as _

from app.utils.frontend_api import api_json
from app.utils.perms import roles_required

groups_bp = Blueprint("groups", __name__, template_folder="../../templates")


def _parse_int(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fetch_paths() -> list[dict]:
    resp, payload = api_json("GET", "/api/paths")
    if resp.status_code != 200:
        flash(_("Could not load paths."), "warning")
        return []
    return payload.get("paths", [])


def _form_values() -> dict:
    direction = (request.form.get("direction") or "forward").strip().lower()
    return {
        "name": (request.form.get("name") or "").strip(),
        "prefix": (request.form.get("prefix") or "").strip() or None,
        "description": (request.form.get("description") or "").strip() or None,
        "path_id": _parse_int(request.form.get("path_id")),
        "direction": direction if direction in ("forward", "reverse") else "forward",
    }


@groups_bp.route("/", methods=["GET"])
@roles_required("judge", "admin")
def list_groups():
    resp, payload = api_json("GET", "/api/groups")
    if resp.status_code != 200:
        flash(_("Could not load groups."), "warning")
        groups = []
    else:
        groups = payload.get("groups", [])
    return render_template("groups_list.html", groups=groups)


@groups_bp.route("/add", methods=["GET", "POST"])
@roles_required("judge", "admin")
def add_group():
    paths = _fetch_paths()

    if request.method == "POST":
        values = _form_values()
        if not values["name"]:
            flash(_("Group name is required."), "warning")
            return render_template("group_edit.html", mode="add", g=values, paths=paths)

        resp, payload = api_json("POST", "/api/groups", json=values)
        if resp.status_code == 201:
            flash(_("Group created."), "success")
            return redirect(url_for("groups.list_groups"))
        flash(payload.get("detail") or payload.get("error") or _("Could not create group."), "warning")
        return render_template("group_edit.html", mode="add", g=values, paths=paths)

    return render_template("group_edit.html", mode="add", g=None, paths=paths)


@groups_bp.route("/<int:group_id>/edit", methods=["GET", "POST"])
@roles_required("judge", "admin")
def edit_group(group_id: int):
    group_resp, group = api_json("GET", f"/api/groups/{group_id}")
    if group_resp.status_code != 200:
        flash(_("Group not found."), "warning")
        return redirect(url_for("groups.list_groups"))

    paths = _fetch_paths()

    if request.method == "POST":
        values = _form_values()
        if not values["name"]:
            flash(_("Group name is required."), "warning")
            group.update(values)
            return render_template("group_edit.html", mode="edit", g=group, paths=paths)

        resp, payload = api_json("PATCH", f"/api/groups/{group_id}", json=values)
        if resp.status_code == 200:
            flash(_("Group updated."), "success")
            return redirect(url_for("groups.list_groups"))
        flash(payload.get("detail") or payload.get("error") or _("Could not update group."), "warning")
        group.update(values)

    return render_template("group_edit.html", mode="edit", g=group, paths=paths)


@groups_bp.route("/<int:group_id>/delete", methods=["POST"])
@roles_required("admin")
def delete_group(group_id: int):
    resp, payload = api_json("DELETE", f"/api/groups/{group_id}")
    if resp.status_code == 200:
        flash(_("Group deleted."), "success")
    else:
        flash(payload.get("detail") or payload.get("error") or _("Could not delete group."), "warning")
    return redirect(url_for("groups.list_groups"))


@groups_bp.route("/set_active", methods=["POST"])
@roles_required("judge", "admin")
def set_active_group_for_team():
    team_id = request.form.get("team_id")
    group_id = request.form.get("group_id")

    if not team_id or not group_id:
        flash(_("team_id and group_id are required."), "warning")
        return redirect(url_for("groups.list_groups"))

    parsed_group_id = _parse_int(group_id)
    if parsed_group_id is None:
        flash(_("group_id must be an integer."), "warning")
        return redirect(url_for("groups.list_groups"))

    resp, payload = api_json(
        "POST",
        f"/api/teams/{team_id}/active-group",
        json={"group_id": parsed_group_id},
    )

    if resp.status_code == 200:
        flash(_("Active group updated."), "success")
    else:
        flash(payload.get("detail") or payload.get("error") or _("Could not set active group."), "warning")
    return redirect(url_for("groups.list_groups"))
