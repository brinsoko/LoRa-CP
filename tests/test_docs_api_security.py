from __future__ import annotations

from tests.support import (
    add_membership,
    create_competition,
    create_user,
    login_as,
)


def test_docs_api_requires_login(client):
    response = client.get("/api/docs")
    assert response.status_code == 401


def test_docs_api_blocks_path_traversal(client):
    user = create_user(username="docs-reader")
    competition = create_competition(name="Docs Comp")
    add_membership(user, competition, role="admin")
    login_as(client, user, competition)

    # Classic and double-encoded traversal attempts must not escape the docs dir.
    for attempt in (
        "../../etc/passwd",
        "....//....//etc/passwd",
        "..%2F..%2Fetc%2Fpasswd",
        "%2e%2e%2f%2e%2e%2fetc%2fpasswd",
    ):
        response = client.get(f"/api/docs/{attempt}")
        assert response.status_code in (400, 404), f"unexpected status for {attempt}: {response.status_code}"
        # /etc/passwd content should never appear.
        assert b"root:" not in response.data
