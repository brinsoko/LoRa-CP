# Audit hardening (llm_audit) - plan

Source: whole-codebase review (2026-06-12), 15 verified findings + overflow.
Branch: `llm_audit @ 18c3e60` (fast-forwarded to master). Tests + ruff must be
green before each commit lands.

## Work groups (one agent each, disjoint file ownership)

| # | Group | Files | Fixes |
|---|---|---|---|
| 1 | transfer-idor | `app/api/transfer.py` | Export/merge authorize against URL `comp_id` (active admin membership or superadmin), not the session competition. |
| 2 | scoring | `app/resources/scores.py`, `app/blueprints/scores/routes.py` | Auto-DNF becomes compute-only on GET (no commit during render); CSV rank uses per-group `place`; legacy time_race paths unified to base+rank via one shared helper; negative `dead_time` rejected; `max_points=0` honored (no `or` falsy-zero); duplicate ScoreRule query and O(N^2) self-lookups removed. |
| 3 | ingest | `app/resources/ingest.py` | Device `ts` bounds-checked (fallback to server time + log); LoRaDevice get-or-create wrapped in savepoint with IntegrityError retry; bare `except: pass` blocks get logging. |
| 4 | checkins | `app/resources/checkins.py` | `_parse_timestamp` converts offset-aware ISO strings to naive UTC instead of dropping the offset. |
| 5 | platform | `app/extensions.py`, `config.py`, `app/__init__.py`, `app/utils/competition.py` | SQLite WAL + busy_timeout pragmas; `db.create_all()` only on fresh DB; `ensure_default_competition` race-safe; CRITICAL startup warning when running on dev secrets. |
| 6 | misc-routes | `app/blueprints/users/routes.py`, `app/blueprints/checkpoints/routes.py`, `app/blueprints/firmware/routes.py`, `app/utils/validators.py` | `add_user` keeps `User.role` to `public` (membership carries the real role); checkpoint float parse 500 -> flash; firmware non-dict JSON guard; i18n f-string/extraction fixes. |
| 7 | sheets | `app/utils/sheets_sync.py` | Score sync no longer clobbers the arrival time cell; `sync_all_checkpoint_tabs` keeps score columns aligned with renumbered teams; `datetime.now()` fallback -> naive UTC. |

## Decisions

- **Auto-DNF**: rendering must not mutate; DNF from timeline is computed into the
  row display each render. Persisted `team.dnf` stays manual/explicit.
- **time_race**: canonical total = base points + rank points (what `score_submit`
  stores). Recompute and live paths align to it. Legacy path kept per the
  post-race plan; rank-mode GlobalScoreRule is the successor.
- **Secrets**: non-breaking hardening; loud CRITICAL log instead of refusing to
  boot when FLASK_ENV is unset and dev secrets are active.

## Deferred (documented, needs own design)

- Cross-process Sheets throttle/ordering (two gunicorn workers, per-process
  queues); needs a file lock or a single sync process.
- CSRF exemption on `/api/auth/login` (login-CSRF); removing it breaks API clients.
- Sheets/API timestamp display-timezone policy (UTC written today, consistently).
- Translating API `detail` strings surfaced in flashes.

---

# Post-race overhaul — plan

Branch: `post-race-improvements`. Starts from `master @ 04f2c70`.

User picked:
1. **Scoring consolidation: option A** — one rank-based system, drop `ScoreRule.time_race` overlap.
2. **Item 15 (sheets readback): deferred.** Not in this pass.

## Schema changes (one alembic migration)

Five new columns. All nullable / defaulted so existing data survives.

| Table | Column | Type | Notes |
|---|---|---|---|
| `judge_checkpoints` | `competition_id` | INTEGER FK→competitions, nullable, indexed | Backfill from `Checkpoint.competition_id` join. New uniqueness: `(user_id, checkpoint_id, competition_id)`. |
| `checkpoints` | `position` | INTEGER, nullable | Backfill = row_number ordered by current name per competition. Mirrors `CheckpointGroup.position`. |
| `teams` | `notes` | TEXT, nullable | Free-text per-team notes for judges (special events). |
| `teams` | `bonus_dead_time` | FLOAT, default 0.0 | Team-level dead-time bucket not bound to any CP. Added to `_get_team_dead_time_total`. |
| `checkpoint_groups` | `reverse` | BOOLEAN, default FALSE | Flips checkpoint route order for groups that traverse the course backward. |

No data migration for `GlobalScoreRule.time` — the `mode` field will be added to the JSON blob with default `"absolute"` so existing rules behave identically. New `"rank"` mode is opt-in.

## Implementation waves

### Wave 1: backend bugs (~2h, parallelizable)
- **#12** Judge assignment per-comp — model + route fix
- **#13** CSV export: drop `cp.` prefix; cells become `<name> | <description> | <points>`
- **#18** Suppress writeback-failed UI when payload isn't a UID
- **#5** Checkpoint global ordering — replace `order_by(name)` sites
- **#1** Judge post-login lands on `/scores/judge`, not `/teams/`

### Wave 2: sheets infra (~2h)
- **#14** Batch sheets API calls in `update_checkpoint_scores_sync` (4-5× rate-limit improvement)
- **#3** Sheet remap from UI — fix the hidden-input form bug
- **#16** Public-only summary tab — new `build_public_summary_tab` function

### Wave 3: scoring consolidation (~3h)
- Add `mode` field to `GlobalScoreRule.time` JSON (`"absolute"` | `"rank"`).
- In `_compute_global_contrib`, branch on `mode`:
  - `"absolute"` — current threshold/penalty formula (unchanged behavior)
  - `"rank"` — call `_compute_time_race_scores_from_checkins` semantics with the group's start/end CPs
- Drop the live `time_race` block from `_build_scores_context` (replaced by the new rank mode in `_compute_global_contrib`).
- Migration: for comp 10 specifically, flip all GlobalScoreRule.time blocks to `mode="rank"` with `max=100 min=10` and drop matching ScoreRule.time_race rows.
- Display: single "Time-trial" column in scores_view showing `<score> (<effective_min> min)`.
- Sheets push: hook in ingest.py at `mark_arrival_checkbox` site so auto-arrivals at the group's end CP trigger a recompute + sheet push.

### Wave 4: live arrivals + judges-on-CP + missed + team search (~2h)
- **#4** `CheckpointGroup.reverse` flag + flip in `_build_group_routes`
- **#10** Missed-CPs column in live arrivals (computed from expected route − actual check-ins, no DB change)
- **#11** Judges section on checkpoint list page with inline assign modal
- **#6** Port `<datalist>` team-search pattern from `score_judge.html` to `add_checkin.html`
- **#8** Same pattern to `/checkins/` and `/teams/` filters

### Wave 5: notes + team dead-time + nav + popups (~1h)
- **#19** `Team.notes` textarea on team edit page
- **#20** `Team.bonus_dead_time` admin-only input + `_get_team_dead_time_total` addition
- **#7** Admin dropdown grouping in nav (Sheets, Score Rules, Audit, Users, Firmware, Settings collapse into one "Admin ▾" menu)
- **#17** Flash messages get semantic icons, auto-dismiss success in 4s, dismissible X button

### Wave 6: verify + summary (~30min)
- Full test suite run, ruff clean, smoke-check the major flows
- Write SUMMARY.md with what shipped, what didn't, why, follow-ups

## Item status table

| # | Item | Status | Wave |
|---|---|---|---|
| 1 | Less bloat, score is the main one | Will redirect judges to score entry | 1 |
| 2 | Single time-trial field in scores | Display consolidation | 3 |
| 3 | Sheet remap from UI (no docker exec) | Form input fix | 2 |
| 4 | Live arrivals track each side | `reverse` flag + flip | 4 |
| 5 | Global checkpoint ordering | `position` column + sort sites | 1 |
| 6 | Search bar consistency (checkins/add) | Port datalist | 4 |
| 7 | Fewer tabs | Admin dropdown grouping | 5 |
| 8 | More team searchability | Datalist on more views | 4 |
| 9 | Time trial logic + display | Display only (logic correct after consolidation) | 3 |
| 10 | Skipped CP tracking | Render-time computed column (no DB) | 4 |
| 11 | Judges on checkpoint page | Inline assignment modal | 4 |
| 12 | Judge assignment per-competition | Schema fix + scoped query | 1 |
| 13 | Cleaner CSV export | Drop prefix, add name/desc | 1 |
| 14 | Batch sheets calls | Restructure to batch_update_columns | 2 |
| 15 | Sheets readback | **DEFERRED** (user agreed) | — |
| 16 | Public sheet with totals only | New `build_public_summary_tab` | 2 |
| 17 | Better popups | Icons + auto-dismiss + dismissible | 5 |
| 18 | Suppress fake writeback-failed | UID heuristic in flash | 1 |
| 19 | Team notes | `Team.notes` column + textarea | 5 |
| 20 | Team-level dead time | `Team.bonus_dead_time` column | 5 |

## Things explicitly NOT done

- **#15 readback**: deferred per agreement. Sheet → DB import with conflict resolution is its own feature.
- **Full nav redesign**: only an "Admin" dropdown grouping. No tab removal, no major hierarchy change.
- **Toast-style popups with action buttons**: just icons + auto-dismiss for now.
- **Migration: drop old `ScoreRule.time_race` rows**: I'll add a script under `scripts/` to do this for comp 10, but won't run it automatically — user runs it once they verify the new rank-based GlobalScoreRule.time scoring works.
- **Drag-and-drop checkpoint reordering UI**: schema + sort sites done, but the reorder UI is left for a follow-up. Order can be set via existing edit form (number input).

## Data preservation

All schema changes are additive with safe defaults. Existing comp 10 data is untouched until the user explicitly opts in to the new scoring mode (which is a JSON field flip, reversible). Migrations are idempotent.
