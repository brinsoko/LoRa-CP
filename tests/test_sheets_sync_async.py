"""Verify the Sheets sync background dispatch wires through to the worker."""

from __future__ import annotations

import time
import threading

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
