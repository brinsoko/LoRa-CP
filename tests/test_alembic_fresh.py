"""Regression test for fresh `alembic upgrade head`.

Before the SKIP_DB_BOOTSTRAP flag, alembic/env.py called create_app()
which ran db.create_all(), so by the time migrations executed all
columns were already created from the models. The migrations then
tried to ADD those columns and `alembic upgrade head` failed with
"duplicate column name" against an empty target DB.

This test runs `alembic upgrade head` against an actual empty SQLite
file and asserts it succeeds, locking in the fix."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_alembic_upgrade_head_on_fresh_db(tmp_path):
    db_path = tmp_path / "fresh_alembic.sqlite"
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path}"

    # Use the current Python interpreter so this works in CI (no venv on
    # disk) and locally regardless of venv naming.
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, (
        f"alembic upgrade head failed against a fresh DB.\n"
        f"stdout: {result.stdout[-2000:]}\n"
        f"stderr: {result.stderr[-2000:]}"
    )

    # Verify the DB ended up at the expected head revision and that
    # all the columns the audit codified are present.
    conn = sqlite3.connect(db_path)
    try:
        cur = conn.cursor()
        cur.execute("SELECT version_num FROM alembic_version")
        version = cur.fetchone()[0]
        # Whatever the current head is, just confirm it's stamped.
        assert version, "alembic_version is empty after upgrade"

        cur.execute("PRAGMA table_info(competitions)")
        comp_cols = {row[1] for row in cur.fetchall()}
        assert "ingest_password_hash" in comp_cols
        assert "hide_gps_map" in comp_cols

        cur.execute("PRAGMA table_info(rfid_cards)")
        rfid_cols = {row[1] for row in cur.fetchall()}
        assert "competition_id" in rfid_cols, (
            "rfid_cards.competition_id missing — did the RFID composite "
            "migration apply?"
        )
    finally:
        conn.close()
