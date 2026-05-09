"""Verify the score-submit auto-checkin path survives a concurrent insert.

Two judges submitting scores for the same team at the same checkpoint can
both pass the existence check and try to insert a checkin row. The DB-level
uq_team_checkpoint constraint then fires on the loser. The fix wraps the
insert in a SAVEPOINT and reuses the winner's row instead of leaking a 500.

Single-threaded SQLite tests can't trigger a real race, so we exercise the
SAVEPOINT semantics directly: pre-seed the conflicting checkin, then attempt
the same insert inside begin_nested() and verify the outer transaction stays
usable.
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


def test_savepoint_isolates_score_submit_checkin_violation(app):
    judge = create_user(username="score-savepoint-judge")
    competition = create_competition(name="Score Savepoint Comp")
    team = create_team(competition, name="Score Savepoint Team", number=1)
    checkpoint = create_checkpoint(competition, name="Score Savepoint CP")

    # Pre-insert the conflicting row that a concurrent judge would have just
    # written between our existence check and our INSERT.
    create_checkin(competition, team, checkpoint, created_by_user=judge)

    duplicate = Checkin(
        competition_id=competition.id,
        team_id=team.id,
        checkpoint_id=checkpoint.id,
    )
    with pytest.raises(IntegrityError):
        with db.session.begin_nested():
            db.session.add(duplicate)

    # The outer transaction must still be usable — the savepoint rollback
    # must not poison it. This is the whole point of using begin_nested().
    rows = Checkin.query.filter_by(team_id=team.id, checkpoint_id=checkpoint.id, competition_id=competition.id).all()
    assert len(rows) == 1
