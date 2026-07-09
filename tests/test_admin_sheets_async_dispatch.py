"""Admin bulk Sheets operations dispatch to the background worker when
SHEETS_SYNC_INLINE is off (i.e. in real prod), so a slow Sheets API
call doesn't tie up the gunicorn worker thread.

Five routes are covered:
  - POST /sheets/build-arrivals
  - POST /sheets/build-teams
  - POST /sheets/build-score
  - POST /sheets/sync-team-numbers/<config_id>
  - POST /sheets/publish-local

In test mode (SHEETS_SYNC_INLINE=True by default), the routes call the
sync helpers directly so existing assertions continue to work. With
SHEETS_SYNC_INLINE=False they hand off to enqueue_*; the assertion is
that the corresponding worker entrypoint got hit and the route returned
immediately (~no time).
"""

from __future__ import annotations

import pytest

from app.extensions import db
from app.models import SheetConfig
from app.utils import sheets_sync_worker
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
    """App with SHEETS_SYNC_INLINE=False — same shape as prod."""
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
        spreadsheet_id="local:1",
        spreadsheet_name="Local",
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


def test_publish_local_route_dispatches_to_worker(async_sheets_app, monkeypatch):
    with async_sheets_app.app_context():
        s = _seed_minimal("Async Pub")
        client = async_sheets_app.test_client()
        login_as(client, s["user"], s["comp"])

        captured: list[tuple] = []

        def fake_enqueue(app, comp_id, spreadsheet_id, **kwargs):
            captured.append((comp_id, spreadsheet_id, kwargs))

        monkeypatch.setattr(sheets_sync_worker, "enqueue_publish_local", fake_enqueue)

        resp = client.post(
            "/sheets/publish-local",
            data={"spreadsheet_id": "REAL-SHEET-ID"},
            follow_redirects=False,
        )
        assert resp.status_code == 302, resp.data
        assert captured == [(s["comp"].id, "REAL-SHEET-ID", {})], captured


def test_sync_team_numbers_route_dispatches_to_worker(async_sheets_app, monkeypatch):
    with async_sheets_app.app_context():
        s = _seed_minimal("Async Sync")
        client = async_sheets_app.test_client()
        login_as(client, s["user"], s["comp"])

        captured: list[tuple] = []

        def fake_enqueue(app, competition_id=None):
            captured.append((competition_id,))

        monkeypatch.setattr(
            sheets_sync_worker, "enqueue_sync_all_checkpoint_tabs", fake_enqueue
        )

        resp = client.post(
            f"/sheets/sync-team-numbers/{s['cfg'].id}", follow_redirects=False
        )
        assert resp.status_code == 302
        assert captured == [(s["comp"].id,)], captured


def test_build_arrivals_route_dispatches_to_worker(async_sheets_app, monkeypatch):
    with async_sheets_app.app_context():
        s = _seed_minimal("Async Arr")
        client = async_sheets_app.test_client()
        login_as(client, s["user"], s["comp"])

        captured: list[tuple] = []

        def fake_enqueue(app, spreadsheet_id, tab_name, **kwargs):
            captured.append((spreadsheet_id, tab_name, kwargs))

        monkeypatch.setattr(
            sheets_sync_worker, "enqueue_build_arrivals_tab", fake_enqueue
        )

        resp = client.post(
            "/sheets/build-arrivals",
            data={"spreadsheet_id": "REAL-SHEET", "tab_name": "Prihodi"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert len(captured) == 1
        ssid, tab, kwargs = captured[0]
        assert ssid == "REAL-SHEET"
        assert tab == "Prihodi"
        assert kwargs["competition_id"] == s["comp"].id


def test_build_teams_route_dispatches_to_worker(async_sheets_app, monkeypatch):
    with async_sheets_app.app_context():
        s = _seed_minimal("Async Teams")
        client = async_sheets_app.test_client()
        login_as(client, s["user"], s["comp"])

        captured: list[tuple] = []

        def fake_enqueue(app, spreadsheet_id, tab_name, **kwargs):
            captured.append((spreadsheet_id, tab_name, kwargs))

        monkeypatch.setattr(
            sheets_sync_worker, "enqueue_build_teams_tab", fake_enqueue
        )

        resp = client.post(
            "/sheets/build-teams",
            data={"spreadsheet_id": "REAL-SHEET", "tab_name": "Ekipe"},
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert len(captured) == 1
        assert captured[0][:2] == ("REAL-SHEET", "Ekipe")


def test_build_score_route_dispatches_to_worker(async_sheets_app, monkeypatch):
    with async_sheets_app.app_context():
        s = _seed_minimal("Async Score")
        client = async_sheets_app.test_client()
        login_as(client, s["user"], s["comp"])

        captured: list[tuple] = []

        def fake_enqueue(app, spreadsheet_id, tab_name, **kwargs):
            captured.append((spreadsheet_id, tab_name, kwargs))

        monkeypatch.setattr(
            sheets_sync_worker, "enqueue_build_score_tab", fake_enqueue
        )

        resp = client.post(
            "/sheets/build-score",
            data={
                "spreadsheet_id": "REAL-SHEET",
                "tab_name": "Skupni seštevek",
                "include_dead_time_sum": "1",
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
        assert len(captured) == 1
        ssid, tab, kwargs = captured[0]
        assert ssid == "REAL-SHEET"
        assert kwargs["include_dead_time_sum"] is True
        assert kwargs["competition_id"] == s["comp"].id


def test_publish_route_returns_immediately_does_not_wait_for_sheets(async_sheets_app, monkeypatch):
    """The key invariant: in non-inline mode, the route must NOT call
    the sync helper inline. Confirmed by patching the sync helper to
    raise — the route must still return 302 (because the sync helper
    is never reached)."""
    from app.utils import sheets_sync

    with async_sheets_app.app_context():
        s = _seed_minimal("Async Block")
        client = async_sheets_app.test_client()
        login_as(client, s["user"], s["comp"])

        def explode(*_a, **_k):
            raise AssertionError("Sync helper must not run on the request thread")

        monkeypatch.setattr(
            sheets_sync, "publish_local_configs_to_spreadsheet", explode
        )
        # And make enqueue a noop so we don't actually queue work.
        monkeypatch.setattr(
            sheets_sync_worker, "enqueue_publish_local", lambda *a, **k: None
        )

        resp = client.post(
            "/sheets/publish-local",
            data={"spreadsheet_id": "REAL-SHEET"},
            follow_redirects=False,
        )
        assert resp.status_code == 302  # would be 500 if explode() ran
