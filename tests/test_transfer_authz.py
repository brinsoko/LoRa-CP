"""Cross-competition authorization for export/merge (IDOR guard).

@json_roles_required("admin") validates the role for the session-selected
competition, while export/merge act on the URL comp_id. An admin of
competition A must not be able to export or merge into competition B.
"""

from __future__ import annotations

import pytest

from tests.support import (
    add_membership,
    create_competition,
    create_user,
    login_as,
)

MERGE_PAYLOAD = {
    "schema_version": "1.0.0",
    "competition": {"name": "x"},
    "teams": [],
    "groups": [],
    "checkpoints": [],
}


@pytest.fixture
def two_comps(app, client):
    admin_a = create_user(username="transfer-admin-a")
    comp_a = create_competition(name="Transfer Comp A")
    comp_b = create_competition(name="Transfer Comp B")
    add_membership(admin_a, comp_a, role="admin")
    return admin_a, comp_a, comp_b


class TestExportAuthz:
    def test_admin_of_other_comp_cannot_export(self, client, two_comps):
        admin_a, comp_a, comp_b = two_comps
        login_as(client, admin_a, comp_a)

        resp = client.get(f"/api/competition/{comp_b.id}/export")
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "forbidden"

    def test_admin_can_export_own_comp(self, client, two_comps):
        admin_a, comp_a, _ = two_comps
        login_as(client, admin_a, comp_a)

        resp = client.get(f"/api/competition/{comp_a.id}/export")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["competition"]["name"] == comp_a.name

    def test_superadmin_can_export_any_comp(self, client, two_comps):
        _, comp_a, comp_b = two_comps
        superadmin = create_user(username="transfer-superadmin", role="superadmin")
        login_as(client, superadmin, comp_a)

        for comp in (comp_a, comp_b):
            resp = client.get(f"/api/competition/{comp.id}/export")
            assert resp.status_code == 200
            assert resp.get_json()["competition"]["name"] == comp.name

    def test_inactive_admin_membership_cannot_export(self, client, two_comps):
        admin_a, comp_a, comp_b = two_comps
        add_membership(admin_a, comp_b, role="admin", active=False)
        login_as(client, admin_a, comp_a)

        resp = client.get(f"/api/competition/{comp_b.id}/export")
        assert resp.status_code == 403


class TestMergeAuthz:
    def test_admin_of_other_comp_cannot_merge(self, client, two_comps):
        admin_a, comp_a, comp_b = two_comps
        login_as(client, admin_a, comp_a)

        resp = client.post(f"/api/competition/{comp_b.id}/merge", json=MERGE_PAYLOAD)
        assert resp.status_code == 403
        assert resp.get_json()["error"] == "forbidden"

    def test_admin_can_merge_own_comp(self, client, two_comps):
        admin_a, comp_a, _ = two_comps
        login_as(client, admin_a, comp_a)

        # Dry run (no resolutions key) is enough to prove the guard passes.
        resp = client.post(f"/api/competition/{comp_a.id}/merge", json=MERGE_PAYLOAD)
        assert resp.status_code == 200
        assert resp.get_json()["dry_run"] is True
