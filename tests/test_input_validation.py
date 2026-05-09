"""Boundary-input validation: GPS lat/lon ranges, finite floats, dev_num > 0."""

from __future__ import annotations

import math

import pytest

from app.utils.validators import validate_finite_float, validate_positive_int

# -- pure unit tests for the validator helpers --


@pytest.mark.parametrize(
    "value, kwargs, expected_err_substr",
    [
        ("not a number", {"field_name": "x"}, "must be a number"),
        (math.nan, {"field_name": "x"}, "finite"),
        (math.inf, {"field_name": "x"}, "finite"),
        (-math.inf, {"field_name": "x"}, "finite"),
        (-91, {"field_name": "lat", "minimum": -90, "maximum": 90}, ">= -90"),
        (91, {"field_name": "lat", "minimum": -90, "maximum": 90}, "<= 90"),
    ],
)
def test_validate_finite_float_rejects(value, kwargs, expected_err_substr):
    parsed, err = validate_finite_float(value, **kwargs)
    assert parsed is None
    assert err is not None and expected_err_substr in err


@pytest.mark.parametrize("value", [None, "", "0.5", 0, 90, -90, 1.5e10])
def test_validate_finite_float_accepts(value):
    # 1.5e10 is finite — large but valid for non-bounded fields.
    parsed, err = validate_finite_float(value, field_name="x")
    assert err is None


def test_validate_positive_int_rejects_zero_and_negative():
    for v in (0, -1, -100):
        parsed, err = validate_positive_int(v, field_name="dev_num")
        assert parsed is None
        assert err and "> 0" in err


def test_validate_positive_int_accepts_positive():
    parsed, err = validate_positive_int(5, field_name="dev_num")
    assert parsed == 5
    assert err is None


# -- integration test for ingest GPS bounds --


def test_ingest_rejects_gps_lat_out_of_range(client, app):
    from tests.support import create_competition

    competition = create_competition(name="GPS Bounds")

    response = client.post(
        "/api/ingest",
        json={
            "competition_id": competition.id,
            "dev_id": 1,
            "payload": "AABBCCDD",
            "gps_lat": 91.0,  # above the 90.0 ceiling
            "gps_lon": 0.0,
        },
    )
    assert response.status_code == 400


def test_ingest_rejects_gps_lat_nan(client, app):
    from tests.support import create_competition

    competition = create_competition(name="GPS NaN")

    # NaN can't survive JSON round-trip, but devices may send "NaN" as a string.
    response = client.post(
        "/api/ingest",
        json={
            "competition_id": competition.id,
            "dev_id": 1,
            "payload": "AABBCCDD",
            "gps_lat": "NaN",
            "gps_lon": "0",
        },
    )
    assert response.status_code == 400


def test_ingest_rejects_negative_dev_id(client, app):
    from tests.support import create_competition

    competition = create_competition(name="Neg Dev")

    response = client.post(
        "/api/ingest",
        json={
            "competition_id": competition.id,
            "dev_id": -3,
            "payload": "AABBCCDD",
        },
    )
    assert response.status_code == 400


def test_checkpoint_create_rejects_inf_easting(client, app):
    from tests.support import (
        add_membership,
        create_competition,
        create_user,
        login_as,
    )

    admin = create_user(username="cp-validate-admin")
    competition = create_competition(name="CP Validate")
    add_membership(admin, competition, role="admin")
    login_as(client, admin, competition)

    response = client.post(
        "/api/checkpoints",
        json={"name": "Bad CP", "easting": "Infinity", "northing": 100.0},
    )
    assert response.status_code == 400
    body = response.get_json()
    assert "easting" in (body.get("detail") or "")
