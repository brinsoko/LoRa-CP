# app/blueprints/docs/routes.py
from __future__ import annotations

from flask import Blueprint, jsonify, render_template, request, abort, current_app

from app.utils.frontend_api import api_json, api_request


docs_bp = Blueprint("docs", __name__, template_folder="../../templates")


@docs_bp.route("/specs", methods=["GET"])
def list_specs():
    resp, payload = api_json("GET", "/api/docs")
    return jsonify(payload), resp.status_code


@docs_bp.route("/openapi.json", methods=["GET"])
def openapi_json_proxy():
    resp = api_request("GET", "/api/docs/openapi.json")
    flask_resp = current_app.response_class(resp.get_data(), status=resp.status_code)
    for header, value in resp.headers.items():
        if header.lower() in {"content-type", "content-length", "last-modified"}:
            flask_resp.headers[header] = value
    return flask_resp


@docs_bp.route("/", methods=["GET"])
def swagger_ui():
    resp, payload = api_json("GET", "/api/docs")
    if resp.status_code != 200 or not payload.get("specs"):
        abort(502, "Documentation service unavailable")

    specs = payload.get("specs", [])
    requested = (request.args.get("spec") or "openapi.json").strip()

    selected = None
    for spec in specs:
        name = spec.get("name") or ""
        path = spec.get("path") or ""
        if requested and (requested == name or requested == path):
            selected = spec
            break

    if selected is None:
        selected = next((s for s in specs if (s.get("name") or "").endswith(".json")), specs[0])

    spec_url = selected.get("path") or "/api/docs/openapi.json"
    active_name = selected.get("name") or spec_url

    return render_template(
        "swagger_ui.html",
        spec_url=spec_url,
        active_spec=active_name,
        specs=specs,
    )
