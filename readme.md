# 🛰️ LoRa Checkpoint Tracking System

A full-featured **RFID & LoRa-based checkpoint management platform** built with **Flask**, designed for scouting events, outdoor competitions, or any scenario where teams check in at physical checkpoints.

---

## 🚀 Features

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

## 🧩 Tech Stack

- **Backend:** Flask (Python 3.10+)
- **Database:** SQLite (SQLAlchemy ORM)
- **Frontend:** Bootstrap 5 + Jinja2 templates
- **Mapping:** Google Maps JavaScript API
- **Authentication:** Flask-Login
- **Logging:** Built-in Flask logger with DEBUG output
- **Environment:** macOS/Linux/Windows (works best with virtualenv)