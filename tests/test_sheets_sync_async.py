"""Verify the Sheets sync background dispatch wires through to the worker."""

from __future__ import annotations

import threading
import time

from app.utils import sheets_sync, sheets_sync_worker


def _wait_for_queue_drain(timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if sheets_sync_worker._jobs.unfinished_tasks == 0:
            return
        time.sleep(0.01)


def test_async_dispatch_enqueues_to_worker(app_factory, monkeypatch):
    application = app_factory(SHEETS_SYNC_INLINE=False)

    calls: list[tuple] = []
    call_thread: list[str] = []

    def fake_sync_impl(team_id, checkpoint_id, arrived_at=None):
        calls.append((team_id, checkpoint_id, arrived_at))
        call_thread.append(threading.current_thread().name)

    monkeypatch.setattr(sheets_sync, "mark_arrival_checkbox_sync", fake_sync_impl)

    with application.app_context():
        sheets_sync.mark_arrival_checkbox(team_id=1, checkpoint_id=2)

    _wait_for_queue_drain()

    # The work happened, but on the dedicated worker thread, not the caller.
    assert calls == [(1, 2, None)]
    assert call_thread == ["sheets-sync"]


def test_inline_flag_runs_in_caller_thread(app_factory, monkeypatch):
    # Default test config sets SHEETS_SYNC_INLINE=True. Confirm the work runs
    # synchronously in the calling thread under that flag.
    application = app_factory()

    call_thread: list[str] = []

    def fake_sync_impl(team_id, checkpoint_id, arrived_at=None):
        call_thread.append(threading.current_thread().name)

    monkeypatch.setattr(sheets_sync, "mark_arrival_checkbox_sync", fake_sync_impl)

    caller_thread_name = threading.current_thread().name
    with application.app_context():
        sheets_sync.mark_arrival_checkbox(team_id=1, checkpoint_id=2)

    assert call_thread == [caller_thread_name]


def test_atexit_drain_waits_for_pending_jobs(app_factory, monkeypatch):
    """The shutdown hook should block briefly for in-flight jobs.

    We hold a job in the worker by making the sync impl block on an event,
    then call _drain_on_shutdown directly and verify it actually waits.
    """
    application = app_factory(SHEETS_SYNC_INLINE=False)

    started = threading.Event()
    can_finish = threading.Event()

    def slow_impl(team_id, checkpoint_id, arrived_at=None):
        started.set()
        can_finish.wait(timeout=2.0)

    monkeypatch.setattr(sheets_sync, "mark_arrival_checkbox_sync", slow_impl)
    # Tighten the drain timeout so the test runs fast.
    monkeypatch.setattr(sheets_sync_worker, "_SHUTDOWN_DRAIN_TIMEOUT_S", 0.5)

    with application.app_context():
        sheets_sync.mark_arrival_checkbox(team_id=1, checkpoint_id=2)

    started.wait(timeout=1.0)
    assert started.is_set()

    # Drain should return when the timeout elapses (the job is still blocked).
    drain_start = time.monotonic()
    sheets_sync_worker._drain_on_shutdown()
    drain_elapsed = time.monotonic() - drain_start
    assert 0.4 <= drain_elapsed <= 1.5, f"drain took {drain_elapsed:.2f}s; expected ~0.5s"

    # Let the worker finish so the daemon thread doesn't leak across tests.
    can_finish.set()
    _wait_for_queue_drain(timeout=2.0)


def test_atexit_hook_is_registered(app_factory, monkeypatch):
    """First _submit triggers atexit registration so prod gunicorn restarts
    invoke the drain. Skipping this would let in-flight Sheets writes vanish
    on every redeploy."""
    application = app_factory(SHEETS_SYNC_INLINE=False)

    # Reset the registration flag so we can observe it being set again.
    monkeypatch.setattr(sheets_sync_worker, "_atexit_registered", False)

    monkeypatch.setattr(sheets_sync, "mark_arrival_checkbox_sync", lambda *_a, **_k: None)
    with application.app_context():
        sheets_sync.mark_arrival_checkbox(team_id=1, checkpoint_id=2)

    assert sheets_sync_worker._atexit_registered is True
    _wait_for_queue_drain(timeout=2.0)
