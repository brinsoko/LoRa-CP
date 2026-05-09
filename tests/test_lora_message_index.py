"""Verify the composite index used by /api/ingest's dedup query is created.

The 10-second dedup query filters on (competition_id, dev_id, received_at)
on every LoRa packet. Without a composite index, SQLite picks one of the
single-column indexes and then row-scans by payload, which becomes slow
once the table accumulates traffic during a race.
"""

from __future__ import annotations

from sqlalchemy import inspect

from app.extensions import db


def test_lora_messages_dedup_index_exists(app):
    insp = inspect(db.engine)
    indexes = insp.get_indexes("lora_messages")
    by_name = {ix["name"]: ix for ix in indexes}
    assert "ix_lora_messages_dedup" in by_name, f"composite index missing; have {sorted(by_name.keys())}"
    cols = by_name["ix_lora_messages_dedup"]["column_names"]
    assert cols == ["competition_id", "dev_id", "received_at"], f"wrong columns: {cols}"
