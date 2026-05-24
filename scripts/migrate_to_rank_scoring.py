#!/usr/bin/env python3
"""One-shot migration from the dual GlobalScoreRule + ScoreRule.time_race
setup to the consolidated rank-mode GlobalScoreRule.

Why: comp 10 (and likely future ones) had time-trial scoring configured
TWICE — once as an absolute threshold/penalty on GlobalScoreRule.time
and again as rank-based on per-CP ScoreRule.time_race. The two systems
added together, producing inflated totals and confusing operators.

What this does, per group in the target competition:
  1. Flip the GlobalScoreRule.time block to mode="rank".
  2. Default max_points=100, min_points=10 (override via CLI if needed).
  3. Drop ScoreRule.time_race rows whose (start_cp, end_cp) match the
     group's GlobalScoreRule.time, so the live-compute block in
     _build_scores_context doesn't double-add.

Usage (idempotent, safe to re-run):
  venv/bin/python scripts/migrate_to_rank_scoring.py --competition-id 10
  venv/bin/python scripts/migrate_to_rank_scoring.py --competition-id 10 \\
      --max-points 100 --min-points 10
  venv/bin/python scripts/migrate_to_rank_scoring.py --competition-id 10 --dry-run

Always run --dry-run first if you're unsure. The print output shows
every change before --apply commits it.
"""

from __future__ import annotations

import argparse
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from sqlalchemy.orm.attributes import flag_modified

from app import create_app
from app.extensions import db
from app.models import CheckpointGroup, GlobalScoreRule, ScoreRule


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--competition-id", type=int, required=True)
    p.add_argument("--max-points", type=float, default=100.0)
    p.add_argument("--min-points", type=float, default=10.0)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Print changes without committing. Recommended first run.",
    )
    args = p.parse_args()

    app = create_app()
    with app.app_context():
        flipped = 0
        dropped = 0
        groups = CheckpointGroup.query.filter_by(competition_id=args.competition_id).all()
        if not groups:
            print(f"No groups in competition {args.competition_id}; nothing to do.")
            return

        for g in groups:
            gsr = GlobalScoreRule.query.filter_by(
                competition_id=args.competition_id,
                group_id=g.id,
            ).first()
            if not gsr:
                print(f"  [{g.name}] no GlobalScoreRule — skipping")
                continue

            rules = dict(gsr.rules or {})
            time_rule = dict(rules.get("time") or {})
            if not time_rule:
                print(f"  [{g.name}] no time rule — skipping")
                continue

            old_mode = (time_rule.get("mode") or "absolute").lower()
            if old_mode != "rank":
                time_rule["mode"] = "rank"
                time_rule["max_points"] = args.max_points
                time_rule["min_points"] = args.min_points
                rules["time"] = time_rule
                gsr.rules = rules
                flag_modified(gsr, "rules")
                flipped += 1
                print(f"  [{g.name}] flipped: mode absolute -> rank (max={args.max_points}, min={args.min_points})")
            else:
                print(f"  [{g.name}] already rank mode — skipping flip")

            start_cp = time_rule.get("start_checkpoint_id")
            end_cp = time_rule.get("end_checkpoint_id")
            if not (start_cp and end_cp):
                continue

            # Find ScoreRule.time_race rows for this group that compete
            # with the global rule (same start/end CPs in either order).
            srs = ScoreRule.query.filter_by(
                competition_id=args.competition_id,
                group_id=g.id,
            ).all()
            for sr in srs:
                tr = (sr.rules or {}).get("time_race") or {}
                if not tr:
                    continue
                sr_start = tr.get("start_checkpoint_id")
                sr_end = tr.get("end_checkpoint_id")
                if not (sr_start and sr_end):
                    continue
                ends_match = {int(sr_start), int(sr_end)} == {int(start_cp), int(end_cp)}
                if not ends_match:
                    continue
                print(
                    f"  [{g.name}] dropping overlapping ScoreRule.time_race "
                    f"at cp={sr.checkpoint_id} (start={sr_start} end={sr_end})"
                )
                db.session.delete(sr)
                dropped += 1

        print()
        if args.dry_run:
            print(f"DRY RUN — {flipped} flip(s) + {dropped} drop(s) planned. Re-run without --dry-run to apply.")
            db.session.rollback()
        else:
            db.session.commit()
            print(f"Committed: {flipped} flip(s) + {dropped} drop(s).")


if __name__ == "__main__":
    main()
