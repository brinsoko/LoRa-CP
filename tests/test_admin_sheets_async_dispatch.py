"""Admin bulk Sheets operations create durable outbox jobs when
SHEETS_SYNC_INLINE is off (i.e. in real prod), so a slow Sheets API call
never ties up the gunicorn worker and a crash can't lose the request.

Five routes are covered:
  - POST /sheets/build-arrivals
  - POST /sheets/build-teams
  - POST /sheets/build-score
  - POST /sheets/sync-team-numbers/<config_id>
  - POST /sheets/publish-local

In test mode (SHEETS_SYNC_INLINE=True by default), the routes call the
sync helpers directly so existing assertions continue to work. With
SHEETS_SYNC_INLINE=False the assertion is that a SheetsSyncJob row with
the right kind/payload landed and the route returned immediately.
"""

from __future__ import annotations

import pytest

from app.extensions import db
from app.models import SheetConfig, SheetsSyncJob
from tests.support import (
    add_membership,
    assign_team_group,
    create_checkpoint,
    create_competition,
    create_group,
    create_team,
    create_user,
    login_as,
    set_group_route,
)


@pytest.fixture
def async_sheets_app(app_factory):
    """App with SHEETS_SYNC_INLINE=False, same shape as prod."""
    application = app_factory(SHEETS_SYNC_ENABLED=True, SHEETS_SYNC_INLINE=False)
    with application.app_context():
        from app.utils.sheets_settings import save_settings

        save_settings({"sync_enabled": True})
        yield application


def _seed_minimal(comp_name: str):
    user = create_user(username=f"admin-{comp_name}", role="admin")
    comp = create_competition(name=comp_name)
    add_membership(user, comp, role="admin")
    grp = create_group(comp, name="Alpha", prefix="1xx")
    cp = create_checkpoint(comp, name="CP-One")
    team = create_team(comp, name="T1", number=101)
    assign_team_group(team, grp)
    set_group_route(grp, [cp])
    cfg = SheetConfig(
        competition_id=comp.id,
        spreadsheet_id="REAL-SHEET",
        spreadsheet_name="Sheet",
        tab_name=cp.name,
        tab_type="checkpoint",
        checkpoint_id=cp.id,
        config={
            "points_header": "Points",
            "dead_time_enabled": False,
            "time_enabled": True,
            "groups": [{"group_id": grp.id, "name": "Alpha", "fields": []}],
        },
    )
    db.session.add(cfg)
    db.session.commit()
    return {"user": user, "comp": comp, "cfg": cfg}


def _jobs(kind: str) -> list[SheetsSyncJob]:
    return SheetsSyncJob.query.filter_by(kind=kind).all()


def test_publish_local_route_enqueues_job(async_sheets_app):
    s = _seed_minimal("Publish Race")
    client = async_sheets_app.test_client()
    login_as(client, s["user"], s["comp"])
    resp = client.post("/sheets/publish-local", data={"spreadsheet_id": "REAL-SHEET-ID"})
    assert resp.status_code == 302, resp.data
    jobs = _jobs("publish")
    assert len(jobs) == 1
    assert jobs[0].payload["spreadsheet_id"] == "REAL-SHEET-ID"
    assert jobs[0].payload["competition_id"] == s["comp"].id
    assert jobs[0].status == "pending"


def test_sync_team_numbers_route_enqueues_jobs(async_sheets_app):
    s = _seed_minimal("Numbers Race")
    client = async_sheets_app.test_client()
    login_as(client, s["user"], s["comp"])
    resp = client.post(f"/sheets/sync-team-numbers/{s['cfg'].id}")
    assert resp.status_code == 302
    assert len(_jobs("sync_team_numbers")) == 1
    # Summary tabs are dirty-flagged for the real spreadsheet too.
    for kind in ("rebuild_teams", "rebuild_arrivals", "rebuild_score"):
        jobs = _jobs(kind)
        assert len(jobs) == 1, kind
        assert jobs[0].payload["spreadsheet_id"] == "REAL-SHEET"


def test_build_arrivals_route_enqueues_job(async_sheets_app):
    s = _seed_minimal("Arrivals Race")
    client = async_sheets_app.test_client()
    login_as(client, s["user"], s["comp"])
    resp = client.post(
        "/sheets/build-arrivals",
        data={"spreadsheet_id": "REAL-SHEET", "tab_name": "Prihodi"},
    )
    assert resp.status_code == 302
    jobs = _jobs("rebuild_arrivals")
    assert len(jobs) == 1
    assert jobs[0].payload["spreadsheet_id"] == "REAL-SHEET"
    assert jobs[0].payload["tab_name"] == "Prihodi"
    assert jobs[0].payload["competition_id"] == s["comp"].id
    assert jobs[0].dedup_key == "rebuild_arrivals:REAL-SHEET:Prihodi"


def test_build_teams_route_enqueues_job(async_sheets_app):
    s = _seed_minimal("Teams Race")
    client = async_sheets_app.test_client()
    login_as(client, s["user"], s["comp"])
    resp = client.post(
        "/sheets/build-teams",
        data={"spreadsheet_id": "REAL-SHEET", "tab_name": "Ekipe"},
    )
    assert resp.status_code == 302
    jobs = _jobs("rebuild_teams")
    assert len(jobs) == 1
    assert jobs[0].payload["tab_name"] == "Ekipe"


def test_build_score_route_enqueues_job(async_sheets_app):
    s = _seed_minimal("Score Race")
    client = async_sheets_app.test_client()
    login_as(client, s["user"], s["comp"])
    resp = client.post(
        "/sheets/build-score",
        data={
            "spreadsheet_id": "REAL-SHEET",
            "tab_name": "Skupni",
            "include_dead_time_sum": "1",
        },
    )
    assert resp.status_code == 302
    jobs = _jobs("rebuild_score")
    assert len(jobs) == 1
    assert jobs[0].payload["include_dead_time_sum"] is True
    assert jobs[0].payload["competition_id"] == s["comp"].id


def test_repeated_button_press_coalesces(async_sheets_app):
    s = _seed_minimal("Coalesce Race")
    client = async_sheets_app.test_client()
    login_as(client, s["user"], s["comp"])
    for _ in range(3):
        client.post(
            "/sheets/build-teams",
            data={"spreadsheet_id": "REAL-SHEET", "tab_name": "Ekipe"},
        )
    assert len(_jobs("rebuild_teams")) == 1


def test_publish_route_returns_immediately_without_touching_sheets(async_sheets_app, monkeypatch):
    """The route must not execute the publish inline: an exploding sync
    helper is only reachable through the worker."""
    from app.utils import sheets_sync

    s = _seed_minimal("NoWait Race")

    def explode(*_a, **_k):
        raise AssertionError("publish ran inline")

    monkeypatch.setattr(sheets_sync, "publish_local_configs_to_spreadsheet", explode)
    client = async_sheets_app.test_client()
    login_as(client, s["user"], s["comp"])
    resp = client.post("/sheets/publish-local", data={"spreadsheet_id": "REAL-SHEET-ID"})
    assert resp.status_code == 302  # would be 500 if explode() ran
    assert len(_jobs("publish")) == 1
