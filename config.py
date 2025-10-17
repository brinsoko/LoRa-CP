import os

class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret")
    # Only read DATABASE_URL from env; if missing, app factory will set a proper sqlite path
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # App settings
    LORA_WEBHOOK_SECRET = os.getenv("LORA_WEBHOOK_SECRET", "CHANGE_LATER")

    # Serial defaults
    SERIAL_BAUDRATE = int(os.environ.get("SERIAL_BAUDRATE", "9600"))
    SERIAL_HINT = os.environ.get("SERIAL_HINT", "")
    SERIAL_TIMEOUT = float(os.environ.get("SERIAL_TIMEOUT", "8.0"))