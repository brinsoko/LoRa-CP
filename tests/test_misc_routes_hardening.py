"""Route-level hardening regressions: User.role invariant on user creation,
checkpoint form float parsing, firmware NVS JSON body guard, and validator
error i18n plumbing."""

from __future__ import annotations

import base64

from app.extensions import db
from app.models import Checkpoint, CompetitionMember, FirmwareFile, User
from app.utils.nvs_gen import EncryptedNVS
from tests.support import (
    add_membership,
    create_competition,
    create_device,
    create_user,
    login_as,
)


def _login_admin(client):
    admin = create_user()
    competition = create_competition()
    add_membership(admin, competition, role="admin")
    login_as(client, admin, competition)
    return admin, competition


def test_add_user_keeps_global_role_public(client, app):
    _, competition = _login_admin(client)

    response = client.post(
        "/users/add",
        data={"username": "new-comp-admin", "password": "secret123", "role": "admin"},
        follow_redirects=True,
    )
    assert response.status_code == 200

    created = User.query.filter_by(username="new-comp-admin").one()
    # Global role stays "public"; the per-competition role lives in
    # CompetitionMember only.
    assert created.role == "public"
    membership = CompetitionMember.query.filter_by(
        user_id=created.id, competition_id=competition.id
    ).one()
    assert membership.role == "admin"


def test_checkpoint_add_with_non_numeric_easting_does_not_500(client, app):
    _, competition = _login_admin(client)

    response = client.post(
        "/checkpoints/add",
        data={"name": "Bad CP", "easting": "abc", "northing": "1.0"},
    )
    assert response.status_code == 200
    assert b"must be a number" in response.data
    assert Checkpoint.query.filter_by(competition_id=competition.id, name="Bad CP").first() is None


def test_checkpoint_add_with_non_numeric_device_id_flashes(client, app):
    _, competition = _login_admin(client)

    response = client.post(
        "/checkpoints/add",
        data={"name": "Bad CP 2", "lora_device_id": "xyz"},
    )
    assert response.status_code == 200
    assert b"Device ID must be an integer." in response.data
    assert Checkpoint.query.filter_by(competition_id=competition.id, name="Bad CP 2").first() is None


def _create_firmware(competition) -> FirmwareFile:
    fw = FirmwareFile(
        competition_id=competition.id,
        name="receiver-fw",
        device_type="receiver",
        filename="abc_receiver.bin",
    )
    db.session.add(fw)
    db.session.commit()
    return fw


def test_firmware_nvs_endpoint_tolerates_json_array_body(client, app, monkeypatch):
    _, competition = _login_admin(client)
    device = create_device(competition, dev_num=1)
    fw = _create_firmware(competition)

    monkeypatch.setattr(
        "app.utils.nvs_gen.generate_encrypted_nvs_partition",
        lambda **kwargs: EncryptedNVS(nvs_bin=b"nvs-bytes", keys_bin=b"key-bytes"),
    )

    response = client.post(f"/firmware/api/nvs/{device.id}/{fw.id}", json=["not", "a", "dict"])
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["nvs_bin"] == base64.b64encode(b"nvs-bytes").decode()
    assert payload["keys_bin"] == base64.b64encode(b"key-bytes").decode()


def test_firmware_nvs_endpoint_still_reads_dict_body(client, app, monkeypatch):
    _, competition = _login_admin(client)
    device = create_device(competition, dev_num=2)
    fw = _create_firmware(competition)

    captured = {}

    def fake_generate(**kwargs):
        captured.update(kwargs)
        return EncryptedNVS(nvs_bin=b"nvs", keys_bin=b"keys")

    monkeypatch.setattr("app.utils.nvs_gen.generate_encrypted_nvs_partition", fake_generate)

    response = client.post(
        f"/firmware/api/nvs/{device.id}/{fw.id}",
        json={"wifi_ssid": "scout-net", "wifi_pass": "pw", "ingest_url": "https://x.example/api"},
    )
    assert response.status_code == 200
    assert captured["wifi_ssid"] == "scout-net"
    assert captured["wifi_pass"] == "pw"
    assert captured["ingest_url"] == "https://x.example/api"


def test_competition_create_flashes_validator_error_untranslated_wrap(client, app):
    admin, competition = _login_admin(client)

    response = client.post(
        "/competitions/create",
        data={"name": ""},
        follow_redirects=True,
    )
    assert response.status_code == 200
    # Validator errors are lazy-translated; under the default "en" locale
    # the original message text must come through unchanged.
    assert b"Competition name is required" in response.data
