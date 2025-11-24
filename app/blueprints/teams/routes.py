# app/blueprints/teams/routes.py
from __future__ import annotations

from flask import Blueprint, render_template, request, redirect, url_for, flash

from app.utils.frontend_api import api_json
from app.utils.perms import roles_required

teams_bp = Blueprint("teams", __name__, template_folder="../../templates")


def _transform_team_payload(team: dict) -> dict:
    assignments = []
    for grp in team.get("groups", []):
        assignments.append({
            "group": {"id": grp.get("id"), "name": grp.get("name")},
            "active": grp.get("active", False),
        })
    team["group_assignments"] = assignments
    return team


@teams_bp.route("/", methods=["GET"])
def list_teams():
    q = (request.args.get("q") or "").strip()
    group_id = request.args.get("group_id")
    sort = (request.args.get("sort") or "name_asc").strip().lower()

    params = {"sort": sort}
    if q:
        params["q"] = q
    if group_id:
        params["group_id"] = group_id

    team_resp, team_payload = api_json("GET", "/api/teams", params=params)
    groups_resp, groups_payload = api_json("GET", "/api/groups")

    if team_resp.status_code != 200:
        flash("Could not load teams.", "warning")
    teams = [_transform_team_payload(t) for t in team_payload.get("teams", [])]

    if groups_resp.status_code != 200:
        flash("Could not load groups.", "warning")
    groups = groups_payload.get("groups", [])

    selected_group_id = int(group_id) if group_id else None

    return render_template(
        "teams_list.html",
        teams=teams,
        groups=groups,
        selected_q=q,
        selected_group_id=selected_group_id,
        selected_sort=sort,
    )


@teams_bp.route("/add", methods=["GET", "POST"])
@roles_required("judge", "admin")
def add_team():
    _, groups_payload = api_json("GET", "/api/groups")
    groups = groups_payload.get("groups", [])
    selected_group_id = None

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        number_raw = request.form.get("number")
        number = int(number_raw) if number_raw else None
        organization = (request.form.get("organization") or "").strip() or None
        selected_group_id = request.form.get("group_id", type=int)

        if not name:
            flash("Team name is required.", "warning")
            return render_template("add_team.html", groups=groups, selected_group_id=selected_group_id)

        resp, payload = api_json(
            "POST",
            "/api/teams",
            json={
                "name": name,
                "number": number,
                "organization": organization,
                "group_id": selected_group_id,
            },
        )

        if resp.status_code == 201:
            flash("Team created.", "success")
            return redirect(url_for("teams.list_teams"))

        flash(payload.get("error") or "Could not create team.", "warning")

    return render_template("add_team.html", groups=groups, selected_group_id=selected_group_id)


@teams_bp.route("/<int:team_id>/edit", methods=["GET", "POST"])
@roles_required("judge", "admin")
def edit_team(team_id: int):
    team_resp, team_payload = api_json("GET", f"/api/teams/{team_id}")
    if team_resp.status_code != 200:
        flash("Team not found.", "warning")
        return redirect(url_for("teams.list_teams"))

    team = _transform_team_payload(team_payload.get("team", team_payload))

    _, groups_payload = api_json("GET", "/api/groups")
    groups = groups_payload.get("groups", [])

    selected_group_id = next((g.get("group", {}).get("id") for g in team.get("group_assignments", []) if g.get("group")), None)

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        number_raw = request.form.get("number")
        number = int(number_raw) if number_raw else None
        organization = (request.form.get("organization") or "").strip() or None
        selected_group_id = request.form.get("group_id", type=int)

        if not name:
            flash("Team name is required.", "warning")
            team["name"] = name
            team["number"] = number
            team["organization"] = organization
            return render_template(
                "team_edit.html",
                team=team,
                groups=groups,
                selected_group_id=selected_group_id,
            )

        resp, payload = api_json(
            "PATCH",
            f"/api/teams/{team_id}",
            json={
                "name": name,
                "number": number,
                "organization": organization,
                "group_id": selected_group_id,
            },
        )

        if resp.status_code == 200:
            flash("Team updated.", "success")
            return redirect(url_for("teams.list_teams"))

        flash(payload.get("error") or "Could not update team.", "warning")
        team["name"] = name
        team["number"] = number
        grp = next((g for g in groups if g.get("id") == selected_group_id), None)
        if grp:
            team["group_assignments"] = [{
                "group": {"id": grp.get("id"), "name": grp.get("name")},
                "active": True,
            }]
        else:
            team["group_assignments"] = []

    return render_template(
        "team_edit.html",
        team=team,
        groups=groups,
        selected_group_id=selected_group_id,
    )


@teams_bp.route("/<int:team_id>/delete", methods=["POST"])
@roles_required("admin")
def delete_team(team_id: int):
    resp, payload = api_json("DELETE", f"/api/teams/{team_id}")

    if resp.status_code == 200:
        flash("Team deleted.", "success")
    else:
        flash(payload.get("detail") or payload.get("error") or "Could not delete team.", "warning")

    return redirect(url_for("teams.list_teams"))
