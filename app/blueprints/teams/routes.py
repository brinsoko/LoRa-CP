# app/blueprints/teams/routes.py
from __future__ import annotations

from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_babel import gettext as _

from app.utils.frontend_api import api_json
from app.utils.competition import get_current_competition_role
from app.utils.perms import roles_required

teams_bp = Blueprint("teams", __name__, template_folder="../../templates")


def _tr(msg: str) -> str:
    try:
        return _(msg)
    except Exception:
        return msg


def _safe_next_url(default: str):
    next_url = (request.form.get("next") or request.args.get("next") or "").strip()
    if next_url.startswith("/") and not next_url.startswith("//"):
        return next_url
    return default

def _load_organizations() -> list[str]:
    resp, payload = api_json("GET", "/api/teams")
    if resp.status_code != 200:
        return []
    orgs = []
    seen = set()
    for team in payload.get("teams", []):
        org = (team.get("organization") or "").strip()
        if not org or org in seen:
            continue
        seen.add(org)
        orgs.append(org)
    return sorted(orgs)


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
    sort = (request.args.get("sort") or "number_asc").strip().lower()

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
    organizations = _load_organizations()

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        number_raw = request.form.get("number")
        number = int(number_raw) if number_raw else None
        organization = (request.form.get("organization") or "").strip() or None
        selected_group_id = request.form.get("group_id", type=int)
        rfid_uid = (request.form.get("rfid_uid") or "").strip().upper()
        rfid_number_raw = request.form.get("rfid_number")
        rfid_number = int(rfid_number_raw) if rfid_number_raw else None

        if not name:
            flash("Team name is required.", "warning")
            return render_template(
                "add_team.html",
                groups=groups,
                selected_group_id=selected_group_id,
                organizations=organizations,
            )

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
            team_id = payload.get("team", {}).get("id")
            if rfid_uid and team_id:
                rfid_resp, rfid_payload = api_json(
                    "POST",
                    "/api/rfid/cards",
                    json={"uid": rfid_uid, "team_id": team_id, "number": rfid_number},
                )
                if rfid_resp.status_code == 201:
                    flash(_tr("RFID mapping created."), "success")
                else:
                    flash(rfid_payload.get("detail") or rfid_payload.get("error") or _tr("Could not create RFID mapping."), "warning")
            flash("Team created.", "success")
            return redirect(url_for("teams.list_teams"))

        flash(payload.get("error") or "Could not create team.", "warning")

    return render_template(
        "add_team.html",
        groups=groups,
        selected_group_id=selected_group_id,
        organizations=organizations,
    )


@teams_bp.route("/<int:team_id>/edit", methods=["GET", "POST"])
@roles_required("judge", "admin")
def edit_team(team_id: int):
    next_url = (request.args.get("next") or request.form.get("next") or "").strip()
    team_resp, team_payload = api_json("GET", f"/api/teams/{team_id}")
    if team_resp.status_code != 200:
        flash("Team not found.", "warning")
        return redirect(url_for("teams.list_teams"))

    team = _transform_team_payload(team_payload.get("team", team_payload))

    _, groups_payload = api_json("GET", "/api/groups")
    groups = groups_payload.get("groups", [])

    rfid_card = None
    cards_resp, cards_payload = api_json("GET", "/api/rfid/cards")
    if cards_resp.status_code == 200:
        for c in cards_payload.get("cards", []):
            t = c.get("team") or {}
            if t.get("id") == team.get("id"):
                rfid_card = c
                break
    else:
        flash(_tr("Could not load RFID mappings."), "warning")

    selected_group_id = next((g.get("group", {}).get("id") for g in team.get("group_assignments", []) if g.get("group")), None)
    organizations = _load_organizations()

    if request.method == "POST":
        name = (request.form.get("name") or "").strip()
        number_raw = request.form.get("number")
        number = int(number_raw) if number_raw else None
        organization = (request.form.get("organization") or "").strip() or None
        selected_group_id = request.form.get("group_id", type=int)
        rfid_uid = (request.form.get("rfid_uid") or "").strip().upper()
        rfid_number_raw = request.form.get("rfid_number")
        rfid_number = int(rfid_number_raw) if rfid_number_raw else None

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
                organizations=organizations,
                next_url=next_url,
            )

        update_payload = {
            "name": name,
            "number": number,
            "organization": organization,
            "group_id": selected_group_id,
        }
        if (get_current_competition_role() or "") == "admin":
            update_payload["dnf"] = bool(request.form.get("dnf"))

        resp, payload = api_json(
            "PATCH",
            f"/api/teams/{team_id}",
            json=update_payload,
        )

        if resp.status_code == 200:
            if rfid_uid or rfid_number is not None or rfid_card:
                # Update existing mapping or create a new one
                if rfid_card:
                    rfid_resp, rfid_payload = api_json(
                        "PATCH",
                        f"/api/rfid/cards/{rfid_card.get('id')}",
                        json={
                            "uid": rfid_uid or rfid_card.get("uid"),
                            "team_id": team_id,
                            "number": rfid_number,
                        },
                    )
                elif rfid_uid:
                    rfid_resp, rfid_payload = api_json(
                        "POST",
                        "/api/rfid/cards",
                        json={"uid": rfid_uid, "team_id": team_id, "number": rfid_number},
                    )
                else:
                    rfid_resp, rfid_payload = None, {}

                if rfid_resp:
                    if rfid_resp.status_code in (200, 201):
                        flash(_tr("RFID mapping saved."), "success")
                    else:
                        flash(rfid_payload.get("detail") or rfid_payload.get("error") or _tr("Could not save RFID mapping."), "warning")

            flash("Team updated.", "success")
            return redirect(_safe_next_url(url_for("teams.list_teams")))

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
        team["dnf"] = bool(request.form.get("dnf"))

    return render_template(
        "team_edit.html",
        team=team,
        groups=groups,
        rfid_card=rfid_card,
        selected_group_id=selected_group_id,
        organizations=organizations,
        next_url=next_url,
    )


@teams_bp.route("/<int:team_id>/delete", methods=["POST"])
@roles_required("admin")
def delete_team(team_id: int):
    force = (request.form.get("force") or "").strip() == "1"
    confirm_text = (request.form.get("confirm_text") or "").strip()
    json_payload = None
    if force or confirm_text:
        json_payload = {"force": force, "confirm_text": confirm_text}
    resp, payload = api_json("DELETE", f"/api/teams/{team_id}", json=json_payload)

    if resp.status_code == 200:
        flash("Team deleted.", "success")
    else:
        flash(payload.get("detail") or payload.get("error") or "Could not delete team.", "warning")

    return redirect(_safe_next_url(url_for("teams.list_teams")))


@teams_bp.route("/randomize", methods=["POST"])
@roles_required("judge", "admin")
def randomize_numbers():
    group_id = (request.form.get("group_id") or "").strip()
    payload = {}
    if group_id:
        payload["group_id"] = group_id

    resp, data = api_json("POST", "/api/teams/randomize", json=payload)
    if resp.status_code != 200:
        flash(data.get("detail") or data.get("error") or "Could not randomize team numbers.", "warning")
        return redirect(_safe_next_url(url_for("teams.list_teams")))

    assigned_total = data.get("assigned_total", 0)
    if assigned_total:
        flash(f"Randomized team numbers. Assigned {assigned_total}.", "success")
    else:
        flash("No team numbers were assigned.", "info")

    for res in data.get("results", []):
        status = res.get("status")
        name = res.get("group_name") or f"Group {res.get('group_id')}"
        if status == "insufficient_numbers":
            flash(f"{name}: not enough numbers in range.", "warning")
        elif status == "skipped":
            flash(f"{name}: invalid or missing prefix.", "warning")

    return redirect(_safe_next_url(url_for("teams.list_teams")))
