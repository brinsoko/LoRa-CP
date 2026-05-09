"""Verify TOCTOU fixes on the duplicate check-in code paths.

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
    rows = Checkin.query.filter_by(team_id=team.id, checkpoint_id=checkpoint.id, competition_id=competition.id).all()
    assert len(rows) == 1


def test_savepoint_isolates_update_replace_violation(app):
    """The same pattern applied to _update_checkin's replace path.

    The replace flow deletes the existing duplicate inside a savepoint,
    then mutates the target row's team_id/checkpoint_id. If a concurrent
    caller fills the freed slot between the delete and the update, the
    update raises uq_team_checkpoint. begin_nested rolls back the delete
    too, so the original duplicate stays put — the caller sees a clean
    409 and no rows were lost.
    """
    admin = create_user(username="replace-admin")
    competition = create_competition(name="Replace Race")
    team_a = create_team(competition, name="Team A", number=1)
    team_b = create_team(competition, name="Team B", number=2)
    checkpoint = create_checkpoint(competition, name="Replace CP")

    # The original duplicate at (team_b, checkpoint).
    dup = create_checkin(competition, team_b, checkpoint, created_by_user=admin)
    # The target check-in that wants to be moved to (team_b, checkpoint).
    target = create_checkin(competition, team_a, checkpoint, created_by_user=admin)

    # Mid-savepoint, simulate a concurrent insert that fills the slot we
    # just freed. We do this by pre-inserting another row at the target
    # coordinates (using a different team to bypass the immediate FK
    # collision), then attempt the savepoint sequence and assert it
    # rolls back cleanly. Since we can't truly race in SQLite, we verify
    # the SAVEPOINT semantics: rollback restores both the delete and the
    # subsequent update.

    # Stage: pre-existing rows are dup and target.
    assert dup.id != target.id

    target_id = target.id
    dup_id = dup.id

    with pytest.raises(IntegrityError):
        with db.session.begin_nested():
            db.session.delete(dup)
            db.session.flush()
            # An adversarial concurrent insert at the freed slot.
            interloper = Checkin(
                team_id=team_b.id,
                checkpoint_id=checkpoint.id,
                competition_id=competition.id,
            )
            db.session.add(interloper)
            db.session.flush()
            # Now move target into (team_b, checkpoint) — boom.
            target.team_id = team_b.id
            db.session.flush()

    # Both the delete and the update must have rolled back.
    survivor = db.session.get(Checkin, dup_id)
    assert survivor is not None  # the dup is back
    refreshed_target = db.session.get(Checkin, target_id)
    assert refreshed_target is not None
    assert refreshed_target.team_id == team_a.id  # target unchanged
