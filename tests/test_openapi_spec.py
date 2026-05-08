"""Sanity checks on docs/openapi.json that catch regressions in
scripts/generate_openapi.py.

These run cheaply on every test invocation, so a future change to the
generator that emits a malformed spec is caught at PR time, not at
deploy time.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

SPEC_PATH = Path(__file__).resolve().parents[1] / "docs" / "openapi.json"


@pytest.fixture(scope="module")
def spec():
    return json.loads(SPEC_PATH.read_text(encoding="utf-8"))


def test_spec_is_openapi_3(spec):
    assert spec["openapi"].startswith("3.")
    assert spec["paths"], "spec has no paths"


def test_operation_ids_are_globally_unique(spec):
    """OpenAPI 3.0 §4.7.10.4: operationId must be unique across all operations."""
    seen = []
    for path, methods in spec["paths"].items():
        for verb, entry in methods.items():
            if not isinstance(entry, dict) or "operationId" not in entry:
                continue
            seen.append((entry["operationId"], path, verb))

    by_id: dict[str, list] = {}
    for op_id, path, verb in seen:
        by_id.setdefault(op_id, []).append(f"{verb.upper()} {path}")
    duplicates = {k: v for k, v in by_id.items() if len(v) > 1}
    assert not duplicates, f"duplicate operationIds: {duplicates}"


def test_every_operation_has_responses(spec):
    """A path operation without `responses` is invalid OpenAPI."""
    for path, methods in spec["paths"].items():
        for verb, entry in methods.items():
            if not isinstance(entry, dict):
                continue
            assert "responses" in entry, f"{verb.upper()} {path} missing responses"
