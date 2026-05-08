from __future__ import annotations


def test_health_returns_200(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.get_json() == {"ok": True}


def test_ready_returns_200_when_db_is_reachable(client):
    response = client.get("/ready")
    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_ready_does_not_require_auth(client):
    # Probes from container orchestrators / Caddy must not need a session.
    response = client.get("/ready")
    assert response.status_code == 200
