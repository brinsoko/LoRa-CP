from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


from app import create_app
from app.extensions import db as _db


def build_test_config(tmp_path: Path) -> dict:
    return {
        "TESTING": True,
        "SECRET_KEY": "test-secret",
        "DEVICE_CARD_SECRET": "card-secret",
        "DEVICE_CARD_HMAC_LEN": 8,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}",
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "WTF_CSRF_ENABLED": False,
        "LOGIN_DISABLED": False,
        "BABEL_DEFAULT_LOCALE": "en",
        "LORA_WEBHOOK_SECRET": "CHANGE_LATER",
        "SHEETS_SYNC_ENABLED": False,
        "SERVER_NAME": "localhost",
        "GOOGLE_OAUTH_CLIENT_ID": None,
        "GOOGLE_OAUTH_CLIENT_SECRET": None,
    }


@pytest.fixture
def app_factory(tmp_path):
    created_apps = []

    def _factory(**overrides):
        cfg = build_test_config(tmp_path)
        cfg.update(overrides)
        application = create_app(cfg)
        created_apps.append(application)
        return application

    yield _factory

    for application in reversed(created_apps):
        with application.app_context():
            _db.session.remove()
            _db.drop_all()


@pytest.fixture(scope="function")
def app(tmp_path):
    application = create_app(build_test_config(tmp_path))
    with application.app_context():
        yield application
        _db.session.remove()
        _db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def runner(app):
    return app.test_cli_runner()
