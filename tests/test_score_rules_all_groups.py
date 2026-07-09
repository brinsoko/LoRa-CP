"""QoL: an "All groups" option on the score-rules form fans the same rule
out to every group linked to the chosen checkpoint, so admins don't have
to click through 6 categories that all use the same scoring formula.

These tests pin three things:
  - POSTing /scores/rules with group_id=__all__ creates one ScoreRule per
    linked CheckpointGroup, and skips groups that aren't linked to that
    checkpoint.
  - A second POST with the same payload updates the existing rules
    instead of duplicating them.
  - GET /api/score-rules/fields?group_id=__all__ returns the union of
    fields across every linked group, so the rule builder can show one
    coherent field list."""

from __future__ import annotations

import json

from app.extensions import db
from app.models import Path, PathStop, ScoreRule, SheetConfig
from tests.support import (
    add_membership,
    create_checkpoint,
    create_competition,
    create_group,
    create_user,
    login_as,
)


def _link(group, checkpoint, position=0):
    """Append the checkpoint to the group's route (creates the path lazily)."""
    path = group.path
    if path is None:
        path = Path(competition_id=group.competition_id, name=f"{group.name} path")
        db.session.add(path)
        db.session.flush()
        group.path_id = path.id
    next_pos = max((s.position for s in path.stops), default=-1) + 1
    db.session.add(PathStop(path_id=path.id, checkpoint_id=checkpoint.id, position=next_pos))


def _seed(client):
    user = create_user(username="rules-admin", role="admin")
    comp = create_competition(name="Rules Race")
    add_membership(user, comp, role="admin")

    cp_shared = create_checkpoint(comp, name="CP-Shared")
    cp_isolated = create_checkpoint(comp, name="CP-Isolated")
    g_alpha = create_group(comp, name="Alpha", prefix="1xx")
    g_beta = create_group(comp, name="Beta", prefix="2xx")
    g_gamma = create_group(comp, name="Gamma", prefix="3xx")  # not linked to cp_shared

    _link(g_alpha, cp_shared, position=0)
    _link(g_beta, cp_shared, position=1)
    _link(g_gamma, cp_isolated, position=0)
    db.session.commit()

    login_as(client, user, comp)
    return {
        "comp": comp,
        "cp_shared": cp_shared,
        "cp_isolated": cp_isolated,
        "g_alpha": g_alpha,
        "g_beta": g_beta,
        "g_gamma": g_gamma,
    }


def _rules_json():
    return json.dumps(
        {
            "field_rules": {"task1": {"type": "multiplier", "factor": 1}},
            "total_fields": ["task1"],
        }
    )


def test_all_groups_creates_one_rule_per_linked_group(client, app):
    s = _seed(client)

    resp = client.post(
        "/scores/rules",
        data={
            "checkpoint_id": str(s["cp_shared"].id),
            "group_id": "__all__",
            "rules_json": _rules_json(),
        },
        follow_redirects=False,
    )
    assert resp.status_code in (200, 302)

    landed = ScoreRule.query.filter_by(
        competition_id=s["comp"].id, checkpoint_id=s["cp_shared"].id
    ).all()
    landed_group_ids = sorted(r.group_id for r in landed)
    expected = sorted([s["g_alpha"].id, s["g_beta"].id])
    assert landed_group_ids == expected, (
        "Fan-out should hit groups linked to this checkpoint only; "
        f"got {landed_group_ids}, expected {expected}"
    )

    # The unlinked group must not get a rule even though it exists in the
    # competition.
    assert (
        ScoreRule.query.filter_by(
            competition_id=s["comp"].id,
            checkpoint_id=s["cp_shared"].id,
            group_id=s["g_gamma"].id,
        ).count()
        == 0
    )


def test_all_groups_resubmit_updates_existing(client, app):
    s = _seed(client)

    first = client.post(
        "/scores/rules",
        data={
            "checkpoint_id": str(s["cp_shared"].id),
            "group_id": "__all__",
            "rules_json": json.dumps(
                {"field_rules": {"task1": {"type": "multiplier", "factor": 1}}}
            ),
        },
    )
    assert first.status_code in (200, 302)
    initial_ids = sorted(
        r.id
        for r in ScoreRule.query.filter_by(
            competition_id=s["comp"].id, checkpoint_id=s["cp_shared"].id
        ).all()
    )
    assert len(initial_ids) == 2

    # Submit again with a different factor — the existing two rows should
    # update in place; no new ScoreRule rows should appear.
    second = client.post(
        "/scores/rules",
        data={
            "checkpoint_id": str(s["cp_shared"].id),
            "group_id": "__all__",
            "rules_json": json.dumps(
                {"field_rules": {"task1": {"type": "multiplier", "factor": 7}}}
            ),
        },
    )
    assert second.status_code in (200, 302)
    rows = ScoreRule.query.filter_by(
        competition_id=s["comp"].id, checkpoint_id=s["cp_shared"].id
    ).all()
    assert sorted(r.id for r in rows) == initial_ids
    for r in rows:
        assert r.rules["field_rules"]["task1"]["factor"] == 7


def test_all_groups_with_no_links_flashes_warning(client, app):
    """A checkpoint on no group route must not silently
    create zero rules — the admin needs to know nothing happened."""
    user = create_user(username="empty-admin", role="admin")
    comp = create_competition(name="Empty Race")
    add_membership(user, comp, role="admin")
    orphan_cp = create_checkpoint(comp, name="Orphan-CP")
    create_group(comp, name="Unattached")  # exists, but not linked
    login_as(client, user, comp)

    resp = client.post(
        "/scores/rules",
        data={
            "checkpoint_id": str(orphan_cp.id),
            "group_id": "__all__",
            "rules_json": _rules_json(),
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    assert ScoreRule.query.filter_by(competition_id=comp.id).count() == 0
    assert b"No groups are linked" in resp.data


def test_fields_endpoint_returns_union_for_all_groups(client, app):
    """The fields helper must hand back the union of every linked group's
    field list so the rule builder can compose one rule that covers all
    of them."""
    s = _seed(client)
    db.session.add(
        SheetConfig(
            competition_id=s["comp"].id,
            spreadsheet_id="local:test",
            spreadsheet_name="Local",
            tab_name=s["cp_shared"].name,
            tab_type="checkpoint",
            checkpoint_id=s["cp_shared"].id,
            config={
                "groups": [
                    {"group_id": s["g_alpha"].id, "name": "Alpha", "fields": ["task1", "task2"]},
                    {"group_id": s["g_beta"].id, "name": "Beta", "fields": ["task2", "topo"]},
                ],
                "points_header": "Points",
                "dead_time_header": "Dead",
                "time_header": "Time",
            },
        )
    )
    db.session.commit()

    resp = client.get(
        f"/api/score-rules/fields?checkpoint_id={s['cp_shared'].id}&group_id=__all__"
    )
    assert resp.status_code == 200
    body = resp.get_json()
    assert body.get("all_groups") is True
    # First-seen order preserved across the union.
    assert body["fields"] == ["task1", "task2", "topo"]
