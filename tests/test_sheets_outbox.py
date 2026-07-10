"""The durable Sheets outbox (redesign plan 3.4): enqueue dispatch,
coalescing, worker execution, backoff, dead-letter, recovery."""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.extensions import db
from app.models import SheetsSyncJob
from app.utils import sheets_outbox, sheets_sync
from app.utils.time import utcnow_naive
from tests.support import (
    add_membership,
    create_checkpoint,
    create_competition,
    create_team,
    create_user,
)


@pytest.fixture
def outbox_app(app_factory):
    """Async-mode app (SHEETS_SYNC_INLINE off) with sync enabled."""
    application = app_factory(SHEETS_SYNC_INLINE=False)
    with application.app_context():
        from app.utils.sheets_settings import save_settings

        save_settings({"sync_enabled": True})
        yield application


def _seed():
    user = create_user(username="outbox-admin", role="admin")
    comp = create_competition(name="Outbox Cup")
    add_membership(user, comp, role="admin")
    cp = create_checkpoint(comp, name="CP-O")
    team = create_team(comp, name="T-O", number=100)
    return comp, cp, team


class TestEnqueueDispatch:
    def test_mark_arrival_enqueues_job(self, outbox_app):
        comp, cp, team = _seed()
        sheets_sync.mark_arrival_checkbox(team.id, cp.id)
        job = SheetsSyncJob.query.filter_by(kind="arrival").one()
        assert job.status == "pending"
        assert job.competition_id == comp.id
        assert job.dedup_key == f"arrival:{team.id}:{cp.id}"
        assert job.payload["team_id"] == team.id

    def test_same_key_coalesces(self, outbox_app):
        comp, cp, team = _seed()
        sheets_sync.update_checkpoint_scores(team.id, cp.id, "Alpha", {"points": 1})
        sheets_sync.update_checkpoint_scores(team.id, cp.id, "Alpha", {"points": 7})
        jobs = SheetsSyncJob.query.filter_by(kind="scores").all()
        assert len(jobs) == 1
        assert jobs[0].payload["values"] == {"points": 7}

    def test_inline_mode_runs_synchronously(self, app, monkeypatch):
        # Default test app has SHEETS_SYNC_INLINE=True.
        comp, cp, team = _seed()
        calls = []
        monkeypatch.setattr(
            sheets_sync, "mark_arrival_checkbox_sync", lambda *a: calls.append(a)
        )
        sheets_sync.mark_arrival_checkbox(team.id, cp.id)
        assert calls == [(team.id, cp.id, None)]
        assert SheetsSyncJob.query.count() == 0

    def test_disabled_sync_enqueues_nothing(self, outbox_app):
        from app.utils.sheets_settings import save_settings

        save_settings({"sync_enabled": False})
        comp, cp, team = _seed()
        sheets_sync.mark_arrival_checkbox(team.id, cp.id)
        assert SheetsSyncJob.query.count() == 0


class TestWorker:
    def test_run_due_jobs_executes_and_marks_done(self, outbox_app, monkeypatch):
        comp, cp, team = _seed()
        calls = []
        monkeypatch.setattr(
            sheets_sync, "mark_arrival_checkbox_sync", lambda *a, **k: calls.append(a)
        )
        sheets_sync.mark_arrival_checkbox(team.id, cp.id)
        stats = sheets_outbox.run_due_jobs()
        assert stats == {"done": 1, "retried": 0, "failed": 0}
        assert calls and calls[0][0] == team.id
        assert SheetsSyncJob.query.one().status == "done"

    def test_failure_backs_off_then_dead_letters(self, outbox_app, monkeypatch):
        comp, cp, team = _seed()

        def boom(*_a, **_k):
            raise RuntimeError("quota exceeded")

        monkeypatch.setattr(sheets_sync, "mark_arrival_checkbox_sync", boom)
        sheets_sync.mark_arrival_checkbox(team.id, cp.id)

        stats = sheets_outbox.run_due_jobs()
        job = SheetsSyncJob.query.one()
        assert stats["retried"] == 1
        assert job.status == "pending"
        assert job.attempts == 1
        assert job.next_attempt_at is not None
        assert "quota exceeded" in job.last_error

        # Not due yet: the backoff keeps it out of the next cycle.
        assert sheets_outbox.run_due_jobs() == {"done": 0, "retried": 0, "failed": 0}

        # Force due repeatedly until dead-lettered.
        for _ in range(sheets_outbox.MAX_ATTEMPTS - 1):
            job.next_attempt_at = utcnow_naive() - timedelta(seconds=1)
            db.session.commit()
            sheets_outbox.run_due_jobs()
        job = SheetsSyncJob.query.one()
        assert job.status == "failed"
        assert job.attempts == sheets_outbox.MAX_ATTEMPTS

    def test_failed_job_can_be_requeued_and_succeed(self, outbox_app, monkeypatch):
        comp, cp, team = _seed()
        sheets_sync.mark_arrival_checkbox(team.id, cp.id)
        job = SheetsSyncJob.query.one()
        job.status = "failed"
        job.attempts = sheets_outbox.MAX_ATTEMPTS
        db.session.commit()

        # What the admin Retry button does.
        job.status = "pending"
        job.attempts = 0
        job.next_attempt_at = None
        db.session.commit()
        monkeypatch.setattr(sheets_sync, "mark_arrival_checkbox_sync", lambda *a, **k: None)
        assert sheets_outbox.run_due_jobs()["done"] == 1

    def test_stuck_running_jobs_recover(self, outbox_app):
        comp, cp, team = _seed()
        sheets_sync.mark_arrival_checkbox(team.id, cp.id)
        job = SheetsSyncJob.query.one()
        job.status = "running"
        job.updated_at = utcnow_naive() - timedelta(minutes=30)
        db.session.commit()
        sheets_outbox._recover_stuck_running()
        assert SheetsSyncJob.query.one().status == "pending"

    def test_prune_removes_old_terminal_jobs(self, outbox_app):
        comp, cp, team = _seed()
        sheets_sync.mark_arrival_checkbox(team.id, cp.id)
        job = SheetsSyncJob.query.one()
        job.status = "done"
        job.updated_at = utcnow_naive() - sheets_outbox.RETENTION - timedelta(days=1)
        db.session.commit()
        sheets_outbox._prune_old_jobs()
        assert SheetsSyncJob.query.count() == 0


class TestSummaryRebuilds:
    def test_roster_change_marks_summary_tabs_dirty(self, outbox_app):
        from app.models import SheetConfig

        comp, cp, team = _seed()
        db.session.add(
            SheetConfig(
                competition_id=comp.id,
                spreadsheet_id="REAL-SHEET",
                spreadsheet_name="S",
                tab_name="Ekipe",
                tab_type="teams",
            )
        )
        db.session.add(
            SheetConfig(
                competition_id=comp.id,
                spreadsheet_id="local:1",
                spreadsheet_name="L",
                tab_name="Prihodi",
                tab_type="arrivals",
            )
        )
        db.session.commit()
        sheets_outbox.enqueue_summary_rebuilds(comp.id)
        db.session.commit()
        kinds = {j.kind for j in SheetsSyncJob.query.all()}
        # local: configs are skipped; the real teams tab and the
        # team-number columns are dirty-flagged.
        assert kinds == {"sync_team_numbers", "rebuild_teams"}


class TestWorkerReliability:
    """Pass-3 hardening: the worker must never mark a job done when the
    Google write failed, must not replay stale payloads over newer ones,
    must not reset the retry budget on coalesce, and must not drain jobs
    as no-ops while sync is disabled."""

    def _cp_config(self, comp, cp):
        from app.models import SheetConfig

        db.session.add(
            SheetConfig(
                competition_id=comp.id,
                spreadsheet_id="REAL-SHEET",
                spreadsheet_name="S",
                tab_name=cp.name,
                tab_type="checkpoint",
                checkpoint_id=cp.id,
                config={"time_enabled": True, "groups": []},
            )
        )
        db.session.commit()

    def test_arrival_write_failure_retries_instead_of_done(self, outbox_app, monkeypatch):
        """mark_arrival_checkbox_sync swallows client failures for inline
        callers, but the worker passes raise_errors=True so the job
        retries; it used to be marked done with the write lost."""
        comp, cp, team = _seed()
        self._cp_config(comp, cp)
        sheets_sync.mark_arrival_checkbox(team.id, cp.id)

        def boom(_app):
            raise RuntimeError("service account unreadable")

        monkeypatch.setattr(sheets_sync, "get_sheets_client", boom)
        stats = sheets_outbox.run_due_jobs()
        job = SheetsSyncJob.query.one()
        assert stats == {"done": 0, "retried": 1, "failed": 0}
        assert job.status == "pending"
        assert "service account unreadable" in (job.last_error or "")

    def test_failed_job_superseded_by_newer_duplicate_is_dropped(self, outbox_app, monkeypatch):
        """A job that fails while a NEWER pending duplicate exists must be
        dropped (like _recover_stuck_running does), not requeued: the
        retry would replay its stale payload after the newer write."""
        comp, cp, team = _seed()
        sheets_sync.mark_arrival_checkbox(team.id, cp.id)
        db.session.commit()

        def execute_with_racing_enqueue(job):
            # Simulate a web process enqueueing a fresh write for the
            # same target while this job is mid-execution (status is
            # 'running', so the coalescing UPDATE inserts a new row).
            sheets_outbox.enqueue_job(
                job.kind, job.competition_id, {"newer": True}, job.dedup_key
            )
            db.session.commit()
            raise RuntimeError("transient API failure")

        monkeypatch.setattr(sheets_outbox, "_execute", execute_with_racing_enqueue)
        sheets_outbox.run_due_jobs()

        jobs = SheetsSyncJob.query.order_by(SheetsSyncJob.id.asc()).all()
        assert len(jobs) == 2
        old, new = jobs
        assert old.status == "done"
        assert "superseded" in (old.last_error or "")
        assert new.status == "pending"
        assert new.payload == {"newer": True}

    def test_coalesce_keeps_retry_budget_and_error(self, outbox_app, monkeypatch):
        """Refreshing a pending job's payload must not reset attempts or
        last_error: a hot dedup_key against a permanently broken sheet
        would otherwise never dead-letter and never surface its error."""
        comp, cp, team = _seed()
        sheets_sync.mark_arrival_checkbox(team.id, cp.id)
        db.session.commit()

        def boom(_job):
            raise RuntimeError("sheet deleted")

        monkeypatch.setattr(sheets_outbox, "_execute", boom)
        sheets_outbox.run_due_jobs()
        job = SheetsSyncJob.query.one()
        assert job.attempts == 1
        assert job.next_attempt_at is not None

        sheets_sync.mark_arrival_checkbox(team.id, cp.id)
        db.session.commit()
        job = SheetsSyncJob.query.one()
        assert job.attempts == 1, "coalesce must not reset the retry budget"
        assert "sheet deleted" in (job.last_error or "")
        assert job.next_attempt_at is None, "fresh payload should retry promptly"

    def test_disabled_sync_leaves_jobs_pending(self, outbox_app):
        """With sync disabled every _sync helper early-returns, which the
        worker used to interpret as success, draining the queue as no-ops
        and losing the writes for good once sync came back."""
        from app.utils.sheets_settings import save_settings

        comp, cp, team = _seed()
        sheets_sync.mark_arrival_checkbox(team.id, cp.id)
        db.session.commit()
        save_settings({"sync_enabled": False})
        assert sheets_outbox.run_due_jobs() == {"done": 0, "retried": 0, "failed": 0}
        assert SheetsSyncJob.query.one().status == "pending"

    def test_roster_delete_and_group_change_mark_tabs_dirty(self, outbox_app):
        """team_delete / active-group change / renumber are roster changes
        too; only create/update used to dirty-flag the sheets."""
        from app.models import TeamGroup, User
        from tests.support import create_group, login_as

        comp, cp, team = _seed()
        group_a = create_group(comp, name="A", prefix="1xx")
        group_b = create_group(comp, name="B", prefix="2xx")
        db.session.add(TeamGroup(team_id=team.id, group_id=group_a.id, active=True))
        db.session.commit()

        client = outbox_app.test_client()
        user = User.query.filter_by(username="outbox-admin").one()
        login_as(client, user, comp)

        def drain():
            SheetsSyncJob.query.delete()
            db.session.commit()

        drain()
        resp = client.post(f"/api/teams/{team.id}/active-group", json={"group_id": group_b.id})
        assert resp.status_code == 200, resp.data
        assert SheetsSyncJob.query.filter_by(kind="sync_team_numbers").count() == 1

        drain()
        resp = client.delete(f"/api/teams/{team.id}", json={})
        assert resp.status_code == 200, resp.data
        assert SheetsSyncJob.query.filter_by(kind="sync_team_numbers").count() == 1
