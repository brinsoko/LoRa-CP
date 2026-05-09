"""Regression tests for role-scoping across competitions.

Before: User.role was unioned into the role-set check, so promoting
someone to "admin" in one competition globalized that role and the
user passed admin gates in *every* competition. Fixed by scoping
gate checks to CompetitionMember.role for the active competition,
with a separate explicit "superadmin" bypass on User.role."""

from __future__ import annotations

from tests.support import (
    add_membership,
    create_competition,
    create_user,
    login_as,
)


def test_admin_in_one_comp_cannot_admin_another(client):
    """A user who is admin in comp A but viewer in comp B must not pass
    admin gates while comp B is active."""
    user = create_user(username="cross-comp", role="public")
    comp_a = create_competition(name="CompA")
    comp_b = create_competition(name="CompB")
    add_membership(user, comp_a, role="admin")
    add_membership(user, comp_b, role="viewer")

    # Active context: comp_b → only viewer rights apply.
    login_as(client, user, comp_b)

    resp = client.post(
        "/api/teams",
        json={"name": "ShouldBeForbidden"},
    )
    assert resp.status_code == 403
    assert (resp.get_json() or {}).get("error") == "forbidden"

    # Switching to comp_a → admin gate should now pass.
    login_as(client, user, comp_a)
    resp = client.post(
        "/api/teams",
        json={"name": "ShouldBeAllowed"},
    )
    assert resp.status_code == 201


def test_superadmin_passes_per_comp_gates_without_membership(client):
    """System-level superadmin still bypasses per-competition role
    checks, with no CompetitionMember row needed."""
    user = create_user(username="root", role="superadmin")
    comp = create_competition(name="WildComp")
    # Intentionally no add_membership(user, comp).

    login_as(client, user, comp)
    resp = client.post("/api/teams", json={"name": "RootCanCreate"})
    assert resp.status_code == 201


def test_ingest_password_not_bypassed_by_unrelated_authenticated_user(client):
    """An authenticated user who is not an active admin/judge of the
    target competition cannot bypass its ingest_password.

    Before the fix, *any* current_user.is_authenticated would skip the
    ingest_password gate."""
    owner = create_user(username="owner", role="public")
    outsider = create_user(username="outsider", role="public")
    comp = create_competition(name="GuardedIngest", ingest_password="topsecret")
    add_membership(owner, comp, role="admin")
    # outsider has no membership in this comp.

    login_as(client, outsider, comp)
    resp = client.post(
        "/api/ingest",
        json={
            "competition_id": comp.id,
            "checkpoint_id": 1,
            "payload": "hello",
        },
    )
    # 403 forbidden (ingest password required) — not silently accepted.
    assert resp.status_code == 403
    body = resp.get_json() or {}
    assert body.get("error") == "forbidden"
    assert "Ingest password required." in (body.get("detail") or "")
