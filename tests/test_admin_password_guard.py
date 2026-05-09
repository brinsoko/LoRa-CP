"""Regression tests for the production guard against default admin
credentials.

The dev convenience of `make admin` / `make seed` falling back to
admin123 is intentional, but production must refuse it. The scripts
exit non-zero with a clear message when FLASK_ENV=production and
ADMIN_PASS / SEED_ADMIN_PASS isn't explicitly set."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON = str(ROOT / "venv313" / "bin" / "python")


def _run_script(script: str, env_overrides: dict[str, str], tmp_path) -> subprocess.CompletedProcess:
    """Run a script with a temp SQLite DB and a fresh subset of the
    parent env so we don't accidentally inherit a SEED_*_PASS that
    would let the script proceed."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "DATABASE_URL": f"sqlite:///{tmp_path / 'guard.sqlite'}",
        "SECRET_KEY": "test-guard",
        "LORA_WEBHOOK_SECRET": "test-guard-webhook",
    }
    env.update(env_overrides)
    return subprocess.run(
        [PYTHON, script],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )


def test_create_admin_refuses_default_password_in_production(tmp_path):
    """scripts/create_admin.py with FLASK_ENV=production and no ADMIN_PASS
    must exit non-zero rather than seed with admin123."""
    result = _run_script(
        "scripts/create_admin.py",
        {"FLASK_ENV": "production"},
        tmp_path,
    )
    assert result.returncode != 0, (
        f"create_admin should have refused the default password in production.\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    assert "ADMIN_PASS" in result.stderr or "ADMIN_PASS" in result.stdout, (
        "expected error message to mention ADMIN_PASS"
    )


def test_create_admin_refuses_explicit_default_password_in_production(tmp_path):
    """Setting ADMIN_PASS=admin123 explicitly is also rejected — the
    guard checks the value, not just whether the env var was set."""
    result = _run_script(
        "scripts/create_admin.py",
        {"FLASK_ENV": "production", "ADMIN_PASS": "admin123"},
        tmp_path,
    )
    assert result.returncode != 0, "explicit admin123 should still be refused"


def test_create_admin_accepts_strong_password_in_production(tmp_path):
    """A non-default ADMIN_PASS in production is accepted. Sanity-
    check that the guard doesn't block legitimate prod usage."""
    result = _run_script(
        "scripts/create_admin.py",
        {"FLASK_ENV": "production", "ADMIN_PASS": "AStr0ngPass!23"},
        tmp_path,
    )
    assert result.returncode == 0, (
        f"create_admin with strong password should succeed in prod.\n"
        f"stderr: {result.stderr}"
    )


def test_create_admin_default_works_outside_production(tmp_path):
    """When FLASK_ENV is unset (dev) the default still works for
    convenience. This is the documented dev path."""
    result = _run_script(
        "scripts/create_admin.py",
        {},  # no FLASK_ENV
        tmp_path,
    )
    assert result.returncode == 0, (
        f"create_admin with no FLASK_ENV should fall back to dev default.\n"
        f"stderr: {result.stderr}"
    )


def test_seed_db_refuses_default_admin_pass_in_production(tmp_path):
    """seed_db.py rejects the SEED_ADMIN_PASS default in production
    the same way."""
    # seed_db expects to be able to drop-and-create when fresh=True;
    # we don't pass --fresh so it just tries to seed, which means it
    # must hit the password guard before any DB ops.
    result = _run_script(
        "scripts/seed_db.py",
        {"FLASK_ENV": "production"},
        tmp_path,
    )
    assert result.returncode != 0, (
        f"seed_db should have refused the default password in production.\n"
        f"stdout: {result.stdout}\n"
        f"stderr: {result.stderr}"
    )
    combined = (result.stderr or "") + (result.stdout or "")
    assert "SEED_ADMIN_PASS" in combined, (
        "expected error message to mention SEED_ADMIN_PASS"
    )
