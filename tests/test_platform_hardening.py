"""Platform/bootstrap hardening for the two-worker gunicorn deployment.

Covers the SQLite connection pragmas (WAL + busy_timeout), the boot-time
db.create_all() guard, and ensure_default_competition surviving a
concurrent-boot insert race."""

from __future__ import annotations

from unittest import mock

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Competition, CompetitionMember
from app.utils.competition import DEFAULT_COMPETITION_NAME, ensure_default_competition


def test_sqlite_pragmas_applied(app):
    busy_timeout = db.session.execute(text("PRAGMA busy_timeout")).scalar()
    assert busy_timeout == 15000

    foreign_keys = db.session.execute(text("PRAGMA foreign_keys")).scalar()
    assert foreign_keys == 1

    # The test DB is file-backed, so WAL must stick. In-memory DBs would
    # report "memory" here instead.
    journal_mode = db.session.execute(text("PRAGMA journal_mode")).scalar()
    assert journal_mode == "wal"


def test_second_boot_skips_create_all(app_factory):
    app_factory()  # first boot builds the schema

    with mock.patch.object(db, "create_all", wraps=db.create_all) as spy:
        second = app_factory()

    assert spy.call_count == 0, "create_all ran again even though the schema exists"

    with second.app_context():
        # The second boot reused the bootstrap competition instead of
        # failing or duplicating it.
        assert Competition.query.filter_by(name=DEFAULT_COMPETITION_NAME).count() == 1


def test_ensure_default_competition_returns_existing(app):
    existing = Competition.query.order_by(Competition.created_at.asc()).first()
    assert existing is not None  # created during app bootstrap

    result = ensure_default_competition()
    assert result is not None
    assert result.id == existing.id
    assert Competition.query.count() == 1


def test_ensure_default_competition_recovers_from_insert_race(app):
    CompetitionMember.query.delete()
    Competition.query.delete()
    db.session.commit()

    real_commit = db.session.commit
    state = {"raced": False}

    def racing_commit():
        if state["raced"]:
            return real_commit()
        state["raced"] = True
        # Simulate the other worker winning the race: our pending insert
        # is rolled back, a competing row is already committed, and the
        # UNIQUE constraint on competitions.name fires.
        db.session.rollback()
        db.session.add(Competition(name=DEFAULT_COMPETITION_NAME))
        real_commit()
        raise IntegrityError(
            "INSERT INTO competitions",
            {},
            Exception("UNIQUE constraint failed: competitions.name"),
        )

    with mock.patch.object(db.session, "commit", new=racing_commit):
        result = ensure_default_competition()

    assert result is not None
    assert result.name == DEFAULT_COMPETITION_NAME
    assert Competition.query.count() == 1
