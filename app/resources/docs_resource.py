# app/resources/docs_resource.py
from __future__ import annotations

import json
import os

from flask import Blueprint, current_app

docs_api_bp = Blueprint("api_docs", __name__)


def _docs_dir() -> str:
    return os.path.normpath(os.path.join(current_app.root_path, "..", "docs"))


@docs_api_bp.get("/api/docs")
def api_docs_list():
        docs_dir = _docs_dir()
        specs = []
        for name in ("openapi.json", "openapi.yaml"):
            path = os.path.join(docs_dir, name)
            if os.path.isfile(path):
                specs.append({"name": name, "path": f"/api/docs/{name}"})
        return {"specs": specs}, 200


@docs_api_bp.get("/api/docs/<string:filename>")
def api_spec(filename: str):
        docs_dir = _docs_dir()
        safe_name = filename.replace("..", "")
        path = os.path.join(docs_dir, safe_name)
        if not os.path.isfile(path):
            return {"error": "not_found"}, 404
        if safe_name.endswith(".json"):
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f), 200
        with open(path, "r", encoding="utf-8") as f:
            return f.read(), 200, {"Content-Type": "text/plain; charset=utf-8"}
