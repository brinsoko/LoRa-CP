# app/resources/docs_resource.py
from __future__ import annotations

import json
import os

from flask import current_app
from flask_restful import Resource


def _docs_dir() -> str:
    return os.path.normpath(os.path.join(current_app.root_path, "..", "docs"))


class ApiDocsListResource(Resource):
    def get(self):
        docs_dir = _docs_dir()
        specs = []
        for name in ("openapi.json", "openapi.yaml"):
            path = os.path.join(docs_dir, name)
            if os.path.isfile(path):
                specs.append({"name": name, "path": f"/api/docs/{name}"})
        return {"specs": specs}, 200


class ApiSpecResource(Resource):
    def get(self, filename: str):
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
