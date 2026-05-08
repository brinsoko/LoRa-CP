"""Verify the TOCTOU fix on duplicate check-in inserts.

Two concurrent callers can both pass the existence check and try to insert the
same (team, checkpoint, competition) row. The DB-level uq_team_checkpoint
unique constraint then fires on the second insert. The fix wraps that insert
in a SAVEPOINT (db.session.begin_nested) and catches IntegrityError so the
caller gets a clean 409 instead of a 500.

We can't trigger a real race in single-threaded SQLite tests, so we directly
exercise the SAVEPOINT pattern that the resource handlers use: pre-insert the
conflicting row, then attempt the same insert inside begin_nested() and
verify that the outer transaction stays usable afterwards.
"""

from __future__ import annotations

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import Checkin
from tests.support import (
    create_checkin,
    create_checkpoint,
    create_competition,
    create_team,
    create_user,
)


def test_savepoint_isolates_unique_constraint_violation(app):
    admin = create_user(username="savepoint-admin")
    competition = create_competition(name="Savepoint Comp")
    team = create_team(competition, name="Savepoint Team", number=1)
    checkpoint = create_checkpoint(competition, name="Savepoint CP")

    # The "concurrent" winner that races our INSERT.
    create_checkin(competition, team, checkpoint, created_by_user=admin)

    # The endpoint's pattern: wrap the insert in begin_nested so the unique
    # constraint can fire without poisoning the outer transaction.
    duplicate = Checkin(
        team_id=team.id,
        checkpoint_id=checkpoint.id,
        competition_id=competition.id,
    )
    with pytest.raises(IntegrityError):
        with db.session.begin_nested():
            db.session.add(duplicate)

    # Outer transaction must still be usable — that's the whole point of the
    # savepoint. If the bare INSERT had been used, the session would now be
    # in a failed state and any subsequent query would error.
    rows = (
        Checkin.query
        .filter_by(team_id=team.id, checkpoint_id=checkpoint.id, competition_id=competition.id)
        .all()
    )
    assert len(rows) == 1
