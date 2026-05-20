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

import atexit
import logging
import queue
import threading
from datetime import datetime
from typing import Any

logger = logging.getLogger(__name__)


_QUEUE_MAXSIZE = 1024
_SHUTDOWN_DRAIN_TIMEOUT_S = 10.0
_jobs: queue.Queue[tuple] = queue.Queue(maxsize=_QUEUE_MAXSIZE)
_worker_thread: threading.Thread | None = None
_lock = threading.Lock()
_atexit_registered = False


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


def _drain_on_shutdown() -> None:
    """Best-effort drain of pending Sheets writes when the process exits.

    Called via atexit so a graceful gunicorn restart (or a Ctrl-C in dev)
    gives in-flight jobs up to _SHUTDOWN_DRAIN_TIMEOUT_S to land before
    the daemon thread is killed. Without this, up to _QUEUE_MAXSIZE
    arrival/score Sheets updates can be lost on restart while the DB
    state appears fully written.
    """
    pending = _jobs.unfinished_tasks
    if not pending:
        return
    logger.info("sheets sync drain: %d job(s) pending, waiting up to %.1fs", pending, _SHUTDOWN_DRAIN_TIMEOUT_S)
    try:
        # queue.Queue has no built-in join-with-timeout; do it manually.
        deadline = threading.Event()
        timer = threading.Timer(_SHUTDOWN_DRAIN_TIMEOUT_S, deadline.set)
        timer.daemon = True
        timer.start()
        try:
            while _jobs.unfinished_tasks and not deadline.is_set():
                deadline.wait(0.1)
        finally:
            timer.cancel()
    except Exception:
        logger.exception("sheets sync drain failed")
    remaining = _jobs.unfinished_tasks
    if remaining:
        logger.warning("sheets sync drain: %d job(s) still pending at exit", remaining)


def _ensure_worker() -> None:
    global _worker_thread, _atexit_registered
    with _lock:
        if _worker_thread is None or not _worker_thread.is_alive():
            _worker_thread = threading.Thread(
                target=_worker_loop,
                name="sheets-sync",
                daemon=True,
            )
            _worker_thread.start()
        if not _atexit_registered:
            atexit.register(_drain_on_shutdown)
            _atexit_registered = True


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


# ---------------------------------------------------------------------------
# Admin-triggered bulk Sheets operations (publish, sync-team-numbers, build
# summary tabs). These can take 30+ seconds when they hit the SheetsClient
# throttle, which exceeds the default gunicorn worker timeout. Routes
# enqueue them and return immediately; failures land in the server log.
# ---------------------------------------------------------------------------


def enqueue_publish_local(
    app,
    competition_id: int,
    spreadsheet_id: str,
    *,
    build_summary_tabs: bool = True,
) -> None:
    from app.utils.sheets_sync import publish_local_configs_to_spreadsheet

    _submit(
        app,
        publish_local_configs_to_spreadsheet,
        competition_id,
        spreadsheet_id,
        build_summary_tabs=build_summary_tabs,
    )


def enqueue_sync_all_checkpoint_tabs(app, competition_id: int | None = None) -> None:
    from app.utils.sheets_sync import sync_all_checkpoint_tabs

    _submit(app, sync_all_checkpoint_tabs, competition_id=competition_id)


def enqueue_build_arrivals_tab(
    app,
    spreadsheet_id: str,
    tab_name: str,
    *,
    competition_id: int | None = None,
    group_order_override: list | None = None,
    checkpoint_order_override: list | None = None,
    per_group_checkpoint_order: dict | None = None,
) -> None:
    from app.utils.sheets_sync import build_arrivals_tab

    _submit(
        app,
        build_arrivals_tab,
        spreadsheet_id,
        tab_name,
        competition_id=competition_id,
        group_order_override=group_order_override,
        checkpoint_order_override=checkpoint_order_override,
        per_group_checkpoint_order=per_group_checkpoint_order,
    )


def enqueue_build_teams_tab(
    app,
    spreadsheet_id: str,
    tab_name: str,
    *,
    headers: list | None = None,
    group_order_override: list | None = None,
    competition_id: int | None = None,
) -> None:
    from app.utils.sheets_sync import build_teams_tab

    _submit(
        app,
        build_teams_tab,
        spreadsheet_id,
        tab_name,
        headers=headers,
        group_order_override=group_order_override,
        competition_id=competition_id,
    )


def enqueue_build_score_tab(
    app,
    spreadsheet_id: str,
    tab_name: str,
    *,
    include_dead_time_sum: bool = True,
    group_order_override: list | None = None,
    checkpoint_order_override: list | None = None,
    per_group_checkpoint_order: dict | None = None,
    competition_id: int | None = None,
) -> None:
    from app.utils.sheets_sync import build_score_tab

    _submit(
        app,
        build_score_tab,
        spreadsheet_id,
        tab_name,
        include_dead_time_sum=include_dead_time_sum,
        group_order_override=group_order_override,
        checkpoint_order_override=checkpoint_order_override,
        per_group_checkpoint_order=per_group_checkpoint_order,
        competition_id=competition_id,
    )
