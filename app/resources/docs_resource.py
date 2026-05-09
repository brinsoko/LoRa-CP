# app/resources/docs_resource.py
from __future__ import annotations

import json
import os

from flask import Blueprint, current_app
from werkzeug.utils import safe_join

from app.utils.rest_auth import json_login_required

docs_api_bp = Blueprint("api_docs", __name__)


def _docs_dir() -> str:
    return os.path.normpath(os.path.join(current_app.root_path, "..", "docs"))


@docs_api_bp.get("/api/docs")
@json_login_required
def api_docs_list():
    docs_dir = _docs_dir()
    specs = []
    for name in ("openapi.json", "openapi.yaml"):
        path = os.path.join(docs_dir, name)
        if os.path.isfile(path):
            specs.append({"name": name, "path": f"/api/docs/{name}"})
    return {"specs": specs}, 200


@docs_api_bp.get("/api/docs/<string:filename>")
@json_login_required
def api_spec(filename: str):
    docs_dir = _docs_dir()
    path = safe_join(docs_dir, filename)
    if path is None or not os.path.isfile(path):
        return {"error": "not_found"}, 404
    if filename.endswith(".json"):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f), 200
    with open(path, "r", encoding="utf-8") as f:
        return f.read(), 200, {"Content-Type": "text/plain; charset=utf-8"}
