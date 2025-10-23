# app/blueprints/docs/routes.py
from flask import Blueprint, jsonify, render_template, current_app, send_file
import os

docs_bp = Blueprint("docs", __name__, template_folder="../../templates")

SPEC_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "..", "openapi.yaml")

@docs_bp.route("/openapi.json")
def openapi_json():
    # If you store YAML only, you can convert once at build time; for now just link to YAML.
    return jsonify({"detail": "Use /docs/openapi.yaml or /docs for Swagger UI"})

@docs_bp.route("/openapi.yaml")
def openapi_yaml():
    return send_file(SPEC_PATH, mimetype="text/yaml")

@docs_bp.route("/")
def swagger_ui():
    # Simple Swagger UI via CDN
    return render_template("swagger_ui.html", spec_url="/docs/openapi.yaml")

@docs_bp.route("/redoc")
def redoc_ui():
    return render_template("redoc.html", spec_url="/docs/openapi.yaml")