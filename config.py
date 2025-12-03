import os

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret")
    DEVICE_CARD_SECRET = os.getenv("DEVICE_CARD_SECRET") or SECRET_KEY
    DEVICE_CARD_HMAC_LEN = int(os.getenv("DEVICE_CARD_HMAC_LEN", "12"))
    # Only read DATABASE_URL from env; if missing, app factory will set a proper sqlite path
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # App settings
    LORA_WEBHOOK_SECRET = os.getenv("LORA_WEBHOOK_SECRET", "CHANGE_LATER")

    # Serial defaults
    SERIAL_BAUDRATE = int(os.environ.get("SERIAL_BAUDRATE", "9600"))
    SERIAL_HINT = os.environ.get("SERIAL_HINT", "")
    SERIAL_TIMEOUT = float(os.environ.get("SERIAL_TIMEOUT", "8.0"))

    # Google Sheets / service account
    GOOGLE_SERVICE_ACCOUNT_FILE = os.getenv("GOOGLE_SERVICE_ACCOUNT_FILE")
    GOOGLE_SERVICE_ACCOUNT_JSON = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON")  # optional: raw JSON string
    GOOGLE_SHEETS_SPREADSHEET_ID = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID")
    GOOGLE_SHEETS_TEAMS_SHEET = os.getenv("GOOGLE_SHEETS_TEAMS_SHEET", "Teams")
