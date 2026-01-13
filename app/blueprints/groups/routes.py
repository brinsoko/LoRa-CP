# app/blueprints/groups/routes.py
from __future__ import annotations

from typing import List, Tuple

from flask import Blueprint, render_template, request, redirect, url_for, flash

from app.utils.frontend_api import api_json
from app.utils.perms import roles_required


groups_bp = Blueprint("groups", __name__, template_folder="../../templates")


def _parse_checkpoint_ids(values) -> List[int]:
    ids: List[int] = []
    for value in values or []:
        try:
            num = int(value)
            if num > 0:
                ids.append(num)
        except Exception:
            continue
    return ids


def _partition_checkpoints(all_checkpoints: List[dict], ordered_ids: List[int]) -> Tuple[List[dict], List[dict]]:
    lookup = {int(cp.get("id")): cp for cp in all_checkpoints}
    selected: List[dict] = []
    for cid in ordered_ids:
        cp = lookup.get(cid)
        if cp:
            selected.append(cp)
    selected_ids = {int(cp.get("id")) for cp in selected}
    available = [cp for cp in all_checkpoints if int(cp.get("id")) not in selected_ids]
    return selected, available


def _fetch_checkpoints() -> List[dict]:
    resp, payload = api_json("GET", "/api/checkpoints")
    if resp.status_code != 200:
        flash("Could not load checkpoints.", "warning")
        return []
    return payload.get("checkpoints", [])


@groups_bp.route("/", methods=["GET"])
@roles_required("judge", "admin")
def list_groups():
    resp, payload = api_json("GET", "/api/groups")
    if resp.status_code != 200:
        flash("Could not load groups.", "warning")
        groups = []
    else:
        groups = payload.get("groups", [])
    return render_template("groups_list.html", groups=groups)


@groups_bp.route("/add", methods=["GET", "POST"])
@roles_required("judge", "admin")
def add_group():
    checkpoints = _fetch_checkpoints()

    selected_ids = _parse_checkpoint_ids(request.form.getlist("checkpoint_ids")) if request.method == "POST" else []
    selected_items, available_items = _partition_checkpoints(checkpoints, selected_ids)

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        prefix = (request.form.get("prefix") or "").strip() or None
        desc = (request.form.get("description") or "").strip() or None

        if not name:
            flash("Group name is required.", "warning")
            return render_template(
                "group_edit.html",
                mode="add",
                g=None,
                selected_ids=selected_ids,
                selected_checkpoints=selected_items,
                available_checkpoints=available_items,
            )

        resp, payload = api_json(
            "POST",
            "/api/groups",
            json={
                "name": name,
                "prefix": prefix,
                "description": desc,
                "checkpoint_ids": selected_ids,
            },
        )

        if resp.status_code == 201:
            flash("Group created.", "success")
            return redirect(url_for("groups.list_groups"))

        flash(payload.get("error") or payload.get("detail") or "Could not create group.", "warning")

    return render_template(
        "group_edit.html",
        mode="add",
        g=None,
        selected_ids=selected_ids,
        selected_checkpoints=selected_items,
        available_checkpoints=available_items,
    )


@groups_bp.route("/<int:group_id>/edit", methods=["GET", "POST"])
@roles_required("judge", "admin")
def edit_group(group_id: int):
    group_resp, group_payload = api_json("GET", f"/api/groups/{group_id}")
    if group_resp.status_code != 200:
        flash("Group not found.", "warning")
        return redirect(url_for("groups.list_groups"))

    group = group_payload
    checkpoints = _fetch_checkpoints()

    existing_ids = [cp.get("id") for cp in group.get("checkpoints", [])]
    selected_ids = _parse_checkpoint_ids(request.form.getlist("checkpoint_ids")) if request.method == "POST" else existing_ids
    selected_items, available_items = _partition_checkpoints(checkpoints, selected_ids)

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        prefix = (request.form.get("prefix") or "").strip() or None
        desc = (request.form.get("description") or "").strip() or None

        if not name:
            flash("Group name is required.", "warning")
            group["name"] = name
            group["description"] = desc
            group["checkpoints"] = [
                {"id": cp.get("id"), "name": cp.get("name"), "position": idx}
                for idx, cp in enumerate(selected_items)
            ]
            return render_template(
                "group_edit.html",
                mode="edit",
                g=group,
                selected_ids=selected_ids,
                selected_checkpoints=selected_items,
                available_checkpoints=available_items,
            )

        resp, payload = api_json(
            "PATCH",
            f"/api/groups/{group_id}",
            json={
                "name": name,
                "prefix": prefix,
                "description": desc,
                "checkpoint_ids": selected_ids,
            },
        )

        if resp.status_code == 200:
            flash("Group updated.", "success")
            return redirect(url_for("groups.list_groups"))

        flash(payload.get("error") or payload.get("detail") or "Could not update group.", "warning")
        group["name"] = name
        group["prefix"] = prefix
        group["description"] = desc
        group["checkpoints"] = [
            {"id": cp.get("id"), "name": cp.get("name"), "position": idx}
            for idx, cp in enumerate(selected_items)
        ]

    else:
        group["checkpoints"] = [
            {"id": cp.get("id"), "name": cp.get("name"), "position": idx}
            for idx, cp in enumerate(selected_items)
        ]

    return render_template(
        "group_edit.html",
        mode="edit",
        g=group,
        selected_ids=selected_ids,
        selected_checkpoints=selected_items,
        available_checkpoints=available_items,
    )


@groups_bp.route("/<int:group_id>/delete", methods=["POST"])
@roles_required("admin")
def delete_group(group_id: int):
    resp, payload = api_json("DELETE", f"/api/groups/{group_id}")
    if resp.status_code == 200:
        flash("Group deleted.", "success")
    else:
        flash(payload.get("detail") or payload.get("error") or "Could not delete group.", "warning")
    return redirect(url_for("groups.list_groups"))


@groups_bp.route("/set_active", methods=["POST"])
@roles_required("judge", "admin")
def set_active_group_for_team():
    team_id = request.form.get("team_id")
    group_id = request.form.get("group_id")

    if not team_id or not group_id:
        flash("team_id and group_id are required.", "warning")
        return redirect(url_for("groups.list_groups"))

    resp, payload = api_json(
        "POST",
        f"/api/teams/{team_id}/active-group",
        json={"group_id": int(group_id)},
    )

    if resp.status_code == 200:
        flash("Active group updated.", "success")
    else:
        flash(payload.get("detail") or payload.get("error") or "Could not set active group.", "warning")
    return redirect(url_for("groups.list_groups"))
