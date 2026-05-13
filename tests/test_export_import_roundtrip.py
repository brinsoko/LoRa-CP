"""Regression tests for fields the export/import path used to drop.

The audit pass found that:
  - Checkpoint.is_virtual was missing from the export payload, so a
    virtual checkpoint imported into another competition came back as
    a regular one.
  - Checkin.created_by_user_id / created_by_device_id were missing,
    so the audit trail of "who logged this check-in" was lost on
    round-trip.

These tests build a competition with the relevant fields populated,
export it, import the JSON into a fresh competition, and assert
each field made it across."""

from __future__ import annotations

import io
import json

from app.models import Checkin, Checkpoint, Competition, LoRaDevice
from tests.support import (
    add_membership,
    create_checkin,
    create_checkpoint,
    create_competition,
    create_device,
    create_team,
    create_user,
    login_as,
)


def _export(client, competition):
    resp = client.get(f"/api/competition/{competition.id}/export")
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()


def _import(client, payload, new_name: str):
    payload = dict(payload)
    payload["competition"] = dict(payload["competition"])
    payload["competition"]["name"] = new_name
    data = io.BytesIO(json.dumps(payload).encode("utf-8"))
    resp = client.post(
        "/api/competition/import",
        data={"file": (data, "export.json")},
        content_type="multipart/form-data",
    )
    assert resp.status_code in (200, 201), resp.get_json()
    body = resp.get_json() or {}
    new_id = body.get("competition_id") or body.get("id")
    if new_id is None:
        # Some routes return the competition under a key.
        if isinstance(body.get("competition"), dict):
            new_id = body["competition"].get("id")
    if new_id is None:
        # Fall back to looking it up by name.
        comp = Competition.query.filter_by(name=new_name).first()
        assert comp is not None
        new_id = comp.id
    return new_id


def test_is_virtual_round_trips_through_export_import(client, app):
    user = create_user(username="rt-admin-virt", role="admin")
    comp = create_competition(name="RTSrcVirt")
    add_membership(user, comp, role="admin")
    create_checkpoint(comp, name="Real")
    create_checkpoint(comp, name="Virtual1")
    # Manually flip the second checkpoint to virtual since the helper
    # doesn't expose the field.
    Checkpoint.query.filter_by(name="Virtual1", competition_id=comp.id).update(
        {Checkpoint.is_virtual: True}
    )
    from app.extensions import db
    db.session.commit()

    login_as(client, user, comp)
    payload = _export(client, comp)

    # Sanity: the export now actually includes the flag.
    cps = {cp["name"]: cp for cp in payload["checkpoints"]}
    assert cps["Virtual1"].get("is_virtual") is True, payload["checkpoints"]
    assert cps["Real"].get("is_virtual") is False, payload["checkpoints"]

    new_comp_id = _import(client, payload, new_name="RTDstVirt")

    imported = (
        Checkpoint.query.filter_by(competition_id=new_comp_id, name="Virtual1").first()
    )
    real = (
        Checkpoint.query.filter_by(competition_id=new_comp_id, name="Real").first()
    )
    assert imported is not None and imported.is_virtual is True
    assert real is not None and real.is_virtual is False


def test_checkin_created_by_round_trips_through_export_import(client, app):
    creator = create_user(username="rt-creator", role="public")
    admin = create_user(username="rt-admin-author", role="admin")
    comp = create_competition(name="RTSrcAuthor")
    add_membership(admin, comp, role="admin")
    add_membership(creator, comp, role="judge")

    device = create_device(comp, dev_num=42, name="Device-42")
    cp = create_checkpoint(comp, name="CP-Auth")
    team = create_team(comp, name="Team-Auth", number=901)

    # User-attributed check-in.
    create_checkin(comp, team, cp, created_by_user=creator)
    # Device-attributed check-in.
    other_cp = create_checkpoint(comp, name="CP-Auth2")
    create_checkin(comp, team, other_cp, created_by_device=device)

    login_as(client, admin, comp)
    payload = _export(client, comp)

    # Sanity: the export carries who-created.
    by_cp = {ci["checkpoint_name"]: ci for ci in payload["checkins"]}
    assert by_cp["CP-Auth"]["created_by_username"] == "rt-creator", payload["checkins"]
    assert by_cp["CP-Auth"]["created_by_dev_num"] is None
    assert by_cp["CP-Auth2"]["created_by_dev_num"] == 42, payload["checkins"]
    assert by_cp["CP-Auth2"]["created_by_username"] is None

    new_comp_id = _import(client, payload, new_name="RTDstAuthor")

    new_user_ci = (
        Checkin.query.join(Checkpoint, Checkin.checkpoint_id == Checkpoint.id)
        .filter(
            Checkin.competition_id == new_comp_id,
            Checkpoint.name == "CP-Auth",
        )
        .first()
    )
    assert new_user_ci is not None
    assert new_user_ci.created_by_user_id == creator.id, (
        "creator user link did not round-trip"
    )

    new_device_ci = (
        Checkin.query.join(Checkpoint, Checkin.checkpoint_id == Checkpoint.id)
        .filter(
            Checkin.competition_id == new_comp_id,
            Checkpoint.name == "CP-Auth2",
        )
        .first()
    )
    new_device = LoRaDevice.query.filter_by(
        competition_id=new_comp_id, dev_num=42
    ).first()
    assert new_device is not None, "device was not imported"
    assert new_device_ci is not None
    assert new_device_ci.created_by_device_id == new_device.id, (
        "device link did not resolve in the destination competition"
    )
