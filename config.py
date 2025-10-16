import os

class Config:
    SECRET_KEY = os.environ.get('FLASK_SECRET_KEY', 'dev-secret')
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL', 'sqlite:///database.db')
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Serial defaults
    SERIAL_BAUDRATE = int(os.environ.get("SERIAL_BAUDRATE", "9600"))
    SERIAL_HINT = os.environ.get("SERIAL_HINT", "")
    SERIAL_TIMEOUT = float(os.environ.get("SERIAL_TIMEOUT", "8.0"))