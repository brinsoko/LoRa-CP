"""Single-threaded background worker for Google Sheets writes.

Race day pattern: every check-in/score POST kicks off one or more Sheets API
calls (each ~200-2000ms round-trip). Doing them inline serializes the whole
request behind the network round-trip, and a single sync gunicorn worker
gets stuck behind a slow Sheets call.

This module owns one daemon thread that consumes jobs from a queue and
invokes the underlying sheets_sync helpers inside a fresh app context. The
request returns immediately after enqueueing.

Design choices:
- One thread, not a pool: keeps writes ordered per (team, checkpoint) which
  matters for arrivals/scores both updating the same row.
- Bounded queue (max 1024) drops oldest on overflow so a Sheets API outage
  can't pin memory. Drops are logged.
- Errors are caught and logged; the original sites already wrap with
  try/except: pass so we replicate that contract — never bubble back.
- enqueue_*() accepts the live Flask app and snapshots the (immutable)
  argument values so the worker can recreate state without holding refs to
  request-scoped objects.
"""

from __future__ import annotations

import logging
import queue
import threading
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


_QUEUE_MAXSIZE = 1024
_jobs: "queue.Queue[tuple]" = queue.Queue(maxsize=_QUEUE_MAXSIZE)
_worker_thread: threading.Thread | None = None
_lock = threading.Lock()


def _worker_loop() -> None:
    while True:
        job = _jobs.get()
        try:
            fn, app, args, kwargs = job
            with app.app_context():
                fn(*args, **kwargs)
        except Exception:
            logger.exception("sheets sync background job failed")
        finally:
            _jobs.task_done()


def _ensure_worker() -> None:
    global _worker_thread
    with _lock:
        if _worker_thread is None or not _worker_thread.is_alive():
            _worker_thread = threading.Thread(
                target=_worker_loop,
                name="sheets-sync",
                daemon=True,
            )
            _worker_thread.start()


def _submit(app, fn, *args, **kwargs) -> None:
    _ensure_worker()
    try:
        _jobs.put_nowait((fn, app, args, kwargs))
    except queue.Full:
        # Drop the oldest job to make room — better than blocking the
        # request when Sheets is slow or down for an extended period.
        try:
            _jobs.get_nowait()
            _jobs.task_done()
        except queue.Empty:
            pass
        try:
            _jobs.put_nowait((fn, app, args, kwargs))
            logger.warning("sheets sync queue full, dropped oldest job")
        except queue.Full:
            logger.error("sheets sync queue still full after drop, losing job")


def enqueue_mark_arrival(app, team_id: int, checkpoint_id: int, arrived_at: datetime | None = None) -> None:
    from app.utils.sheets_sync import mark_arrival_checkbox_sync
    _submit(app, mark_arrival_checkbox_sync, team_id, checkpoint_id, arrived_at)


def enqueue_update_scores(
    app,
    team_id: int,
    checkpoint_id: int,
    group_name: str,
    values: dict[str, Any],
    scored_at: datetime | None = None,
) -> None:
    from app.utils.sheets_sync import update_checkpoint_scores_sync
    _submit(app, update_checkpoint_scores_sync, team_id, checkpoint_id, group_name, values, scored_at)
