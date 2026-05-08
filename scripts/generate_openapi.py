#!/usr/bin/env python
"""Sync docs/openapi.json with the live Flask url_map.

What this does:
- Walks the application's url_map and collects every route under /api/...
  plus /health and /ready (the documented operator probes).
- Reads the existing docs/openapi.json to preserve schemas, component
  definitions, and any hand-written summaries/descriptions that we don't
  want to lose on each regeneration.
- For each wired route, ensures the spec has an entry with the correct
  HTTP methods and path parameters; drops any path in the spec that no
  longer corresponds to a wired route.
- Writes the updated spec back out, sorted, with stable formatting.

What this does NOT do:
- Infer request/response schemas from view function signatures. The
  payload schemas in components/schemas were hand-written and continue
  to be hand-written. This script only keeps the path index in sync;
  detailed payload work is still manual.
- Produce a strict spec that validates requests. For that we'd switch to
  flask-smorest or apispec — see docs/openapi.md for the long-term plan.

Usage:
    make openapi              # regenerate docs/openapi.json
    make openapi-check        # exit nonzero if regenerating would change anything (CI)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Iterable

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

SPEC_PATH = ROOT / "docs" / "openapi.json"

INCLUDED_PREFIXES = ("/api/", "/health", "/ready")

# Maps Werkzeug URL converter names to OpenAPI types.
PARAM_TYPE_MAP = {
    "int": ("integer", "int32"),
    "float": ("number", None),
    "string": ("string", None),
    "path": ("string", None),
    "uuid": ("string", "uuid"),
}


def _build_test_app():
    """Create the Flask app with a minimal test config so we can read its
    url_map without needing a real DB or secrets."""
    from app import create_app

    return create_app({
        "TESTING": True,
        "SQLALCHEMY_DATABASE_URI": "sqlite:///:memory:",
        "_EPHEMERAL_TEST_DB": True,
        "WTF_CSRF_ENABLED": False,
        "SECRET_KEY": "openapi-gen",
        "DEVICE_CARD_SECRET": "openapi-gen",
        "SERVER_NAME": "localhost",
        "GOOGLE_OAUTH_CLIENT_ID": None,
        "GOOGLE_OAUTH_CLIENT_SECRET": None,
        "LORA_WEBHOOK_SECRET": "CHANGE_LATER",
        "RATELIMIT_ENABLED": False,
        "SHEETS_SYNC_INLINE": True,
    })


_PARAM_RE = re.compile(r"<(?:(?P<conv>[^:>]+):)?(?P<name>[^>]+)>")
_NON_ALNUM_RE = re.compile(r"[^a-zA-Z0-9]+")


def _spec_path(rule: str) -> str:
    """Convert /api/teams/<int:team_id> to /api/teams/{team_id}."""
    return _PARAM_RE.sub(lambda m: "{" + m.group("name") + "}", rule)


def _operation_id(verb: str, spec_path: str) -> str:
    """Derive a unique operationId from verb + path.

    Different routes that share an endpoint name (e.g. /api/devices and
    /api/lora/devices, both registered to lora_device_list) must get
    distinct operationIds — OpenAPI 3.0 requires global uniqueness.
    """
    slug = _NON_ALNUM_RE.sub("_", spec_path).strip("_")
    return f"{verb}_{slug}"


def _path_parameters(rule: str) -> list[dict]:
    """Extract OpenAPI path-parameter entries from the rule string."""
    params = []
    for m in _PARAM_RE.finditer(rule):
        conv = (m.group("conv") or "string").lower()
        type_, fmt = PARAM_TYPE_MAP.get(conv, ("string", None))
        schema: dict = {"type": type_}
        if fmt:
            schema["format"] = fmt
        params.append({
            "name": m.group("name"),
            "in": "path",
            "required": True,
            "schema": schema,
        })
    return params


def _collect_routes(app) -> dict[str, dict[str, dict]]:
    """Return {spec_path: {method_lower: {summary?, parameters?}}}."""
    out: dict[str, dict[str, dict]] = {}
    for rule in app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        if not rule.rule.startswith(INCLUDED_PREFIXES):
            continue
        methods = {m for m in rule.methods if m not in {"HEAD", "OPTIONS"}}
        if not methods:
            continue
        path = _spec_path(rule.rule)
        path_entry = out.setdefault(path, {})
        for method in methods:
            verb = method.lower()
            parameters = _path_parameters(rule.rule)
            path_entry[verb] = {
                "endpoint": rule.endpoint,
                "parameters": parameters,
            }
    return out


def _merge(existing: dict, wired: dict[str, dict[str, dict]]) -> dict:
    """Update `existing` paths in-place with `wired` info while preserving
    every hand-written summary/description/requestBody/responses block."""
    spec = json.loads(json.dumps(existing))  # deep copy
    paths = spec.setdefault("paths", {})

    # Drop paths that are no longer wired.
    for orphan in [p for p in paths if p not in wired]:
        del paths[orphan]

    # Sync each wired path.
    for path, methods in wired.items():
        existing_path = paths.setdefault(path, {})

        # Preserve existing path-level keys (parameters, description), but
        # drop methods that are no longer wired.
        for verb in list(existing_path):
            if verb in {"summary", "description", "parameters", "servers"}:
                continue
            if verb.lower() not in methods:
                del existing_path[verb]

        # Ensure each wired verb has an entry.
        for verb_lower, info in methods.items():
            entry = existing_path.setdefault(verb_lower, {})
            # Merge parameters: keep any existing query/header params, replace
            # path params from the live URL rule (source of truth).
            existing_params = [
                p for p in entry.get("parameters", [])
                if isinstance(p, dict) and p.get("in") != "path"
            ]
            entry["parameters"] = info["parameters"] + existing_params

            # operationId must be globally unique per OpenAPI 3.0; derive
            # from verb + path so aliased endpoints get distinct IDs.
            entry["operationId"] = _operation_id(verb_lower, path)
            entry.setdefault("summary", "")
            entry.setdefault("responses", {
                "default": {"description": ""},
            })

    # Sort paths for stable diffs.
    spec["paths"] = dict(sorted(paths.items()))
    return spec


def _load_spec() -> dict:
    if SPEC_PATH.is_file():
        return json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    # Bare-bones starter spec.
    return {
        "openapi": "3.0.3",
        "info": {"title": "LoRa KT API", "version": "v1"},
        "paths": {},
        "components": {"schemas": {}},
    }


def _emit(spec: dict) -> str:
    return json.dumps(spec, indent=2, ensure_ascii=False, sort_keys=False) + "\n"


def main(argv: Iterable[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="Exit nonzero if regeneration would change docs/openapi.json.",
    )
    args = parser.parse_args(list(argv))

    app = _build_test_app()
    with app.app_context():
        wired = _collect_routes(app)
    existing = _load_spec()
    merged = _merge(existing, wired)
    new_text = _emit(merged)
    old_text = SPEC_PATH.read_text(encoding="utf-8") if SPEC_PATH.is_file() else ""

    if args.check:
        if new_text == old_text:
            print("openapi.json is up to date")
            return 0
        print("openapi.json is out of sync; run `make openapi`", file=sys.stderr)
        return 1

    SPEC_PATH.write_text(new_text, encoding="utf-8")
    print(f"wrote {SPEC_PATH.relative_to(ROOT)} ({len(merged['paths'])} paths)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
