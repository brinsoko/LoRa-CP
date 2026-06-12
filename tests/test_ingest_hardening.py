"""Hardening tests for /api/ingest.

Covers three failure modes seen with two gunicorn workers and flaky
device clocks:

1. Device-supplied "ts" values that are absurd (epoch reset, far future)
   must not be stored or anchor the dedup window; the server clock wins.
2. The first-seen dev_num get-or-create can race between workers; the
   loser must recover the winner's row instead of 500-ing and dropping a
   legitimate arrival. We cannot truly race single-threaded SQLite, so we
   inject the conflicting row on the session connection just before the
   SAVEPOINT opens, which reproduces the loser's view exactly.

The test config leaves LORA_WEBHOOK_SECRET at the dev default
("CHANGE_LATER"), so the endpoint is open and no X-Webhook-Secret header
is needed (same pattern as TestIngestApi in test_lora_cp.py).
"""

from __future__ import annotations

import logging
import time

from sqlalchemy import insert

from app.extensions import db
from app.models import Checkpoint, LoRaDevice, LoRaMessage
from app.utils.time import utc_from_timestamp_naive, utcnow_naive
from tests.support import create_competition, create_device


def _post_ingest(client, competition, *, dev_id: int, payload: str, ts: int | None = None):
    body = {"competition_id": competition.id, "dev_id": dev_id, "payload": payload}
    if ts is not None:
        body["ts"] = ts
    return client.post("/api/ingest", json=body)


def _stored_received_at(response):
    message_id = response.get_json()["message_id"]
    msg = db.session.get(LoRaMessage, message_id)
    assert msg is not None
    return msg.received_at


class TestDeviceTimestampBounds:
    def test_ts_zero_falls_back_to_server_time(self, client, app):
        competition = create_competition(name="TS Zero Race")
        response = _post_ingest(client, competition, dev_id=1, payload="TS-ZERO", ts=0)

        assert response.status_code == 201
        received_at = _stored_received_at(response)
        assert abs((received_at - utcnow_naive()).total_seconds()) < 30

    def test_ts_far_in_past_falls_back_to_server_time(self, client, app, caplog):
        competition = create_competition(name="TS Past Race")
        stale = int(time.time()) - 30 * 24 * 3600  # a month ago, way past 48 h

        with caplog.at_level(logging.WARNING):
            response = _post_ingest(client, competition, dev_id=2, payload="TS-PAST", ts=stale)

        assert response.status_code == 201
        received_at = _stored_received_at(response)
        assert abs((received_at - utcnow_naive()).total_seconds()) < 30
        assert any("out of bounds" in rec.getMessage() for rec in caplog.records)

    def test_ts_far_in_future_falls_back_to_server_time(self, client, app, caplog):
        competition = create_competition(name="TS Future Race")
        future = int(time.time()) + 24 * 3600  # a day ahead, way past 10 min

        with caplog.at_level(logging.WARNING):
            response = _post_ingest(client, competition, dev_id=3, payload="TS-FUTURE", ts=future)

        assert response.status_code == 201
        received_at = _stored_received_at(response)
        assert abs((received_at - utcnow_naive()).total_seconds()) < 30
        assert any("dev_num=3" in rec.getMessage() for rec in caplog.records)

    def test_ts_overflowing_unix_range_falls_back_to_server_time(self, client, app):
        competition = create_competition(name="TS Overflow Race")
        response = _post_ingest(client, competition, dev_id=4, payload="TS-HUGE", ts=10**18)

        assert response.status_code == 201
        received_at = _stored_received_at(response)
        assert abs((received_at - utcnow_naive()).total_seconds()) < 30

    def test_recent_ts_is_preserved_exactly(self, client, app):
        competition = create_competition(name="TS Recent Race")
        recent = int(time.time()) - 3600  # an hour ago, well inside 48 h

        response = _post_ingest(client, competition, dev_id=5, payload="TS-RECENT", ts=recent)

        assert response.status_code == 201
        assert _stored_received_at(response) == utc_from_timestamp_naive(recent)


class TestGetOrCreateRace:
    def _race_begin_nested(self, monkeypatch, inject):
        """Patch db.session.begin_nested so the first call runs `inject`
        on the session connection right before the SAVEPOINT opens. That
        is exactly the loser's timeline: the existence check saw nothing,
        the winner's row landed, then our insert hits the constraint."""
        real_begin_nested = db.session.begin_nested
        state = {"done": False}

        def begin_nested_with_race():
            if not state["done"]:
                state["done"] = True
                inject()
            return real_begin_nested()

        monkeypatch.setattr(db.session, "begin_nested", begin_nested_with_race)

    def test_device_insert_race_recovers_existing_row(self, client, app, monkeypatch):
        competition = create_competition(name="Device Race")

        def inject_winner_device():
            db.session.execute(
                insert(LoRaDevice).values(
                    competition_id=competition.id,
                    dev_num=77,
                    name="DEV-77",
                    active=True,
                )
            )

        self._race_begin_nested(monkeypatch, inject_winner_device)

        response = _post_ingest(client, competition, dev_id=77, payload="RACE-DEV")

        assert response.status_code == 201
        assert response.get_json()["ok"] is True
        devices = LoRaDevice.query.filter_by(competition_id=competition.id, dev_num=77).all()
        assert len(devices) == 1
        # The arrival resolved to the winner's device and still produced
        # the auto-created checkpoint linked to it.
        checkpoint = Checkpoint.query.filter_by(competition_id=competition.id, lora_device_id=devices[0].id).one()
        assert checkpoint.name == "Device 77"
        assert devices[0].last_seen is not None

    def test_checkpoint_insert_race_recovers_existing_row(self, client, app, monkeypatch):
        competition = create_competition(name="Checkpoint Race")
        device = create_device(competition, dev_num=88, name="DEV-88")

        def inject_winner_checkpoint():
            db.session.execute(
                insert(Checkpoint).values(
                    competition_id=competition.id,
                    name="Device 88",
                    description="Auto-created from device ingest",
                    lora_device_id=device.id,
                )
            )

        self._race_begin_nested(monkeypatch, inject_winner_checkpoint)

        response = _post_ingest(client, competition, dev_id=88, payload="RACE-CP")

        assert response.status_code == 201
        assert response.get_json()["ok"] is True
        checkpoints = Checkpoint.query.filter_by(competition_id=competition.id, lora_device_id=device.id).all()
        assert len(checkpoints) == 1
        assert response.get_json()["checkpoint"] == "Device 88"
