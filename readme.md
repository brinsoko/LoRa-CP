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
- **Database:** SQLite (SQLAlchemy ORM)
- **Frontend:** Bootstrap 5 + Jinja2 templates
- **Mapping:** Google Maps JavaScript API
- **Authentication:** Flask-Login, Google OAuth2
- **Logging:** Built-in Flask logger with DEBUG output

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

### Quick Calls

Ingest a device message (JSON):
```bash
curl -X POST /api/ingest \
  -H "Content-Type: application/json" \
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



To do:
- design a PCB
- add wifi support for data transfers??
