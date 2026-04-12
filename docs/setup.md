# Detailed Installation Guide

This guide covers setting up LoRa-CP for local development from scratch,
including Alembic migrations.

## Prerequisites

- Python 3.10 or newer
- `pip` and `venv` (included with most Python installs)
- SQLite (ships with Python; no separate install needed)
- Git

Optional:
- Google Cloud service account (for Sheets integration)
- Google OAuth credentials (for Google login)

## 1. Clone and create a virtual environment

```bash
git clone https://github.com/brinsoko/LoRa-CP.git lora-kt
cd lora-kt
python3 -m venv venv
source venv/bin/activate   # macOS/Linux
# venv\Scripts\activate    # Windows
```

## 2. Install dependencies

```bash
make install          # runtime only
# or
make install-dev      # runtime + pytest, coverage, etc.
```

If you do not use `make`:

```bash
pip install -r requirements.txt
pip install -r requirements-dev.txt   # optional, for tests
```

## 3. Environment variables

Create a `.env` file in the project root (it is gitignored). At minimum:

```bash
SECRET_KEY=some-random-string
LORA_WEBHOOK_SECRET=another-random-string
```

Full reference:

| Variable | Default | Notes |
|---|---|---|
| `SECRET_KEY` | `dev-secret` | **Required in production.** |
| `DATABASE_URL` | `sqlite:///instance/app.db` | SQLAlchemy URI. Leave unset for local SQLite. |
| `LORA_WEBHOOK_SECRET` | `CHANGE_LATER` | **Required in production.** Protects `/api/ingest`. |
| `GOOGLE_OAUTH_CLIENT_ID` | - | For Google login. |
| `GOOGLE_OAUTH_CLIENT_SECRET` | - | For Google login. |
| `GOOGLE_SERVICE_ACCOUNT_FILE` | - | Path to service account JSON (Sheets). |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | - | Raw JSON string alternative to the file above. |
| `GOOGLE_SHEETS_SPREADSHEET_ID` | - | Default spreadsheet ID for Sheets admin. |
| `SHEETS_SYNC_ENABLED` | `true` | Set to `false` to disable automatic Sheets sync. |
| `SERIAL_BAUDRATE` | `9600` | Baud rate for serial LoRa bridge. |
| `SERIAL_HINT` | - | Substring hint for auto-detecting serial port. |
| `SERIAL_TIMEOUT` | `8.0` | Serial read timeout in seconds. |
| `SEED_ADMIN_USER` | `admin` | Username for seeded admin account. |
| `SEED_ADMIN_PASS` | `admin123` | Password for seeded admin account. |
| `DEVICE_CARD_SECRET` | value of `SECRET_KEY` | HMAC key for NFC card digests. |
| `DEVICE_CARD_HMAC_LEN` | `12` | Truncated HMAC length (characters). |
| `WTF_CSRF_ENABLED` | `true` | Disable CSRF for API-only testing if needed. |

## 4. Initialize the database

```bash
make db-init
```

This runs `db.create_all()` inside a Flask app context, creating all tables in
the SQLite database at `instance/app.db`.

## 5. Seed data

```bash
make seed             # creates admin user + demo data
make admin            # create/update just the admin user
```

To skip demo data:

```bash
make seed SEED_SKIP_DEMO=1
```

To import teams from a CSV file:

```bash
make seed SEED_TEAMS_CSV=path/to/teams.csv
```

To drop the database and start fresh:

```bash
make seed-fresh
```

## 6. Compile translations

```bash
make i18n-compile
```

This compiles `.po` files to `.mo` so Flask-Babel can serve Slovenian
translations.

## 7. Run the development server

```bash
make run
```

Opens on `http://127.0.0.1:5001` with debug mode enabled.

## 8. Run tests

```bash
make test             # full suite
make test-fast        # core tests only
make cov              # tests with coverage report
```

---

## Alembic Migrations

Alembic is configured in `alembic.ini` and `alembic/env.py`. The env file
creates a Flask app context to read `SQLALCHEMY_DATABASE_URI` from Flask
config, so the database URL is always consistent.

Batch mode (`render_as_batch=True`) is enabled for SQLite compatibility.

### Common commands

```bash
# Show current migration state
alembic current

# View history
alembic history --verbose

# Generate a new auto-detected migration
alembic revision --autogenerate -m "add foo column to bar"

# Apply all pending migrations
alembic upgrade head

# Downgrade by one revision
alembic downgrade -1

# Upgrade to a specific revision
alembic upgrade <revision_id>
```

### Workflow after changing models

1. Edit `app/models.py`.
2. Generate migration: `alembic revision --autogenerate -m "describe change"`.
3. Review the generated file in `alembic/versions/`.
4. Apply: `alembic upgrade head`.
5. Commit the migration file along with the model change.

### Stamping an existing database

If you created tables with `db.create_all()` (no Alembic history yet), stamp
the current state so Alembic knows where you are:

```bash
alembic stamp head
```

### Migration files

Migrations live in `alembic/versions/`. Each file has `upgrade()` and
`downgrade()` functions. Always review auto-generated migrations before
applying -- autogenerate does not detect all changes (e.g., renamed columns).

---

## Docker

```bash
make build            # build the image
make up               # start containers (docker-compose.yml)
make logs             # tail logs
make down             # stop containers
```

For production Docker deployment with HTTPS, see [deploy.md](deploy.md).

---

## Troubleshooting

**"FATAL: SECRET_KEY must be set in production."**
Set `FLASK_ENV=development` locally, or export a real `SECRET_KEY`.

**Tables not created / missing columns**
The app auto-creates tables on startup via `db.create_all()` and runs
inline `ALTER TABLE` statements for newer columns. For a clean start,
use `make seed-fresh` or `make db-rebuild`.

**Alembic "Target database is not up to date"**
Run `alembic upgrade head` to apply pending migrations, or `alembic stamp head`
if the schema is already current but Alembic has no record of it.

**Translation strings not appearing**
Run `make i18n-compile` after editing `.po` files.
