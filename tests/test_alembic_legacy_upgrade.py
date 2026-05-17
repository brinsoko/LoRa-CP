"""Regression tests for `alembic upgrade head` against pre-existing
DBs that look like real prod data — not fresh ones built by the model.

The idempotency in commit 32dcf46 is meant to handle three nasty
scenarios that fresh-DB tests don't exercise:

  1. A DB that previously ran the boot-time ALTER TABLE blocks, so
     it's already at the d4e5f6a7b8c9 column-level state but Alembic
     thinks it's still at a3b1c2d4e5f6.

  2. A DB still on the OLD rfid_cards schema (uid VARCHAR UNIQUE) —
     upgrading must drop that global UNIQUE and replace it with the
     composite (competition_id, uid) UNIQUE without losing the data.

  3. A DB with orphaned rfid_cards (team_id pointing at a row that's
     since been deleted) — the e1f2a3b4c5d6 backfill must handle
     this gracefully rather than crashing on the NOT NULL alter.

Each test builds the bad-state DB by hand, sets alembic_version
explicitly, runs alembic upgrade head as a subprocess, and asserts
both the exit code and the resulting invariants."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_upgrade(db_path: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["DATABASE_URL"] = f"sqlite:///{db_path}"
    # Run alembic via the current Python interpreter so the test works
    # in CI (no venv on disk) and locally regardless of venv naming.
    return subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=str(ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_upgrade_legacy_db_with_runtime_ddl_columns_already_present(tmp_path):
    """A legacy DB stamped at a3b1c2d4e5f6 may already have columns
    that d4e5f6a7b8c9 codifies (because the boot-time ALTER blocks
    populated them). The migration must skip what's already present
    instead of failing with duplicate column."""
    db = tmp_path / "legacy_with_drift.sqlite"
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE competitions (
            id INTEGER PRIMARY KEY,
            name VARCHAR,
            ingest_password_hash VARCHAR(255),
            hide_gps_map BOOLEAN DEFAULT 0 NOT NULL
        );
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            username VARCHAR(50) UNIQUE NOT NULL,
            password_hash VARCHAR(255) NOT NULL,
            role VARCHAR(20) NOT NULL,
            email VARCHAR(255),
            google_sub VARCHAR(255),
            last_competition_id INTEGER
        );
        CREATE TABLE teams (
            id INTEGER PRIMARY KEY,
            name VARCHAR,
            number INTEGER,
            organization VARCHAR,
            dnf BOOLEAN,
            competition_id INTEGER REFERENCES competitions(id)
        );
        CREATE TABLE checkpoint_groups (
            id INTEGER PRIMARY KEY,
            name VARCHAR,
            position INTEGER,
            competition_id INTEGER
        );
        CREATE TABLE checkpoints (
            id INTEGER PRIMARY KEY,
            name VARCHAR,
            competition_id INTEGER,
            is_virtual BOOLEAN DEFAULT 0 NOT NULL
        );
        CREATE TABLE lora_devices (id INTEGER PRIMARY KEY, dev_num INTEGER, competition_id INTEGER);
        CREATE TABLE checkins (
            id INTEGER PRIMARY KEY,
            competition_id INTEGER,
            team_id INTEGER,
            checkpoint_id INTEGER,
            timestamp DATETIME,
            created_by_user_id INTEGER,
            created_by_device_id INTEGER
        );
        CREATE TABLE lora_messages (
            id INTEGER PRIMARY KEY,
            competition_id INTEGER,
            dev_id INTEGER,
            received_at DATETIME
        );
        CREATE TABLE firmware_files (
            id INTEGER PRIMARY KEY,
            name VARCHAR,
            nvs_size INTEGER DEFAULT 20480 NOT NULL
        );
        CREATE TABLE rfid_cards (
            id INTEGER PRIMARY KEY,
            uid VARCHAR(100) UNIQUE NOT NULL,
            team_id INTEGER UNIQUE NOT NULL REFERENCES teams(id),
            number INTEGER
        );
        CREATE TABLE team_groups (
            id INTEGER PRIMARY KEY,
            team_id INTEGER NOT NULL,
            group_id INTEGER NOT NULL,
            active BOOLEAN NOT NULL,
            UNIQUE (team_id, group_id)
        );
        CREATE TABLE score_entries (
            id INTEGER PRIMARY KEY,
            competition_id INTEGER,
            team_id INTEGER,
            checkin_id INTEGER NOT NULL,
            checkpoint_id INTEGER,
            field VARCHAR,
            value VARCHAR,
            created_at DATETIME
        );
        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('a3b1c2d4e5f6');
        """
    )
    conn.commit()
    conn.close()

    result = _run_upgrade(db)
    assert result.returncode == 0, (
        f"upgrade against legacy-with-drift DB failed.\n"
        f"stderr: {result.stderr[-2000:]}"
    )


def test_upgrade_legacy_db_with_old_rfid_uid_global_unique(tmp_path):
    """A DB stamped at d4e5f6a7b8c9 still has rfid_cards with the
    OLD column-level UNIQUE on uid. The e1f2a3b4c5d6 migration must
    swap that for UNIQUE(competition_id, uid) so the same physical
    card can be reused across events."""
    db = tmp_path / "legacy_old_rfid.sqlite"
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE competitions (id INTEGER PRIMARY KEY, name VARCHAR,
            ingest_password_hash VARCHAR(255), hide_gps_map BOOLEAN DEFAULT 0 NOT NULL);
        CREATE TABLE teams (id INTEGER PRIMARY KEY, name VARCHAR,
            competition_id INTEGER REFERENCES competitions(id));
        CREATE TABLE checkpoint_groups (id INTEGER PRIMARY KEY, name VARCHAR);
        CREATE TABLE rfid_cards (
            id INTEGER PRIMARY KEY,
            uid VARCHAR(100) UNIQUE NOT NULL,
            team_id INTEGER UNIQUE NOT NULL REFERENCES teams(id),
            number INTEGER
        );
        CREATE TABLE team_groups (id INTEGER PRIMARY KEY,
            team_id INTEGER NOT NULL, group_id INTEGER NOT NULL,
            active BOOLEAN NOT NULL, UNIQUE (team_id, group_id));

        INSERT INTO competitions (id, name, hide_gps_map) VALUES (1, 'A', 0), (2, 'B', 0);
        INSERT INTO teams (id, name, competition_id) VALUES (1, 'TA', 1), (2, 'TB', 2);
        INSERT INTO rfid_cards (uid, team_id) VALUES ('SHARED', 1);

        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('d4e5f6a7b8c9');
        """
    )
    conn.commit()
    conn.close()

    result = _run_upgrade(db)
    assert result.returncode == 0, f"upgrade failed: {result.stderr[-2000:]}"

    conn = sqlite3.connect(db)
    cur = conn.cursor()
    try:
        # The same UID must now insert successfully into a different
        # competition.
        cur.execute(
            "INSERT INTO rfid_cards (competition_id, uid, team_id) VALUES (2, 'SHARED', 2)"
        )
        conn.commit()

        # But within one competition, the same UID must still collide.
        try:
            cur.execute(
                "INSERT INTO rfid_cards (competition_id, uid, team_id) "
                "VALUES (1, 'SHARED', 99)"
            )
            conn.commit()
            raise AssertionError(
                "intra-competition UID duplication should have raised IntegrityError"
            )
        except sqlite3.IntegrityError:
            pass

        # And the original row's competition_id should have been
        # backfilled from teams.
        cur.execute("SELECT competition_id FROM rfid_cards WHERE uid='SHARED' AND team_id=1")
        row = cur.fetchone()
        assert row[0] == 1, f"backfill did not set competition_id from team: {row}"
    finally:
        conn.close()


def test_upgrade_drops_orphaned_rfid_cards(tmp_path):
    """If an rfid_cards row points at a team_id that no longer exists
    (orphan), the e1f2a3b4c5d6 backfill subquery returns NULL and the
    NOT NULL alter would fail. The migration must drop those orphans
    rather than crashing."""
    db = tmp_path / "legacy_orphan_rfid.sqlite"
    conn = sqlite3.connect(db)
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE competitions (id INTEGER PRIMARY KEY, name VARCHAR,
            ingest_password_hash VARCHAR(255), hide_gps_map BOOLEAN DEFAULT 0 NOT NULL);
        CREATE TABLE teams (id INTEGER PRIMARY KEY, name VARCHAR,
            competition_id INTEGER REFERENCES competitions(id));
        CREATE TABLE checkpoint_groups (id INTEGER PRIMARY KEY, name VARCHAR);
        CREATE TABLE rfid_cards (
            id INTEGER PRIMARY KEY,
            uid VARCHAR(100) UNIQUE NOT NULL,
            team_id INTEGER UNIQUE NOT NULL,
            number INTEGER
        );
        CREATE TABLE team_groups (id INTEGER PRIMARY KEY,
            team_id INTEGER NOT NULL, group_id INTEGER NOT NULL,
            active BOOLEAN NOT NULL, UNIQUE (team_id, group_id));

        INSERT INTO competitions (id, name, hide_gps_map) VALUES (1, 'X', 0);
        INSERT INTO teams (id, name, competition_id) VALUES (1, 'real-team', 1);
        -- orphan: team 999 was deleted before the FK was enforced.
        INSERT INTO rfid_cards (uid, team_id) VALUES ('LEGIT', 1), ('ORPHAN', 999);

        CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL PRIMARY KEY);
        INSERT INTO alembic_version VALUES ('d4e5f6a7b8c9');
        """
    )
    conn.commit()
    conn.close()

    result = _run_upgrade(db)
    assert result.returncode == 0, (
        f"upgrade with orphaned rfid card failed.\n"
        f"stderr: {result.stderr[-2000:]}"
    )

    conn = sqlite3.connect(db)
    cur = conn.cursor()
    try:
        # The orphan should be gone; the legitimate card should remain.
        cur.execute("SELECT uid FROM rfid_cards ORDER BY uid")
        uids = [row[0] for row in cur.fetchall()]
        assert uids == ["LEGIT"], (
            f"orphaned card was not dropped — found: {uids}"
        )
    finally:
        conn.close()
