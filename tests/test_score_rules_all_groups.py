"""Phase-2 replacement for the old '__all__' score-rule fan-out.

The old model fanned one ScoreRule out to every group linked to a
checkpoint. In the phase-2 model a ScoreField applies to EVERY group by
default: no per-group rows exist until a group diverges, and a
ScoreFieldGroup row with enabled=False hides the field for that group.

These tests pin the equivalents of the old behaviors:
  - creating a field creates no per-group rows and every group resolves
    it (defaults-for-all-groups replaces the fan-out),
  - re-POSTing the same checkpoint+key upserts the single row in place
    instead of duplicating it (the old "resubmit updates existing"),
  - the setup form's group matrix creates enabled=False rows for unticked
    groups and deletes them again on re-tick,
  - GET /api/score-fields/resolved?group_id=__all__ returns the union of
    the per-group field lists in first-seen order (successor of
    GET /api/score-rules/fields).

The old "no groups linked -> flash a warning" test has no equivalent:
field existence no longer depends on group-route links at all.
"""

from __future__ import annotations

from app.models import ScoreField, ScoreFieldGroup
from tests.support import (
    add_membership,
    create_checkpoint,
    create_competition,
    create_group,
    create_score_field,
    create_user,
    login_as,
    set_field_group,
)


def _seed(client):
    user = create_user(username="rules-admin")
    comp = create_competition(name="Rules Race")
    add_membership(user, comp, role="admin")

    cp_shared = create_checkpoint(comp, name="CP-Shared")
    cp_isolated = create_checkpoint(comp, name="CP-Isolated")
    g_alpha = create_group(comp, name="Alpha", prefix="1xx")
    g_beta = create_group(comp, name="Beta", prefix="2xx")
    g_gamma = create_group(comp, name="Gamma", prefix="3xx")

    login_as(client, user, comp)
    return {
        "comp": comp,
        "cp_shared": cp_shared,
        "cp_isolated": cp_isolated,
        "g_alpha": g_alpha,
        "g_beta": g_beta,
        "g_gamma": g_gamma,
    }


def _resolved_keys(client, checkpoint_id: int, group_id) -> list[str]:
    resp = client.get(f"/api/score-fields/resolved?checkpoint_id={checkpoint_id}&group_id={group_id}")
    assert resp.status_code == 200, resp.get_json()
    return resp.get_json()["fields"]


def test_new_field_applies_to_every_group_by_default(client, app):
    s = _seed(client)

    resp = client.post(
        "/api/score-fields",
        json={
            "checkpoint_id": s["cp_shared"].id,
            "key": "task1",
            "rule_type": "multiplier",
            "rule_params": {"factor": 1},
        },
    )
    assert resp.status_code == 201, resp.get_json()

    fields = ScoreField.query.filter_by(
        competition_id=s["comp"].id, checkpoint_id=s["cp_shared"].id
    ).all()
    assert len(fields) == 1
    # Defaults-for-all-groups: no ScoreFieldGroup rows appear until a
    # group actually diverges from the default.
    assert ScoreFieldGroup.query.filter_by(score_field_id=fields[0].id).count() == 0

    # Every group in the competition resolves the field (in the old model
    # only route-linked groups got the fan-out; enablement is now purely
    # a matrix concern, independent of routes).
    for group in (s["g_alpha"], s["g_beta"], s["g_gamma"]):
        assert _resolved_keys(client, s["cp_shared"].id, group.id) == ["task1"]
    # And it stays scoped to its checkpoint.
    assert _resolved_keys(client, s["cp_isolated"].id, s["g_alpha"].id) == []


def test_resubmit_same_key_updates_existing_field(client, app):
    s = _seed(client)

    first = client.post(
        "/api/score-fields",
        json={
            "checkpoint_id": s["cp_shared"].id,
            "key": "task1",
            "rule_type": "multiplier",
            "rule_params": {"factor": 1},
        },
    )
    assert first.status_code == 201
    field_id = first.get_json()["field"]["id"]

    # Submit again with a different factor - the existing row updates in
    # place; no new ScoreField rows appear.
    second = client.post(
        "/api/score-fields",
        json={
            "checkpoint_id": s["cp_shared"].id,
            "key": "task1",
            "rule_type": "multiplier",
            "rule_params": {"factor": 7},
        },
    )
    assert second.status_code == 200
    body = second.get_json()
    assert body["created"] is False
    assert body["field"]["id"] == field_id

    rows = ScoreField.query.filter_by(checkpoint_id=s["cp_shared"].id, key="task1").all()
    assert [r.id for r in rows] == [field_id]
    assert rows[0].rule_params == {"factor": 7}


def test_group_matrix_disables_and_reenables_field_per_group(client, app):
    s = _seed(client)
    field = create_score_field(
        s["cp_shared"], "task1", rule_type="multiplier", rule_params={"factor": 1}
    )

    # Untick Beta and Gamma in the setup form's matrix: enabled=False rows
    # appear only for the disabled groups, Alpha stays row-less.
    resp = client.post(
        "/scores/setup/fields",
        data={
            "checkpoint_id": str(s["cp_shared"].id),
            "matrix_present": "1",
            f"enabled_{field.id}_{s['g_alpha'].id}": "on",
        },
    )
    assert resp.status_code in (200, 302)
    rows = ScoreFieldGroup.query.filter_by(score_field_id=field.id).all()
    assert {(r.group_id, r.enabled) for r in rows} == {
        (s["g_beta"].id, False),
        (s["g_gamma"].id, False),
    }
    assert _resolved_keys(client, s["cp_shared"].id, s["g_alpha"].id) == ["task1"]
    assert _resolved_keys(client, s["cp_shared"].id, s["g_beta"].id) == []

    # Re-tick everything: the divergence rows are deleted (back to the
    # default), not kept around as enabled=True clutter.
    resp = client.post(
        "/scores/setup/fields",
        data={
            "checkpoint_id": str(s["cp_shared"].id),
            "matrix_present": "1",
            f"enabled_{field.id}_{s['g_alpha'].id}": "on",
            f"enabled_{field.id}_{s['g_beta'].id}": "on",
            f"enabled_{field.id}_{s['g_gamma'].id}": "on",
        },
    )
    assert resp.status_code in (200, 302)
    assert ScoreFieldGroup.query.filter_by(score_field_id=field.id).count() == 0
    assert _resolved_keys(client, s["cp_shared"].id, s["g_beta"].id) == ["task1"]


def test_resolved_endpoint_returns_union_for_all_groups(client, app):
    """group_id=__all__ must hand back the union of every group's resolved
    field list so admin tooling can show one coherent field list."""
    s = _seed(client)
    f_task1 = create_score_field(s["cp_shared"], "task1")
    f_task2 = create_score_field(s["cp_shared"], "task2")
    f_topo = create_score_field(s["cp_shared"], "topo")
    # Alpha sees task1+task2, Beta sees task2+topo, Gamma sees nothing
    # (mirrors the old per-group SheetConfig field lists).
    set_field_group(f_topo, s["g_alpha"], enabled=False)
    set_field_group(f_task1, s["g_beta"], enabled=False)
    for f in (f_task1, f_task2, f_topo):
        set_field_group(f, s["g_gamma"], enabled=False)

    assert _resolved_keys(client, s["cp_shared"].id, s["g_alpha"].id) == ["task1", "task2"]
    assert _resolved_keys(client, s["cp_shared"].id, s["g_beta"].id) == ["task2", "topo"]
    assert _resolved_keys(client, s["cp_shared"].id, s["g_gamma"].id) == []

    resp = client.get(
        f"/api/score-fields/resolved?checkpoint_id={s['cp_shared'].id}&group_id=__all__"
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body.get("all_groups") is True
    # First-seen order preserved across the union.
    assert body["fields"] == ["task1", "task2", "topo"]
    assert [f["key"] for f in body["resolved"]] == ["task1", "task2", "topo"]
