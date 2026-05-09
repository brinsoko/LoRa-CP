"""Regression tests for role-scoping across competitions.

Two complementary defences are exercised here:

  1) Session sanity check (app/utils/competition.py): you cannot have
     an active competition you don't belong to. If the session points
     somewhere stale, get_current_competition_id() pops it and falls
     back to a competition you ARE a member of (or None).

  2) Role scoping (app/utils/perms.py, app/utils/rest_auth.py): for
     whichever competition is active, only CompetitionMember.role for
     that competition counts; the global User.role field is consulted
     only for the explicit "superadmin" system bypass.

Before the fix, defence (2) was leaky: User.role was unioned into the
role-set, so promoting someone to "admin" in one competition globalized
that role and they passed admin gates in every competition. Defence (1)
existed before and is unchanged."""

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


def test_user_with_no_memberships_cannot_act(client):
    """An authenticated user with zero CompetitionMember rows anywhere
    must not be able to act on protected endpoints, even if their
    session points at a competition. require_current_competition_id()
    should fail to resolve a comp_id and json_roles_required should
    return 400 no_competition (not 201, not 403 admin-success)."""
    user = create_user(username="orphan-user", role="public")
    comp = create_competition(name="Orphanage")
    # No add_membership for this user, anywhere.

    login_as(client, user, comp)
    resp = client.post("/api/teams", json={"name": "ShouldBeBlocked"})
    body = resp.get_json() or {}
    # 400 no_competition is the contract: nothing to scope role checks
    # against, so the request is structurally invalid for this user.
    # 403 forbidden would also be acceptable; what matters is that 201
    # never happens.
    assert resp.status_code in (400, 403), body
    assert body.get("error") in ("no_competition", "forbidden"), body


def test_session_with_revoked_membership_redirects_to_valid_comp(client):
    """If the session's competition_id points at a competition the user
    no longer belongs to (e.g. they were removed after login), the
    next request should NOT execute against that competition. The
    auto-switch in get_current_competition_id should fall back to a
    competition where the user does have an active membership."""
    user = create_user(username="kicked-user", role="public")
    comp_stale = create_competition(name="StaleComp")
    comp_real = create_competition(name="RealComp")
    add_membership(user, comp_real, role="admin")
    # Membership in comp_stale was never created (or imagine it was
    # revoked since the session was set).

    login_as(client, user, comp_stale)
    # GET /api/teams returns the team list for whatever the *resolved*
    # current competition is. With the auto-switch, that should be
    # comp_real, where the user is admin.
    resp = client.get("/api/teams")
    assert resp.status_code == 200, resp.get_json()

    # Sanity: a write that requires admin should also succeed because
    # the resolved competition is comp_real where this user is admin.
    write = client.post("/api/teams", json={"name": "AfterSwap"})
    assert write.status_code == 201, write.get_json()


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
