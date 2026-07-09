# Redesign plan: judge UX, paths, scoring, sheets (July 2026)

Input: team feedback from the Scukanujanje debrief. Goals, in their words:

1. The judge app must be super simple: fewer buttons, mobile-first, a clean
   view of "my checkpoint", arrived teams, and overall competition state,
   including who the judge still waits for and roughly how long.
2. Sheets sync must stop being inconsistent.
3. Time trial: scoring views should show `A (arrival); B (arrival);
   A -> B (diff, points)`, not just the merged number.
4. Paths need to be first-class: store `A -> B -> C`, flip it to
   `C -> B -> A` with one action, duplicate paths (including a reversed
   duplicate), and share them between categories. Typical event: 2 courses,
   each run in both directions, so 4 categories from 2 authored paths.
5. Scoring config: a list of scoring fields attached to the checkpoint with
   default scoring, overridable per group, with checkboxes so a group can
   use only one (or a few) of the fields.
6. Declutter the DB: scoring definitions live in a real scoring table, not
   inside the Sheets config (that layout is a leftover from before local
   scoring existed). Simplify wherever possible.

## 1. What the audit found (why these problems exist)

**Judge UX.** Every page renders the same global action bar with ~10 buttons
plus 3 dropdowns (`base.html:146-209`); nothing is role-prioritized. The app
already knows the judge's checkpoint (`JudgeCheckpoint.is_default`) but only
uses it to preselect dropdowns; no screen is scoped to it. Expected-vs-arrived
per checkpoint and per-team "still out" are already computed
(`app/utils/live_arrivals.py:258-358`) but rendered as 6- and 9-column
whole-competition tables that are unusable on a phone. The core score flow
uses blocking `alert()` popups, and `judges_note` (per-CP instructions for
judges) is authored by admins but never shown to judges.

**Paths.** There is no route entity. A route is emergent from
`CheckpointGroup` + ordered `CheckpointGroupLink` rows + a `reverse` bool.
`reverse` is honored in exactly one place (live arrivals,
`live_arrivals.py:75-82`). The scoring engine, sheets, and ingest recompute
all use a second, independent direction source: hand-set
`start_checkpoint_id`/`end_checkpoint_id` in score-rule JSON. Nothing keeps
the two consistent; a reversed group with a forward-configured leg silently
scores nothing (negative durations are dropped,
`app/resources/scores.py:456-458`). "Same path, opposite direction" today
means rebuilding the checkpoint list from scratch in a second group and
manually flipping the leg config.

**Scoring config.** Which fields a judge scores is defined inside
`SheetConfig.config.groups[]` (a Google-Sheets tab config); how those fields
score is defined in `ScoreRule.rules` JSON; whole-race contributions live in
`GlobalScoreRule.rules` JSON. Three homes, two of them opaque JSON blobs, one
of them in the wrong subsystem entirely. Scoring cannot even be configured
without touching Sheets config. There are also two overlapping timing systems
(`ScoreRule.time_race` rank-spread vs `GlobalScoreRule.time`
threshold-penalty) and an inconsistency between the submit and recompute
paths (`scores.py:864-872` adds the leg to the base total,
`scores.py:521-523` overwrites the total).

**Time trial display.** The per-team A and B arrival timestamps are computed
during leaderboard build (`blueprints/scores/routes.py:589-592`) and then
discarded; only the diff survives. The "merged leg cell" from commit 731f77f
exists only in `scores_view.html`; the CSV export and the Sheets score tab
still show the old unmerged layout.

**Sheets sync.** The sync queue is an in-memory `queue.Queue` with one daemon
thread, per process (`app/utils/sheets_sync_worker.py`). With gunicorn's two
workers that means: two independent rate limiters (so the real Google quota
gets blown), two queues (jobs die with their process; the atexit drain does
not run on SIGKILL/timeout), queue overflow silently discards the oldest job,
failed jobs are never re-enqueued, and every call site swallows every
exception. Summary tabs (Ekipe/Prihodi/Skupni sestevek) are only rebuilt by a
manual admin button, so they chronically lag. Row targets are recomputed from
live roster order at write time, so a roster change between enqueue and write
lands data on the wrong row. "Inconsistent" is the expected behavior of this
architecture.

## 2. Design principles

- One source of truth per concept: direction lives on the path assignment,
  field definitions live in scoring tables, Sheets is a pure projection of
  the DB and can always be rebuilt from it.
- The judge's phone is the primary client. Every judge screen must work
  one-handed on a 6" display without horizontal scrolling.
- Structured columns over JSON blobs wherever the shape is known. JSON stays
  only for genuinely polymorphic rule parameters.
- Delete what we replace. Each phase drops its superseded tables/flags in the
  same phase, after backfill.

## 3. Target data model

### 3.1 Paths (new)

```
Path
  id, competition_id, name, notes

PathStop
  id, path_id (FK), checkpoint_id (FK), position,
  expected_leg_minutes (float, nullable)   # expected duration of the leg
                                           # (previous stop -> this stop),
                                           # undirected; ETA fallback until
                                           # enough observed data exists
  unique (path_id, position)          # NOTE: duplicates of checkpoint_id allowed
```

`CheckpointGroup` (conceptually "category") changes:

- add `path_id` (FK paths, nullable), `direction` ('forward' | 'reverse')
- drop `reverse` and the whole `CheckpointGroupLink` table after backfill

A category is now: name + prefix + team assignment + (path, direction).
Two categories running the same course opposite ways reference one Path row,
so the standard setup (2 courses, each run in both directions = 4
categories) means authoring exactly 2 paths and picking path + direction on
each of the 4 categories. No checkpoint list is ever entered twice.

Path management UI:

- Path list with **Duplicate** and **Duplicate reversed** actions, for the
  cases where a copy should evolve independently of the original.
- Path edit with drag-ordering and a **Reverse order** action (reorders the
  stops in place).
- Category form is just a path picker plus a direction toggle.

One resolver becomes the single route authority, replacing today's
`_build_group_routes` and all ad-hoc start/end lookups:

```python
# app/utils/routes.py (new)
resolve_route(group) -> [checkpoint_id, ...]   # directed
route_start(group), route_finish(group)        # first/last of directed list
```

Consumers to switch: `live_arrivals.py`, `_build_scores_context`,
`sheets_sync.py`, `ingest.py` recompute trigger, stats.

Checkin stays one-per-(team, checkpoint) (`uq_team_checkpoint`). PathStop
allows a checkpoint to appear twice so the model does not block it, but
revisit *recording* (A -> B -> A for one team) is out of scope; it needs a
visit index on Checkin plus ingest dedup rework. Flagged as follow-up.

### 3.2 Scoring fields (new, replaces SheetConfig-as-field-source and ScoreRule.field_rules)

```
ScoreField                           # "what can be scored at this CP"
  id, competition_id, checkpoint_id (FK), key, label, hint,
  position, rule_type ('none'|'mapping'|'interpolate'|'multiplier'|'deviation'),
  rule_params (JSON), max_input, counts_in_total (bool)

ScoreFieldGroup                      # per-group selection + override
  id, score_field_id (FK), group_id (FK),
  enabled (bool), rule_params_override (JSON, nullable)
  unique (score_field_id, group_id)
```

Semantics:

- No `ScoreFieldGroup` rows for a (field, group) pair means the field is
  enabled with default scoring; overrides are the exception, matching the
  "default scoring, can be overwritten per group" requirement.
- The admin UI per checkpoint: list of fields, then a group-by-field checkbox
  matrix. The meeting example (CP scores two things, group X gets only field
  1, group Y only field 2) is two fields and two checkbox rows.
- `dead_time` becomes a per-checkpoint flag (`Checkpoint.dead_time_enabled`)
  instead of a SheetConfig key.
- `/api/scores/resolve` reads ScoreField + ScoreFieldGroup only. SheetConfig
  no longer participates in scoring; its `groups[].fields` blob is dropped
  after backfill, and sheet column layout is generated from ScoreField.
- `ScoreEntry` is unchanged (raw_fields keyed by `ScoreField.key`, total
  derived).

"Found points" (points per visited CP): the per-category value lives in
`GroupScoring.found_points_per` (see 3.3), and eligibility becomes a
per-checkpoint checkbox, `Checkpoint.counts_for_found` (default true;
unchecked for virtual checkpoints, start, finish, and similar). A team's
found points = `found_points_per` x visited route checkpoints that have the
checkbox on. This replaces the old per-group exclude_start/exclude_end
flags with something an admin can actually see. With time moving to 3.3,
**both `ScoreRule` and `GlobalScoreRule` tables get dropped.**

### 3.3 Time trials and the race time rule (replace ScoreRule.time_race and GlobalScoreRule.time)

These are two different mechanisms and both stay, but as two clearly
separated concepts instead of two JSON blobs:

**Time trial**: a timed stretch between two stops on the path, scored by
rank spread. Example: within the race, the leg A -> B is a sprint.

```
TimedSegment
  id, competition_id, path_id (FK),
  start_stop_id (FK path_stops), end_stop_id (FK path_stops),
  name,                              # display label; default "A -> B"
  max_points, min_points
```

- Defined once on the Path; endpoints swap automatically for
  reverse-direction categories.
- **Any number of time trials per path/competition.** Today's code assumes
  one leg per group (`group_leg_info` takes the first rule); the new engine
  computes and displays N segments per team.
- Rank spread is computed **within each category**: the fastest team of the
  category gets `max_points`, the slowest `min_points`, linear in between.
  Two categories sharing a path are ranked separately.
- Segment points stop hijacking a checkpoint's `ScoreEntry`
  (`scoring_checkpoint_id` is gone): segments are computed by the engine
  and get their own columns in every surface. This also removes the
  submit-vs-recompute total inconsistency by construction.
- Segment diffs are raw `B - A`; **dead time never applies to a time
  trial.** Dead time awarded at a segment's start checkpoint is fine (it
  counts toward the overall race dead-time total only), but dead time must
  never be awarded at a segment's end checkpoint: the admin UI blocks
  enabling `dead_time_enabled` on a checkpoint that is the end stop of any
  segment, and validation rejects it server-side.

**Race time rule**: the whole race (route start -> route finish) has an
expected duration; finishing over it costs points. Per category, since
different age groups get different expected durations on the same course.
Lives in the new category-scoring table, which also absorbs found points:

```
GroupScoring                         # 1:1 with CheckpointGroup, all nullable
  group_id (PK, FK),
  found_points_per,                  # points per found checkpoint (3.2)
  race_max_points, race_threshold_minutes,
  race_penalty_minutes, race_penalty_points,   # "deduct y per x minutes over"
  race_min_points, race_dq_multiplier
```

- Endpoints are always `route_start`/`route_finish` of the category's
  directed route; no configurable checkpoints, no JSON override.
- Accumulated dead time is subtracted before comparing to the threshold
  (current behavior kept; this is the only place dead time affects timing).
- The deduction is **stepped per full block**:
  `floor(minutes_over / race_penalty_minutes) * race_penalty_points`, so
  with "10 points per 5 minutes" a team 7 minutes over loses 10 points and a
  team 2 minutes over loses nothing. This intentionally replaces today's
  proportional deduction; the migration acceptance diff will show it on past
  races with fractional blocks (expected, see section 4).
- `CheckpointGroup` itself stays identity-only (name, prefix, path,
  direction, teams); category-level scoring params all live in GroupScoring.

**Display contract: four values per time trial per team, everywhere** (app
extended view, CSV, judge results tab, stats, Sheets):

```
time A | time B | diff | points
```

with timestamps in Europe/Ljubljana display time. In Sheets that is
literally four columns per segment on the score tab. A and B are written by
the sync from checkins; **diff is an in-sheet formula (B - A)** so a
hand-entered time still computes; points are written by the system.
Hand-edit rule: the sync never clears or overwrites a cell it has no DB data
for, so a missed scan can be patched by hand in the sheet without the next
sync wiping it. **No readback**: hand edits never flow back into the DB;
the DB stays authoritative and the sheet is patchable for display only.

The app-side timestamps are already computed and discarded today
(`routes.py:589-592`), so a cut-down version of this display ships as a
Phase 0 quick win.

### 3.4 Sheets sync (outbox + single worker + reconciliation)

```
SheetsSyncJob
  id, competition_id, kind ('arrival'|'scores'|'team_numbers'|'rebuild_tab'|...),
  payload (JSON), dedup_key (str, indexed),
  status ('pending'|'running'|'done'|'failed'),
  attempts, next_attempt_at, last_error, created_at, updated_at
```

- **Enqueue = DB insert in the same transaction as the domain write** (checkin,
  score, team change). Nothing can be lost to a restart, an overflow, or a
  swallowed exception, because there is no in-memory state.
- **One dispatcher.** A `flask sheets-worker` CLI process (own systemd unit /
  compose service) polls the table, coalesces pending jobs by `dedup_key`
  (latest wins), applies the 40-calls/60s throttle globally (accurate, since
  there is exactly one), retries with exponential backoff on 429/network, and
  marks jobs `failed` with `last_error` after N attempts instead of dropping
  them. Fallback if a second process is unwanted: an SQLite lease lock so
  exactly one gunicorn worker runs the drain thread.
- **Reconciliation instead of manual buttons.** Roster/score/config changes
  set a dirty flag (a `rebuild_tab` job with a stable dedup_key); summary
  tabs rebuild within ~1 minute of a change instead of waiting for an admin
  click. A low-frequency periodic job re-verifies bindings and heals missing
  tabs.
- **Stable row addressing.** CP tabs get a key column (team number or id);
  writes locate the row by key instead of recomputing `.index()` from live
  roster order.
- **Observability.** sheets_admin shows queue depth, last successful sync per
  tab, and failed jobs with error + a retry button. The per-process quota
  gauge becomes the worker's real gauge. The bare `except Exception: pass`
  wrappers around sync calls are deleted.

`sheets_sync_worker.py` (in-memory queue) is deleted. `sheets_client.py`
throttle/retry logic survives, hosted by the worker.

The worker ships as a separate service in the deploy compose file (agreed).
The SQLite outbox is deliberately the first implementation: it needs no new
infrastructure and the write volume (tens of jobs per minute at peak) is far
below SQLite's limits. If queue latency ever becomes a real problem, the
dispatcher's polling loop is the only piece that changes: swap the poll for
a Redis queue and keep the same job table for durability. Not needed now.

### 3.5 Judge experience

New judge shell, mobile-first, replacing the judge's view of the current nav
(admins keep a full nav, possibly slimmed later):

- **Bottom nav, 3 tabs: "My CP" / "Teams" / "Results"** (English base
  strings; judges see the Slovenian translations, e.g. "Moja KT" / "Ekipe" /
  "Rezultati"). No global action bar for judges. Competition switcher,
  language, and logout fold into a single header menu.
- **My CP (landing).** Header: checkpoint name + `judges_note` (finally
  surfaced) + scoring instructions collapsed behind a tap. One primary
  action: scan (Web NFC where available) with an always-visible team-number
  search fallback. Scanning or picking a team does check-in and scoring in
  one flow: create the checkin if missing, render that team's fields (from
  ScoreField resolution), big touch-friendly widgets, submit, inline toast.
  No `alert()`, no raw JSON panels, no separate "Judge RFID" vs "Score"
  consoles: `score_judge`, `rfid_judge`, and `add_checkin` collapse into
  this one screen for judges. Below the action area: arrived-teams list for
  this CP with scored/unscored badges, tap to (re)score. **Corrections are a
  first-class part of this list:** a judge can reopen and resubmit a team's
  score at any time after the team has moved on (each resubmission is a new
  `ScoreEntry`, latest wins, full history preserved for audit).
- **Teams (who is still coming).** Scoped to the judge's checkpoint. A team
  counts as *waiting-for* only while it can still plausibly arrive; it drops
  off the list as soon as any of these holds:
  - it has a checkin at this CP (arrived),
  - it has a checkin at **any later stop** on its directed route (it skipped
    this CP; shown separately in a "missed you" bucket, not silently
    dropped, since that is information the judge wants),
  - it is DNF,
  - it has finished (checkin at its route's finish).
  For each genuinely waiting team: team, category, last seen (CP + minutes
  ago), and an ETA. ETA logic: once at least ~3 teams have completed the leg
  (previous stop -> this stop, same direction), use the **mean of observed
  leg durations**; before that, fall back to the manual
  `PathStop.expected_leg_minutes` if set; otherwise show only "last seen"
  with no estimate. Rendered as "arriving in ~12 min" / "overdue 5 min" /
  "not started" (sl: "pride cez ~12 min" / "zamuja 5 min" / "se ni
  startala"). A per-CP summary line on top: "12 of 18 teams arrived,
  waiting for 6".
- **Results (overall state).** Compact competition overview: per-category
  progress (started/on course/finished/DNF), top teams, and the segment
  results in the four-cell format (time A, time B, diff, points). Cards, not
  9-column tables.
- Multi-checkpoint judges get a checkpoint switcher in the header (default =
  `JudgeCheckpoint.is_default`).

Implementation: one new blueprint (`app/blueprints/judge/`) + templates; the
expected/ETA computation extends `live_arrivals.py` (or the new routes util).
Old judge-facing pages remain reachable for admins until Phase 5 cleanup.

### 3.6 Bulk score entry (per-checkpoint opt-in)

Use case: stations like written tests, where papers are collected, sorted by
team number, scored offline, and then entered in one sitting. Entering them
one team at a time through the single-team flow is the pain point.

- `Checkpoint.bulk_entry_enabled` (bool, admin sets it on the checkpoint).
- On such checkpoints the judge shell shows an extra **"Table"** sub-tab
  (sl "Tabela"): a grid with one row per team (sorted by team number, filterable by
  category), one column per resolved score field for that team's group,
  keyboard-friendly (tab/enter moves through cells), with per-row save state
  and a single "save all" action.
- Backend: a batch submit endpoint that validates each row with the same
  rules as the single flow and writes one `ScoreEntry` per team. Teams
  without a checkin at the CP get one created (marked as manual, audited),
  since paper-based stations may not have scanned arrivals.
- The grid is also available to admins from the scores area, since bulk
  entry after the race is often done at HQ rather than on the phone at the
  station. The grid must still work on a phone, but it is the one judge
  surface where a wide table is accepted.

## 4. Migration and backfill

All schema work via Alembic batch mode, one migration per phase, safe against
the live SQLite DB (two-step deploy convention unchanged). Backfills:

- **Phase 1:** for each `CheckpointGroup`, create a `Path` (name = group
  name) from its `CheckpointGroupLink` order; `direction` = 'reverse' if
  `reverse` was set. Where two groups have identical stop sequences (or exact
  reverses of one another), merge them onto one shared Path in the backfill.
  Then drop `CheckpointGroupLink` and `CheckpointGroup.reverse`.
- **Phase 2:** build `ScoreField` rows from `SheetConfig.config.groups[].fields`
  unioned with `ScoreRule.rules.field_rules` (rule params, labels, hints,
  total_fields -> counts_in_total); `ScoreFieldGroup` rows where groups
  differ. `ScoreRule.time_race` -> `TimedSegment` rows;
  `GlobalScoreRule.rules.time` -> `GroupScoring` race columns;
  `GlobalScoreRule.rules.found` -> `GroupScoring.found_points_per` plus
  `Checkpoint.counts_for_found` backfill (false for virtual CPs and for
  start/finish CPs that any group's rule excluded; note this deliberately
  simplifies per-group exclusions to a per-checkpoint checkbox, as decided).
  Then drop `ScoreRule` and `GlobalScoreRule` and strip field definitions
  from `SheetConfig.config`.
- Every backfill is rehearsed against a copy of the production DB before
  deploy, with a before/after leaderboard diff for a real past competition as
  the acceptance check. Totals must be identical, with one known exception:
  the race-time penalty intentionally changes from proportional to stepped
  (3.3), so rows whose overage was a fractional block may differ, and only
  by the expected amount. The rehearsal script verifies each diff against
  the stepped formula instead of ignoring it.

## 5. Phases

Each phase lands as a PR into master at a coherent checkpoint, tests + ruff
green. Working agreement:

- Claude commits locally on the phase branch, with no Co-Authored-By
  trailers, and never pushes; PRs are opened by hand (never by Claude).
- All user-facing strings are authored in **English** and wrapped in `_()`;
  each phase ends with the pybabel extract/update/compile pass so every new
  term ships with its **Slovenian** translation.

**Phase 0, quick wins (no schema, shippable immediately):**
- Show A/B arrival clock times in the existing time-trial leg cell
  (`scores_view.html`), and align the CSV export with the merged-leg format.
- Surface `judges_note` on `score_judge`.
- Role-aware nav: judges stop seeing the 10-button action bar; keep only
  Score / Live / Check-ins for them. Pure template change, big decluttering
  payoff while the real judge shell is built.
- (Dropped: the reported blank `display_timezone` on `add_checkin` was a
  false positive; the value is injected globally by the context processor
  in `app/__init__.py`, verified during implementation.)

**Phase 1, paths:** new tables + resolver + backfill; group/category UI
becomes path picker + direction; live arrivals, scoring context, ingest
recompute, and sheets consume the resolver. Drop `CheckpointGroupLink`,
`reverse`.

**Phase 2, scoring tables + segments:** ScoreField/ScoreFieldGroup +
TimedSegment + GroupScoring + `Checkpoint.counts_for_found` + backfill; new
checkpoint-scoring admin UI (field list + group matrix); `/api/scores/resolve`
rewired; one segment computation path; the four-cell time-trial display in
all surfaces (app, CSV, Sheets with in-sheet diff formula). Drop `ScoreRule`,
`GlobalScoreRule`, SheetConfig field blobs.

Phase 2 implementation notes (deviations from the letter of the plan,
same spirit):

- `SheetConfig.config` keeps its `groups[].fields` / `dead_time_enabled`
  keys: they describe the column layout of tabs already published to a
  spreadsheet and per-cell writes compute offsets from them. They are now
  a derived layout cache (publishes regenerate them from ScoreField), not
  a scoring source; the plan's "strip field definitions" would have
  broken existing tabs' geometry.
- In Sheets, the segment A/B arrival cells are INDEX/MATCH formulas over
  the CP tabs' Time cells (not system-written values), and diff/points
  are formulas over those. This is strictly better for the hand-patch
  requirement: fixing a missed scan on the CP tab flows through A, B,
  diff, points and the total with no sync involved.
- The race time formula in Sheets uses FLOOR (stepped) and always the
  directed route endpoints, matching the engine.

**Phase 3, judge shell:** new `/judge` blueprint, three tabs, merged
scan+checkin+score flow, expected/ETA view (with missed/DNF/finished
exclusions), score corrections from the arrived list, bulk-entry grid for
`bulk_entry_enabled` checkpoints, toasts. Judges land here after login.

Phase 3 implementation notes:

- The scan flow records the arrival at scan time (resolve with
  create_checkin), not at score submit: an arrival must exist even if the
  judge never submits a score. Submit still auto-creates as a fallback.
- A team that skipped the checkpoint all the way to the finish appears in
  the "missed you" bucket AND counts as finished in the summary line.
- expected_leg_minutes is editable per stop on the path edit page (the
  ETA fallback); the minutes value rides in each list row so drag/reverse
  reorder keeps the pairs aligned.
- The bulk grid renders one section per category (fields differ per
  group), writes only changed rows, and records an arrival for teams
  scored on paper without a scan.

**Phase 4, sheets:** outbox table + single worker process + coalescing +
backoff + dirty-flag reconciliation + keyed row writes + health panel.
Delete the in-memory queue.

**Phase 5, cleanup:** remove superseded judge pages/routes from judge role,
slim admin nav, regenerate ERD/architecture docs, translation sweep, delete
dead code. Optional: mount Flask-Admin under `/superadmin` as the row-level
escape hatch (the "Django admin" itch, without the migration).

Ordering rationale: paths first because segments, judge ETA, and sheets
column layout all depend on directed routes being trustworthy; scoring tables
second because the judge shell renders from them; sheets last because its
rebuild logic should be written once against the final model, not twice.

## 6. Explicitly out of scope

- **Django migration.** The admin-CRUD value is covered by the optional
  Flask-Admin item; everything else would be a rewrite of working software.
- **Same-team checkpoint revisits** (A -> B -> A recorded twice): follow-up,
  needs `(team, checkpoint, visit_index)` and ingest dedup changes. Not the
  same thing as *score corrections* after a team moved on, which ARE in
  scope (section 3.5: reopen and resubmit from the arrived list at any
  time).
- **Scheduled start times / waves.** ETA uses observed leg means with the
  manual `expected_leg_minutes` fallback instead. If a real start schedule
  appears later, it slots into the same ETA function.
- **Redis for the sheets queue.** The SQLite outbox covers the current
  volume; Redis is the documented escalation path if queue latency ever
  hurts (section 3.4), not part of this redesign.

## 7. Decisions log and remaining open points

Decided (first iteration round):

- ETA: observed **mean** once enough teams completed the leg, manual
  `expected_leg_minutes` per path stop as the pre-data fallback.
- Judge flow: scan/checkin/score merged into one screen; separate RFID
  console retired for judges (finish verification console stays).
- Sheets worker: separate compose service. SQLite outbox first; Redis only
  as a later escalation if queue latency becomes a problem.
- Multiple time trials per path/competition: fully supported (3.3).
- Waiting list: teams drop off when they arrive, skip past, DNF, or finish;
  skipped teams surface in a "missed you" bucket (3.5).
- Score corrections after the team moved on: in scope via the arrived list.
- Bulk score entry grid for selected checkpoints (3.6).
- Paths: duplicate and duplicate-reversed actions on the path list, on top
  of shared-path + direction assignment (3.1).

Decided (second iteration round):

- Both timing mechanisms stay as separate concepts: time trial = rank
  spread between two path stops (`TimedSegment`); race time rule =
  threshold penalty on route start -> finish, per category (`GroupScoring`).
- Sheets time-trial layout: four columns per segment (time A, time B, diff,
  points), diff as an in-sheet formula, hand-patched cells never clobbered
  by the sync.
- Found points: per-category `found_points_per` + per-checkpoint
  `counts_for_found` checkbox (off for virtual/start/finish).

Decided (third iteration round):

- No sheet readback at this stage: hand edits patch the sheet for display
  only; the DB stays authoritative and the sync never clobbers cells it has
  no data for.
- Race penalty is stepped per full block: `floor(minutes_over /
  race_penalty_minutes) * race_penalty_points`. Intentional behavior change
  from today's proportional deduction, accounted for in the migration
  acceptance check (section 4).
- Rank-spread pool = the category. Categories sharing a path are ranked
  separately.
- No dead time in time trials, ever; dead time affects only the overall
  race time rule. Dead time may be awarded at a segment's start checkpoint
  but never at its end checkpoint (enforced in UI and server-side
  validation).

Process (fourth round):

- Claude commits locally without Co-Authored-By trailers, never pushes,
  never opens PRs; PRs are opened by hand.
- UI strings authored in English, Slovenian translations delivered with
  every phase via the pybabel workflow.
- 4-5 PRs total: phases map to PRs, phase 5 cleanup rides inside phases 1-4
  (leftover docs/translation sweep tags along with phase 4); phase 0 is its
  own tiny PR. Each PR is internally 3-6 commits.

Still open:

1. **Phase 0 scope:** stands as the four quick wins listed in section 5
   unless the team adds more during review.
