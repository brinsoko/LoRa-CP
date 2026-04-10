#  LoRa Checkpoint Tracking System

A full-featured **RFID & LoRa-based checkpoint management platform** built with **Flask**, designed for scouting events, outdoor competitions, or any scenario where teams check in at physical checkpoints.

---

##  Features

- **Multi-competition support:** Create multiple competitions and switch via the competition selector  
- **Per-competition roles:** Admin, Judge, Viewer roles scoped to each competition  
- **Judge checkpoint assignment:** Admins assign one or more checkpoints per judge with a default  
- **Teams:** Create, edit, and manage teams with unique numbers  
- **RFID cards:** Map RFID chips to teams, with optional numeric identifiers  
- **Devices (LoRa or phones):** Manage device IDs and link each to a checkpoint; ingest accepts `/api/devices` (alias of legacy LoRa endpoints)  
- **Web NFC judge tools:** Android Chrome can read a tag UID, call ingest, and append a truncated HMAC payload to the tag for offline proof  
- **Finish-line verifier:** Web NFC page reads tag digests, recomputes HMACs for known devices, and highlights mismatches vs the team’s recorded check-ins  
- **Checkpoints:**  
  - Import from JSON files  
  - Assign to multiple groups  
  - View and edit coordinates (Easting/Northing)  
- **Groups:**  
  - Manage checkpoint groups  
  - Assign checkpoints and teams to multiple groups  
  - Display relationships dynamically  
- **Check-ins:**  
  - Record RFID-based or manual check-ins  
  - Export to CSV for analysis  
- **Map view:**  
  - Visualize checkpoints on Google Maps  
  - Show status per team (found, next, not found)  
  - Auto-color based on progress  
- **Google Sheets automation:** Admin UI to build checkpoint tabs, arrivals matrix, teams roster, and scoreboards in a shared spreadsheet (per competition)  
- **Dark/Light mode:** Follows system preference or user toggle  
- **Audit logs:** Console-based logging for debugging and traceability  

---

##  Tech Stack

- **Backend:** Flask (Python 3.10+)
- **API layer:** Plain Flask blueprints
- **Database:** SQLite (SQLAlchemy ORM via Flask-SQLAlchemy)
- **Frontend:** Bootstrap 5 + Jinja2 templates
- **Mapping:** Google Maps JavaScript API
- **Authentication:** Flask-Login, Google OAuth2, Flask-Babel
- **HTTP integrations:** `requests`
- **Google Sheets integration:** `gspread`, `google-auth`
- **Optional hardware access:** `pyserial`
- **Logging:** Built-in Flask logger with DEBUG output

---

## Dependency Notes

The current app actively uses these runtime libraries:

- Flask
- Flask-SQLAlchemy / SQLAlchemy
- Flask-Login
- Flask-Babel
- requests
- pyserial
- gspread
- google-auth

The repo also contains some packages in `requirements.txt` that are not part of the current documented runtime flow. Those are intentionally not listed here unless they are actually used by the application code.

---

## Local Development

### Requirements

- Python 3.10+
- `venv` recommended
- SQLite for local development

### Setup

```bash
python3 -m venv venv
. venv/bin/activate
make install-dev
```

Create tables and optional demo data:

```bash
make db-init
make seed
```

Run the app locally on `127.0.0.1:5001`:

```bash
make run
```

Useful local helpers:

```bash
make admin                 # create/update admin user
make seed-fresh            # drop/create local DB, then seed demo data
make db-rebuild            # interactive local DB rebuild
make i18n-compile          # compile translations
make openapi-check         # validate docs/openapi.json
make stress-help           # show stress test options
```

### Tests

Run the full suite:

```bash
make test
```

Run the main subsets:

```bash
make test-fast
make test-extended
make test-matrix
make cov
```

There is also a live-target integer-input smoke script:

```bash
make smoke-int BASE_URL=http://127.0.0.1:5001
```

### Docker

```bash
make build
make up
make logs
make down
```

---

##  Google Sheets admin (scoreboard)

- Prereqs: enable Google Sheets API, create a service account, and share the target spreadsheet with the service account email (Editor).  
- Config: set either `GOOGLE_SERVICE_ACCOUNT_FILE` (path to JSON) **or** `GOOGLE_SERVICE_ACCOUNT_JSON` (raw JSON).  
- Access: log in as an Admin and open `/sheets` (navbar “Sheets” button). Paste the spreadsheet ID in the top field so all actions target the same sheet.  
- Wizard: builds checkpoint tabs for every checkpoint with arrived/points/dead time/time columns, optional extra fields per CP, and per-group ordering/exclusions.  
- Build buttons: regenerate Arrivals (matrix of arrivals across checkpoint tabs), Teams (grouped roster), and Score (per-group totals, optional dead time sum).  
- Add tab: create a single checkpoint tab with custom headers/fields. “Sync team numbers” keeps team lists aligned with DB; “Prune missing tabs” removes stale configs if the tab was deleted.  
- Language pack: `/sheets/lang` lets you override tab/column labels for non-English sheets.

![Architecture](docs/architecture.svg)


---

## API Docs

- Swagger UI: `/docs`
- Raw spec: `/docs/openapi.json`

### Auth
Cookie-based session from `/login` (form POST). Many routes are public; judge/admin routes require login. Roles are per competition, and the current competition is selected after login.

For browser-based HTML forms, CSRF protection is enabled. For scripted API calls from a browser session, include the session’s CSRF token header when posting to protected endpoints.

### Quick Calls

Ingest a device message (JSON):
```bash
curl -X POST /api/ingest \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: <secret-if-configured>" \
  -d '{"competition_id":1,"dev_id":1,"payload":"A1B2C3D4","rssi":-62.5,"snr":9.0}'
```

Verify tag digests at finish (server-side recompute):
```bash
curl -X POST /api/rfid/verify \
  -H "Content-Type: application/json" \
  -d '{"uid":"A1B2C3D4","digests":["abcd1234"],"device_ids":[1,2,3]}'
```

### Judge/Finish Web NFC flows
- `/rfid/judge-console`: tap a tag with Android Chrome Web NFC; reads UID, calls ingest for the selected device, and appends the truncated HMAC to the tag as text.
- `/rfid/finish`: tap a tag; reads UID + all text records (digests), recomputes truncated HMACs for known devices, shows matches, collisions, and warns if a digest refers to a checkpoint the team hasn’t checked in at.

### Admin: judge checkpoint assignment
- `/judges/assign`: select a judge, choose allowed checkpoints, and set a default checkpoint.

Export check-ins (CSV):

```bash
curl "/checkins/export.csv?sort=new"
```

Paginated check-in API example:

```bash
curl "/api/checkins?sort=new&page=1&per_page=100"
```
