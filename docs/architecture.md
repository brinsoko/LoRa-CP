# System Architecture

This document describes the high-level architecture, data model, and request
flow of the LoRa-CP checkpoint tracking system.

![Architecture diagram](architecture.svg)

---

## Overview

LoRa-CP is a monolithic Flask application that serves both HTML pages (Jinja2
templates with Bootstrap 5) and a JSON API. It uses SQLite via SQLAlchemy for
persistence and Alembic for schema migrations.

The system tracks teams checking in at physical checkpoints during scouting
events. Check-ins can arrive through:

1. **Manual entry** -- judges use the web UI or API.
2. **LoRa device ingest** -- hardware devices send RFID card UIDs over LoRa to
   the `/api/ingest` endpoint.
3. **Web NFC** -- Android Chrome reads NFC tags in the judge shell
   (`/judge`, My CP tab) and records the arrival via the scores API.

---

## Project Structure

```
lora-kt/
  app/
    __init__.py          # create_app() factory
    models.py            # all SQLAlchemy models
    extensions.py        # db, login_manager, babel
    api/                 # JSON API blueprints
      auth.py            #   auth, users CRUD
      teams.py           #   teams CRUD, randomize
      checkpoints.py     #   checkpoints CRUD, bulk import
      groups.py          #   groups CRUD, reorder
      transfer.py        #   export/import/merge
      helpers.py         #   shared response helpers
    resources/           # additional API resources
      checkins.py        #   check-ins CRUD, CSV export
      ingest.py          #   device message ingest
      lora.py            #   LoRa device management
      scores.py          #   scoring entries
      score_rules.py     #   ScoreField REST API
      rfid.py            #   RFID card management, verify
      map.py             #   map data endpoints
      messages.py        #   raw LoRa messages
      docs_resource.py   #   OpenAPI spec serving
    blueprints/          # HTML view blueprints
      auth/              #   login, register, OAuth
      main/              #   dashboard, competition switcher
      teams/             #   team management UI
      checkpoints/       #   checkpoint management UI
      checkins/          #   check-in list and forms
      groups/            #   group management UI
      map/               #   Google Maps view
      rfid/              #   RFID cards, finish verifier, legacy console (admin-only)
      sheets/            #   Google Sheets admin
      audit/             #   audit log viewer
      scores/            #   scoring UI + /scores/setup admin
      judge/             #   mobile judge shell (/judge)
      judges/            #   judge assignment UI
      firmware/          #   ESP32 firmware flasher
      users/             #   user management UI
      lora/              #   LoRa device UI
      messages/          #   raw messages UI
      docs/              #   Swagger UI
    templates/           # Jinja2 templates (Bootstrap 5)
    static/              # CSS, JS, images
    translations/        # Flask-Babel .po/.mo files (en, sl)
    utils/               # shared utilities
  alembic/               # migration scripts
    env.py               # reads DB URL from Flask config
    versions/            # migration files
  config.py              # Config class (reads env vars)
  run.py                 # development entrypoint
  wsgi.py                # production entrypoint (gunicorn)
  scripts/               # admin/seed scripts
  tests/                 # pytest suite
  docs/                  # documentation
  deploy/                # Docker Compose production config
  serial/                # serial bridge utilities
  ESP_RECEIVER/          # ESP32 receiver firmware
  ESP_TEST/              # ESP32 test firmware
```

---

## Data Model

### Core entities

**Competition** -- top-level container. All other entities are scoped to a
competition. Settings include `public_results` and `hide_gps_map`.

**User** -- authentication entity. Has a global `role` (public/judge/admin/
superadmin) plus per-competition roles via `CompetitionMember`.

**CompetitionMember** -- join table linking users to competitions with a role
(admin/judge/viewer) and an active flag.

**CompetitionInvite** -- token-based invitation to join a competition with a
specific role.

**Team** -- a participating team with name, number, organization, and a DNF
flag. Belongs to one competition. Optionally assigned to one group via
`TeamGroup`.

**RFIDCard** -- maps a physical RFID tag UID to a team (one card per team).

**Checkpoint** -- a location where teams check in. Has coordinates
(easting/northing) and an optional linked LoRa device.

**CheckpointGroup** -- a category of teams: name, `prefix` (e.g., `1xx`,
used for team number randomization), team assignment, and a reference to
a Path plus a direction (`forward`/`reverse`). Groups have an ordering
`position`.

**Path** -- an ordered course through checkpoints, shared between
categories. Two groups running the same course opposite ways reference
one Path row with opposite directions. Route resolution (direction
applied) lives in `app/utils/paths.py` and is the single authority for
start/finish and traversal order.

**PathStop** -- one ordered stop on a Path (unique on path + position, so
a checkpoint may appear twice). Carries `expected_leg_minutes`, the
manual ETA fallback for the judge shell.

**TeamGroup** -- links a team to a checkpoint group with an `active` flag.
Each team should have at most one active group.

**Checkin** -- records that a team arrived at a checkpoint. Has a unique
constraint on (team, checkpoint). Tracks who/what created it (user or device).

**LoRaDevice** -- represents a physical LoRa device or phone. Has a
`dev_num`, optional name, telemetry fields (last_seen, last_rssi, battery).

**LoRaMessage** -- raw message log from device ingest. Stores payload, RSSI,
SNR, and timestamp.

### Scoring

**ScoreEntry** -- judge-assigned scores for a team at a checkpoint, linked to
a check-in. Stores `raw_fields` (JSON) and a computed `total`.

**ScoreField** -- one judged input at a checkpoint: key, label/hint,
structured scoring rule (`rule_type` + `rule_params`), and whether it
counts toward the total. **ScoreFieldGroup** holds per-group
enable/override rows (no row = enabled with defaults).

**TimedSegment** -- a time trial between two checkpoints of a path,
scored by rank spread within each category; endpoints swap automatically
for reverse-direction groups. Computed at read time, never stored in
ScoreEntry.

**GroupScoring** -- category-level rules: found-checkpoint points and the
race time rule (route start -> finish, threshold + stepped penalty, dead
time subtracted). The engine lives in `app/utils/scoring.py`.

### Audit

**AuditEvent** -- append-only log of all significant actions. Records event
type, entity type/ID, actor (user or device), summary text, and a JSON
details field.

### Google Sheets

**SheetConfig** -- stores per-competition configuration for Google Sheets
tabs (checkpoint tabs, arrivals matrix, teams roster, score tabs).

### Firmware

**FirmwareFile** -- uploaded ESP32 firmware binaries for the web flasher.

### Entity relationship summary

```
Competition
  +-- CompetitionMember --> User
  +-- CompetitionInvite
  +-- Team
  |     +-- RFIDCard
  |     +-- Checkin --> Checkpoint
  |     +-- TeamGroup --> CheckpointGroup
  +-- Path
  |     +-- PathStop --> Checkpoint
  |     +-- TimedSegment --> Checkpoint (start/end)
  +-- Checkpoint
  |     +-- ScoreField (+-- ScoreFieldGroup --> CheckpointGroup)
  |     +-- LoRaDevice (optional, 1:1)
  +-- CheckpointGroup --> Path (direction) (+-- GroupScoring)
  +-- LoRaDevice
  +-- LoRaMessage
  +-- SheetConfig
  +-- SheetsSyncJob
  +-- FirmwareFile
  +-- ScoreEntry --> Checkin, Team, Checkpoint
  +-- AuditEvent
```

A full ERD is available at `docs/erd.png` / `docs/erd.pdf`. NOTE: the
rendered images predate the July 2026 redesign (paths + scoring tables +
sheets outbox); regenerate with `scripts/render_erd.py` on a machine
with graphviz installed.

---

## Request Flow

### Typical web request

1. Browser sends request to Flask.
2. `before_request` hooks run:
   - Request logging
   - CSRF protection (for form submissions)
   - Locale selection (Flask-Babel)
   - Current competition resolution (from session)
3. Blueprint route handler executes.
4. SQLAlchemy queries hit the SQLite database.
5. Jinja2 template renders with Bootstrap 5 and context variables.
6. Response returned to browser.

### API request

1. Client sends JSON request with session cookie (or webhook secret for
   ingest).
2. `@json_roles_required` or `@json_login_required` decorator checks
   authentication and per-competition role.
3. Route handler processes the request.
4. Response returned as JSON with appropriate status code.

### Device ingest flow

1. LoRa device sends RFID UID to the receiver.
2. Receiver (ESP32 or serial bridge) POSTs to `/api/ingest` with the
   `X-Webhook-Secret` header.
3. Ingest endpoint:
   - Validates the webhook secret.
   - Stores the raw `LoRaMessage`.
   - Resolves or auto-creates the device and checkpoint.
   - Looks up the RFID UID in `RFIDCard`.
   - If matched, creates a `Checkin` (or deduplicates).
   - Optionally computes a card writeback (HMAC digest).
   - Records audit events.
4. Response includes check-in status and optional card writeback payload.

### Google Sheets sync

Sheets writes go through a durable outbox (`SheetsSyncJob` rows, see
`app/utils/sheets_outbox.py`). Domain code enqueues; the dedicated
`flask sheets-worker` process (its own compose service, exactly one
replica) drains the table with dedup-key coalescing, an accurate global
throttle, exponential backoff, and dead-lettering visible on the sheets
admin page. Roster changes dirty-flag the summary tabs, and a periodic
pass re-verifies/heals tabs. In tests/CLI (`SHEETS_SYNC_INLINE`), writes
run synchronously.

---

## Authentication and Authorization

- **Flask-Login** manages session-based authentication.
- **Google OAuth2** is an alternative login method (optional).
- **Roles** are per-competition via `CompetitionMember`:
  - `admin` -- full access to the competition.
  - `judge` -- can create check-ins, manage teams; checkpoint access can be
    restricted via `JudgeCheckpoint`.
  - `viewer` -- read-only access.
- The global `User.role` field (`public`, `judge`, `admin`, `superadmin`) is
  a legacy fallback; per-competition roles take precedence.

---

## Internationalization

Flask-Babel provides i18n with two languages:

- `en` -- English (default)
- `sl` -- Slovenian

Locale is determined by session preference, then `Accept-Language` header.
Translation files live in `app/translations/`.

---

## Key Design Decisions

1. **SQLite** -- chosen for simplicity and zero-config deployment. Alembic
   uses batch mode for ALTER TABLE operations that SQLite does not natively
   support.

2. **Monolithic Flask** -- all views and API in a single application for ease
   of deployment and development. Blueprints provide logical separation.

3. **Per-competition scoping** -- nearly all queries filter by the current
   competition ID, keeping data isolated between events.

4. **Append-only audit** -- `AuditEvent` records are never deleted or
   modified, providing a reliable history of all changes.

5. **Auto-creation on ingest** -- if a device or checkpoint does not exist
   when a message arrives, the system creates them automatically to avoid
   data loss during events.
