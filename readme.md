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

## API Docs

- Swagger UI: `http://localhost:5001/docs`
- Raw spec: `http://localhost:5001/docs/openapi.yaml`

### Auth
Cookie-based session from `/login` (form POST). Many routes are public; judge/admin routes require login.

### Quick Calls

Ingest a LoRa message (JSON):
```bash
curl -X POST http://localhost:5001/api/ingest \
  -H "Content-Type: application/json" \
  -d '{"dev_id":1,"payload":"A1B2C3D4","rssi":-62.5,"snr":9.0}'
  ```

Export check-ins (CSV):

```bash
  curl "http://localhost:5001/checkins/export.csv?sort=new"
  ```



To do:
- add rfid reader
- add SD card support, when it does not recieve ACK
- design a PCB
- Fix import checkpoints to a specific coordinate system
- look into adding mailing client for registration via email (public deployment)
- look into upgrading the database, to support multiple competitions
- Look into adding a scoring system (per CP)
- fix serial data from LoRa module
- add wifi support for data transfers??
