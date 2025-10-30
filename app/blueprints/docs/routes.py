# app/blueprints/docs/routes.py
from __future__ import annotations
import os
from flask import Blueprint, jsonify, render_template, send_from_directory, request, abort, current_app

docs_bp = Blueprint("docs", __name__, template_folder="../../templates")

# Short-name -> filename
SPECS = {
    "api": "openapi-api.yaml",
    "web": "openapi-web.yaml",
}

def _docs_dir() -> str:
    # Resolve to <project_root>/docs no matter where the blueprint lives
    # current_app.root_path typically points to <project_root>/app
    return os.path.normpath(os.path.join(current_app.root_path, "..", "docs"))

def _ensure_exists_or_404(spec_name: str) -> tuple[str, str]:
    fname = SPECS.get(spec_name)
    if not fname:
        abort(404, f"Unknown spec '{spec_name}'.")
    docs_dir = _docs_dir()
    fpath = os.path.join(docs_dir, fname)
    if not os.path.isfile(fpath):
        abort(404, f"Spec '{spec_name}' not found at {fpath}")
    return docs_dir, fname

@docs_bp.route("/specs", methods=["GET"])
def list_specs():
    docs_dir = _docs_dir()
    available = []
    for key, fname in SPECS.items():
        if os.path.isfile(os.path.join(docs_dir, fname)):
            available.append({"name": key, "filename": fname, "url": f"/docs/{key}.yaml"})
    return jsonify({"available": available})

@docs_bp.route("/<spec_name>.yaml", methods=["GET"])
def serve_spec_yaml(spec_name: str):
    docs_dir, fname = _ensure_exists_or_404(spec_name)
    return send_from_directory(docs_dir, fname, mimetype="text/yaml")

@docs_bp.route("/openapi.yaml", methods=["GET"])
def openapi_yaml_default():
    # Back-compat: default to API spec
    docs_dir, fname = _ensure_exists_or_404("api")
    return send_from_directory(docs_dir, fname, mimetype="text/yaml")

@docs_bp.route("/openapi.json", methods=["GET"])
def openapi_json_hint():
    return jsonify({"detail": "Use /docs?spec=api or /docs?spec=web (and /docs/api.yaml or /docs/web.yaml)"}), 200

@docs_bp.route("/", methods=["GET"])
def swagger_ui():
    spec = (request.args.get("spec") or "api").strip().lower()
    if spec not in SPECS:
        abort(404, f"Unknown spec '{spec}'. Try one of: {', '.join(SPECS.keys())}")
    # Just pass the URL that maps to the route above
    return render_template("swagger_ui.html", spec_url=f"/docs/{spec}.yaml", active_spec=spec)