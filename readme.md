#  LoRa Checkpoint Tracking System

A full-featured **RFID & LoRa-based checkpoint management platform** built with **Flask**, designed for scouting events, outdoor competitions, or any scenario where teams check in at physical checkpoints.

---

##  Features

- **User roles:** Admin, Judge, and Public views with role-based permissions  
- **Teams:** Create, edit, and manage teams with unique numbers  
- **RFID cards:** Map RFID chips to teams, with optional numeric identifiers  
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
- **Google Sheets automation:** Admin UI to build checkpoint tabs, arrivals matrix, teams roster, and scoreboards in a shared spreadsheet  
- **Dark/Light mode:** Follows system preference or user toggle  
- **Audit logs:** Console-based logging for debugging and traceability  

---

##  Tech Stack

- **Backend:** Flask (Python 3.10+)
- **Database:** SQLite (SQLAlchemy ORM)
- **Frontend:** Bootstrap 5 + Jinja2 templates
- **Mapping:** Google Maps JavaScript API
- **Authentication:** Flask-Login
- **Logging:** Built-in Flask logger with DEBUG output
- **Environment:** macOS/Linux/Windows (works best with virtualenv)

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
- Raw spec: `/docs/openapi.yaml`

### Auth
Cookie-based session from `/login` (form POST). Many routes are public; judge/admin routes require login.

### Quick Calls

Ingest a LoRa message (JSON):
```bash
curl -X POST /api/ingest \
  -H "Content-Type: application/json" \
  -d '{"dev_id":1,"payload":"A1B2C3D4","rssi":-62.5,"snr":9.0}'
  ```

Export check-ins (CSV):

```bash
  curl "/checkins/export.csv?sort=new"
  ```



To do:
- add rfid reader
- design a PCB
- look into upgrading the database, to support multiple competitions
- Look into adding a scoring system (per CP)
- add wifi support for data transfers??
