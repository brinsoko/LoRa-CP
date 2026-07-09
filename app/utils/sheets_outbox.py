# app/utils/sheets_outbox.py
"""Durable outbox for Google Sheets writes (redesign plan 3.4).

Enqueue = a row in sheets_sync_jobs, inserted in the same transaction as
the domain change; nothing is lost to restarts, overflows, or swallowed
exceptions. A single dedicated process drains the table:

    venv/bin/flask sheets-worker

One dispatcher means the SheetsClient throttle window is finally
accurate (the old per-gunicorn-worker threads each thought they had the
full 40 calls/60s), writes stay ordered, and there are no cross-process
races. Coalescing happens at enqueue time via dedup_key: a burst of
updates for the same cell refreshes one pending job instead of queueing
duplicates. Failures retry with exponential backoff and land in status
'failed' with last_error after MAX_ATTEMPTS, visible (and retryable) on
the sheets admin page instead of vanishing into a log.
"""

from __future__ import annotations

import logging
import time
from datetime import timedelta

from app.extensions import db
from app.models import SheetConfig, SheetsSyncJob
from app.utils.sheets_settings import sheets_sync_enabled
from app.utils.time import utcnow_naive

logger = logging.getLogger(__name__)

MAX_ATTEMPTS = 5
BASE_BACKOFF_S = 30
MAX_BACKOFF_S = 900
VERIFY_INTERVAL_S = 15 * 60
# Terminal done/failed rows older than this are pruned by the worker so
# the table doesn't grow without bound over a season.
RETENTION = timedelta(days=7)


def enqueue_job(kind: str, competition_id: int, payload: dict | None, dedup_key: str) -> None:
    """Insert or refresh (coalesce) an outbox job. Commits are the
    caller's business: domain code calls this right before its own
    commit so the job rides the same transaction."""
    if not sheets_sync_enabled():
        return
    existing = (
        SheetsSyncJob.query.filter_by(dedup_key=dedup_key, status="pending")
        .order_by(SheetsSyncJob.id.desc())
        .first()
    )
    if existing is not None:
        existing.payload = payload
        existing.updated_at = utcnow_naive()
        return
    db.session.add(
        SheetsSyncJob(
            competition_id=competition_id,
            kind=kind,
            payload=payload,
            dedup_key=dedup_key,
        )
    )


def enqueue_and_commit(kind: str, competition_id: int, payload: dict | None, dedup_key: str) -> None:
    """Enqueue outside a domain transaction (admin buttons)."""
    enqueue_job(kind, competition_id, payload, dedup_key)
    db.session.commit()


def enqueue_summary_rebuilds(competition_id: int) -> None:
    """Dirty-flag the competition's summary tabs (teams/arrivals/total)
    plus the team-number columns, so roster changes reach the spreadsheet
    within one worker cycle instead of waiting for an admin button."""
    if not sheets_sync_enabled():
        return
    enqueue_job(
        "sync_team_numbers",
        competition_id,
        {"competition_id": competition_id},
        f"sync_team_numbers:{competition_id}",
    )
    kind_by_tab_type = {
        "teams": "rebuild_teams",
        "arrivals": "rebuild_arrivals",
        "total": "rebuild_score",
    }
    configs = SheetConfig.query.filter(
        SheetConfig.competition_id == competition_id,
        SheetConfig.tab_type.in_(list(kind_by_tab_type)),
    ).all()
    for cfg in configs:
        if (cfg.spreadsheet_id or "").startswith("local:"):
            continue
        kind = kind_by_tab_type[cfg.tab_type]
        enqueue_job(
            kind,
            competition_id,
            {
                "spreadsheet_id": cfg.spreadsheet_id,
                "tab_name": cfg.tab_name,
                "competition_id": competition_id,
            },
            f"{kind}:{cfg.spreadsheet_id}:{cfg.tab_name}",
        )


# ---------------------------------------------------------------------------
# Worker side
# ---------------------------------------------------------------------------


def _execute(job: SheetsSyncJob) -> None:
    from app.utils import sheets_sync

    payload = job.payload or {}

    def _ts(key: str):
        raw = payload.get(key)
        if not raw:
            return None
        from datetime import datetime

        try:
            return datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return None

    if job.kind == "arrival":
        sheets_sync.mark_arrival_checkbox_sync(
            payload["team_id"], payload["checkpoint_id"], _ts("arrived_at")
        )
    elif job.kind == "scores":
        sheets_sync.update_checkpoint_scores_sync(
            payload["team_id"],
            payload["checkpoint_id"],
            payload.get("group_name") or "",
            payload.get("values") or {},
            _ts("scored_at"),
        )
    elif job.kind == "sync_team_numbers":
        sheets_sync.sync_all_checkpoint_tabs(competition_id=payload.get("competition_id"))
    elif job.kind == "rebuild_teams":
        sheets_sync.build_teams_tab(
            payload["spreadsheet_id"],
            payload["tab_name"],
            headers=payload.get("headers"),
            group_order_override=payload.get("group_order_override"),
            competition_id=payload.get("competition_id"),
        )
    elif job.kind == "rebuild_arrivals":
        sheets_sync.build_arrivals_tab(
            payload["spreadsheet_id"],
            payload["tab_name"],
            competition_id=payload.get("competition_id"),
            group_order_override=payload.get("group_order_override"),
            checkpoint_order_override=payload.get("checkpoint_order_override"),
            per_group_checkpoint_order=payload.get("per_group_checkpoint_order"),
        )
    elif job.kind == "rebuild_score":
        sheets_sync.build_score_tab(
            payload["spreadsheet_id"],
            payload["tab_name"],
            include_dead_time_sum=payload.get("include_dead_time_sum", True),
            group_order_override=payload.get("group_order_override"),
            checkpoint_order_override=payload.get("checkpoint_order_override"),
            per_group_checkpoint_order=payload.get("per_group_checkpoint_order"),
            competition_id=payload.get("competition_id"),
        )
    elif job.kind == "publish":
        sheets_sync.publish_local_configs_to_spreadsheet(
            payload["competition_id"],
            payload["spreadsheet_id"],
            build_summary_tabs=payload.get("build_summary_tabs", True),
        )
    else:
        raise ValueError(f"unknown sheets job kind {job.kind!r}")


def run_due_jobs(limit: int = 25) -> dict:
    """Claim and execute due pending jobs (oldest first). Returns counts."""
    now = utcnow_naive()
    jobs = (
        SheetsSyncJob.query.filter(
            SheetsSyncJob.status == "pending",
            db.or_(SheetsSyncJob.next_attempt_at.is_(None), SheetsSyncJob.next_attempt_at <= now),
        )
        .order_by(SheetsSyncJob.id.asc())
        .limit(limit)
        .all()
    )
    stats = {"done": 0, "retried": 0, "failed": 0}
    for job in jobs:
        job.status = "running"
        db.session.commit()
        try:
            _execute(job)
        except Exception as exc:
            job.attempts += 1
            job.last_error = str(exc)[:2000]
            if job.attempts >= MAX_ATTEMPTS:
                job.status = "failed"
                stats["failed"] += 1
                logger.error("sheets job %s (%s) dead-lettered: %s", job.id, job.kind, exc)
            else:
                job.status = "pending"
                backoff = min(BASE_BACKOFF_S * (2 ** (job.attempts - 1)), MAX_BACKOFF_S)
                job.next_attempt_at = utcnow_naive() + timedelta(seconds=backoff)
                stats["retried"] += 1
                logger.warning(
                    "sheets job %s (%s) attempt %d failed, retry in %ds: %s",
                    job.id,
                    job.kind,
                    job.attempts,
                    backoff,
                    exc,
                )
            db.session.commit()
        else:
            job.status = "done"
            job.last_error = None
            db.session.commit()
            stats["done"] += 1
    return stats


def _prune_old_jobs() -> None:
    cutoff = utcnow_naive() - RETENTION
    SheetsSyncJob.query.filter(
        SheetsSyncJob.status.in_(("done", "failed")),
        SheetsSyncJob.updated_at < cutoff,
    ).delete(synchronize_session=False)
    db.session.commit()


def _recover_stuck_running() -> None:
    """A worker crash mid-job leaves status='running' forever; on startup
    (and periodically) push those back to pending so they retry."""
    stuck = SheetsSyncJob.query.filter(
        SheetsSyncJob.status == "running",
        SheetsSyncJob.updated_at < utcnow_naive() - timedelta(minutes=10),
    ).all()
    for job in stuck:
        job.status = "pending"
    if stuck:
        db.session.commit()


def worker_loop(poll_seconds: float = 2.0, run_once: bool = False) -> None:
    """The sheets-worker process body. Exactly one instance should run
    (the dedicated compose service); web workers only ever enqueue."""
    from app.models import Competition

    logger.info("sheets worker starting (poll every %.1fs)", poll_seconds)
    _recover_stuck_running()
    last_verify = 0.0
    last_prune = 0.0
    while True:
        try:
            stats = run_due_jobs()
            if any(stats.values()):
                logger.info(
                    "sheets worker cycle: %d done, %d retried, %d dead-lettered",
                    stats["done"],
                    stats["retried"],
                    stats["failed"],
                )
            now = time.monotonic()
            if now - last_verify >= VERIFY_INTERVAL_S:
                last_verify = now
                # Periodic re-verify: refresh team-number columns (which
                # also heals missing tabs) for every competition that has
                # remote checkpoint tabs.
                comp_ids = {
                    comp_id
                    for (comp_id,) in db.session.query(SheetConfig.competition_id)
                    .filter(SheetConfig.tab_type == "checkpoint")
                    .filter(~SheetConfig.spreadsheet_id.startswith("local:"))
                    .distinct()
                    .all()
                    if db.session.get(Competition, comp_id) is not None
                }
                for comp_id in comp_ids:
                    enqueue_job(
                        "sync_team_numbers",
                        comp_id,
                        {"competition_id": comp_id},
                        f"sync_team_numbers:{comp_id}",
                    )
                db.session.commit()
                _recover_stuck_running()
            if now - last_prune >= 3600:
                last_prune = now
                _prune_old_jobs()
        except Exception:
            logger.exception("sheets worker cycle failed")
            db.session.rollback()
        if run_once:
            return
        time.sleep(poll_seconds)
