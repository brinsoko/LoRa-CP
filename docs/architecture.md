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
3. **Web NFC** -- Android Chrome reads NFC tags and calls ingest directly from
   the judge console page.

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
      score_rules.py     #   scoring rule config
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
      rfid/              #   NFC judge console, finish verifier
      sheets/            #   Google Sheets admin
      audit/             #   audit log viewer
      scores/            #   scoring UI
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

**CheckpointGroup** -- a named grouping of checkpoints with a `prefix` field
(e.g., `1xx`) used for team number randomization. Groups have an ordering
`position`.

**CheckpointGroupLink** -- many-to-many between checkpoints and groups with
a `position` for ordering within the group.

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

**ScoreRule** -- per-checkpoint scoring rules scoped to a group.

**GlobalScoreRule** -- competition-wide scoring rules scoped to a group.

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
  +-- Checkpoint
  |     +-- CheckpointGroupLink --> CheckpointGroup
  |     +-- LoRaDevice (optional, 1:1)
  +-- CheckpointGroup
  +-- LoRaDevice
  +-- LoRaMessage
  +-- SheetConfig
  +-- FirmwareFile
  +-- ScoreEntry --> Checkin, Team, Checkpoint
  +-- ScoreRule --> Checkpoint, CheckpointGroup
  +-- GlobalScoreRule --> CheckpointGroup
  +-- AuditEvent
```

A full ERD is available at `docs/erd.png` / `docs/erd.pdf`.

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

When a check-in is created (manually or via ingest), the system optionally
calls `mark_arrival_checkbox()` to update the Google Sheets arrivals matrix.
This runs synchronously but failures are caught and ignored to avoid blocking
the primary operation.

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
