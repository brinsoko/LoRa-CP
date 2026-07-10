"""Paths API + group wiring: the phase-1 first-class route model.

Covers /api/paths CRUD, duplicate (plain and reversed), delete
protection, direction-aware group serialization, and checkpoint-side
path membership.
"""

from __future__ import annotations

from app.extensions import db
from app.models import CheckpointGroup, Path
from app.utils.paths import resolve_route_ids, route_finish, route_start
from tests.support import (
    add_membership,
    create_checkpoint,
    create_competition,
    create_group,
    create_user,
    login_as,
    set_group_route,
)


def _seed(client):
    user = create_user(username="paths-admin", role="admin")
    comp = create_competition(name="Paths Race")
    add_membership(user, comp, role="admin")
    cps = [create_checkpoint(comp, name=f"CP-{i}") for i in range(1, 5)]
    login_as(client, user, comp)
    return comp, cps


def test_create_path_with_ordered_stops(client, app):
    comp, cps = _seed(client)
    resp = client.post(
        "/api/paths",
        json={"name": "Main", "checkpoint_ids": [cps[0].id, cps[2].id, cps[1].id]},
    )
    assert resp.status_code == 201
    payload = resp.get_json()["path"]
    assert [s["checkpoint_id"] for s in payload["stops"]] == [cps[0].id, cps[2].id, cps[1].id]
    assert [s["position"] for s in payload["stops"]] == [0, 1, 2]


def test_create_path_allows_repeated_checkpoint(client, app):
    """Out-and-back: the same checkpoint may appear twice."""
    comp, cps = _seed(client)
    resp = client.post(
        "/api/paths",
        json={"name": "OutAndBack", "checkpoint_ids": [cps[0].id, cps[1].id, cps[0].id]},
    )
    assert resp.status_code == 201
    stops = resp.get_json()["path"]["stops"]
    assert [s["checkpoint_id"] for s in stops] == [cps[0].id, cps[1].id, cps[0].id]


def test_patch_reorders_stops(client, app):
    comp, cps = _seed(client)
    created = client.post(
        "/api/paths", json={"name": "Reorder", "checkpoint_ids": [cps[0].id, cps[1].id]}
    ).get_json()["path"]
    resp = client.patch(
        f"/api/paths/{created['id']}",
        json={"checkpoint_ids": [cps[1].id, cps[0].id, cps[2].id]},
    )
    assert resp.status_code == 200
    stops = resp.get_json()["path"]["stops"]
    assert [s["checkpoint_id"] for s in stops] == [cps[1].id, cps[0].id, cps[2].id]


def test_duplicate_and_duplicate_reversed(client, app):
    comp, cps = _seed(client)
    created = client.post(
        "/api/paths", json={"name": "Course", "checkpoint_ids": [c.id for c in cps[:3]]}
    ).get_json()["path"]

    plain = client.post(f"/api/paths/{created['id']}/duplicate", json={})
    assert plain.status_code == 201
    plain_stops = plain.get_json()["path"]["stops"]
    assert [s["checkpoint_id"] for s in plain_stops] == [c.id for c in cps[:3]]

    rev = client.post(f"/api/paths/{created['id']}/duplicate", json={"reversed": True})
    assert rev.status_code == 201
    rev_payload = rev.get_json()["path"]
    assert [s["checkpoint_id"] for s in rev_payload["stops"]] == [c.id for c in reversed(cps[:3])]
    assert rev_payload["name"] != created["name"]


def test_duplicate_name_conflict(client, app):
    comp, cps = _seed(client)
    client.post("/api/paths", json={"name": "Taken", "checkpoint_ids": []})
    resp = client.post("/api/paths", json={"name": "Taken", "checkpoint_ids": []})
    assert resp.status_code == 409


def test_delete_refused_while_group_references(client, app):
    comp, cps = _seed(client)
    group = create_group(comp, name="Cat A")
    path = set_group_route(group, cps[:2])

    resp = client.delete(f"/api/paths/{path.id}")
    assert resp.status_code == 409

    group.path_id = None
    db.session.commit()
    resp = client.delete(f"/api/paths/{path.id}")
    assert resp.status_code == 200
    assert db.session.get(Path, path.id) is None


def test_group_api_wires_path_and_direction(client, app):
    comp, cps = _seed(client)
    path_id = client.post(
        "/api/paths", json={"name": "Shared", "checkpoint_ids": [c.id for c in cps[:3]]}
    ).get_json()["path"]["id"]

    fwd = client.post(
        "/api/groups", json={"name": "Fwd", "path_id": path_id, "direction": "forward"}
    )
    rev = client.post(
        "/api/groups", json={"name": "Rev", "path_id": path_id, "direction": "reverse"}
    )
    assert fwd.status_code == 201 and rev.status_code == 201

    fwd_cps = [c["id"] for c in fwd.get_json()["group"]["checkpoints"]]
    rev_cps = [c["id"] for c in rev.get_json()["group"]["checkpoints"]]
    assert fwd_cps == [c.id for c in cps[:3]]
    assert rev_cps == list(reversed(fwd_cps))

    rev_group = CheckpointGroup.query.filter_by(competition_id=comp.id, name="Rev").first()
    assert route_start(rev_group) == cps[2].id
    assert route_finish(rev_group) == cps[0].id


def test_group_create_enforces_dead_time_segment_invariant(client, app):
    """Creating a group must run the same dead-time-vs-segment-end check
    as updating one: a reverse group flips the segment end onto a
    dead-time checkpoint, which update rejected but create let through."""
    from tests.support import create_segment

    comp, cps = _seed(client)
    path_id = client.post(
        "/api/paths", json={"name": "SegPath", "checkpoint_ids": [cps[0].id, cps[1].id]}
    ).get_json()["path"]["id"]
    create_segment(db.session.get(Path, path_id), cps[0], cps[1])
    cps[0].dead_time_enabled = True
    db.session.commit()

    # Reverse direction makes cps[0] (dead-time enabled) the directed end.
    rev = client.post(
        "/api/groups", json={"name": "RevSeg", "path_id": path_id, "direction": "reverse"}
    )
    assert rev.status_code == 400, rev.data
    assert CheckpointGroup.query.filter_by(competition_id=comp.id, name="RevSeg").count() == 0


def test_group_api_rejects_bad_direction_and_foreign_path(client, app):
    comp, cps = _seed(client)
    other_comp = create_competition(name="Other Comp")
    foreign = Path(competition_id=other_comp.id, name="Foreign")
    db.session.add(foreign)
    db.session.commit()

    bad_dir = client.post("/api/groups", json={"name": "X", "direction": "sideways"})
    assert bad_dir.status_code == 400
    bad_path = client.post("/api/groups", json={"name": "Y", "path_id": foreign.id})
    assert bad_path.status_code == 400


def test_checkpoint_path_membership_append_and_remove(client, app):
    comp, cps = _seed(client)
    group = create_group(comp, name="Cat B")
    path = set_group_route(group, cps[:2])
    new_cp = create_checkpoint(comp, name="CP-New")

    # Tick the path on the new checkpoint: appended as last stop.
    resp = client.patch(f"/api/checkpoints/{new_cp.id}", json={"path_ids": [path.id]})
    assert resp.status_code == 200
    db.session.refresh(group)
    assert resolve_route_ids(group) == [cps[0].id, cps[1].id, new_cp.id]

    # Untick: removed, positions re-densified.
    resp = client.patch(f"/api/checkpoints/{new_cp.id}", json={"path_ids": []})
    assert resp.status_code == 200
    db.session.refresh(group)
    assert resolve_route_ids(group) == [cps[0].id, cps[1].id]
    assert [s.position for s in group.path.stops] == [0, 1]


def test_duplicate_reversed_shifts_leg_minutes(client, app):
    """expected_leg_minutes is stored on the LATER stop of each pair;
    reversing the stop order must shift the list one slot (pass-1 fix),
    not copy it per-stop, or every leg estimate lands on the wrong leg."""
    comp, cps = _seed(client)
    resp = client.post(
        "/api/paths",
        json={
            "name": "Timed",
            "checkpoint_ids": [cps[0].id, cps[1].id, cps[2].id],
            "expected_leg_minutes": [None, 10, 20],
        },
    )
    assert resp.status_code == 201, resp.data
    path_id = resp.get_json()["path"]["id"]

    resp = client.post(f"/api/paths/{path_id}/duplicate", json={"reversed": True})
    assert resp.status_code == 201, resp.data
    stops = resp.get_json()["path"]["stops"]
    assert [s["checkpoint_id"] for s in stops] == [cps[2].id, cps[1].id, cps[0].id]
    # Leg C-B was stored on C's successor (20), leg B-A on B's (10):
    # reversed, the first traversed leg is C->B (20), then B->A (10).
    assert [s["expected_leg_minutes"] for s in stops] == [None, 20, 10]


def test_stop_rewrite_drops_stranded_segments(client, app):
    """Removing a segment endpoint from the path must delete the segment
    (pass-1 fix on the paths API side): it can never time again and its
    directed end would keep blocking dead-time toggles forever."""
    from tests.support import create_segment

    comp, cps = _seed(client)
    resp = client.post(
        "/api/paths", json={"name": "SegDrop", "checkpoint_ids": [cps[0].id, cps[1].id]}
    )
    path_id = resp.get_json()["path"]["id"]
    create_segment(db.session.get(Path, path_id), cps[0], cps[1])

    resp = client.patch(
        f"/api/paths/{path_id}", json={"checkpoint_ids": [cps[0].id, cps[2].id]}
    )
    assert resp.status_code == 200, resp.data
    from app.models import TimedSegment

    assert TimedSegment.query.filter_by(path_id=path_id).count() == 0


def test_checkpoint_side_removal_drops_stranded_segments(client, app):
    """Same invariant via the checkpoint edit (pass-2 fix in
    _apply_paths): unticking a path there rewrites the stops too."""
    from tests.support import create_segment

    comp, cps = _seed(client)
    resp = client.post(
        "/api/paths", json={"name": "SegDropCp", "checkpoint_ids": [cps[0].id, cps[1].id]}
    )
    path_id = resp.get_json()["path"]["id"]
    create_segment(db.session.get(Path, path_id), cps[0], cps[1])

    resp = client.patch(f"/api/checkpoints/{cps[1].id}", json={"path_ids": []})
    assert resp.status_code == 200, resp.data
    from app.models import TimedSegment

    assert TimedSegment.query.filter_by(path_id=path_id).count() == 0
