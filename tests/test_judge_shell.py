"""Phase-3 judge shell: checkpoint-scoped views, ETA, bulk entry.

Covers the waiting-list drop rules from the decisions log (arrived /
skipped ahead / DNF / finished), the ETA source ladder (observed mean
once >= 3 samples, else PathStop.expected_leg_minutes, else none), the
scan-time check-in on /api/scores/resolve, and the bulk-entry grid.
"""

from __future__ import annotations

from datetime import timedelta

from app.extensions import db
from app.models import Checkin, JudgeCheckpoint, ScoreEntry
from app.utils.judge_view import build_judge_checkpoint_view
from app.utils.time import utcnow_naive
from tests.support import (
    add_membership,
    assign_team_group,
    create_checkpoint,
    create_competition,
    create_group,
    create_score_field,
    create_team,
    create_user,
    login_as,
    set_group_route,
)


def _seed(client, *, role="judge"):
    user = create_user(username=f"shell-{role}", role="public")
    comp = create_competition(name=f"Shell Cup {role}")
    add_membership(user, comp, role=role)
    group = create_group(comp, name="Alpha")
    cps = [create_checkpoint(comp, name=f"CP{i}") for i in range(1, 4)]
    path = set_group_route(group, cps)
    db.session.add(
        JudgeCheckpoint(user_id=user.id, checkpoint_id=cps[1].id, competition_id=comp.id, is_default=True)
    )
    teams = [create_team(comp, name=f"Team{i}", number=100 + i) for i in range(1, 6)]
    for team in teams:
        assign_team_group(team, group)
    db.session.commit()
    login_as(client, user, comp)
    return user, comp, group, path, cps, teams


def _checkin(comp, team, cp, minutes_ago):
    db.session.add(
        Checkin(
            competition_id=comp.id,
            team_id=team.id,
            checkpoint_id=cp.id,
            timestamp=utcnow_naive() - timedelta(minutes=minutes_ago),
        )
    )
    db.session.commit()


class TestJudgeView:
    def test_waiting_buckets_and_summary(self, client, app):
        _user, comp, _group, _path, cps, teams = _seed(client)
        _checkin(comp, teams[0], cps[1], 5)     # arrived here
        _checkin(comp, teams[1], cps[0], 20)    # on course toward us
        _checkin(comp, teams[2], cps[2], 1)     # skipped us to the finish
        teams[3].dnf = True
        db.session.commit()
        # teams[4] has no checkins: not started

        view = build_judge_checkpoint_view(comp.id, cps[1].id)
        assert view["expected_total"] == 5
        assert view["arrived_count"] == 1
        assert [row["team"].id for row in view["missed"]] == [teams[2].id]
        assert view["finished_count"] == 1  # the skip went all the way to the finish
        assert view["dnf_count"] == 1
        waiting_ids = {row["team"].id for row in view["waiting"]}
        assert waiting_ids == {teams[1].id, teams[4].id}
        states = {row["team"].id: row["eta_state"] for row in view["waiting"]}
        assert states[teams[4].id] == "not_started"

    def test_eta_uses_expected_leg_minutes_fallback(self, client, app):
        _user, comp, _group, path, cps, teams = _seed(client)
        path.stops[1].expected_leg_minutes = 30.0
        db.session.commit()
        _checkin(comp, teams[0], cps[0], 20)  # 20 elapsed of expected 30

        view = build_judge_checkpoint_view(comp.id, cps[1].id)
        row = next(r for r in view["waiting"] if r["team"].id == teams[0].id)
        assert row["eta_state"] == "eta"
        assert 8 <= row["eta_minutes"] <= 11

    def test_eta_prefers_observed_mean_with_enough_samples(self, client, app):
        _user, comp, _group, path, cps, teams = _seed(client)
        path.stops[1].expected_leg_minutes = 60.0  # fallback says 60; reality says ~10
        db.session.commit()
        for team in teams[:3]:  # three completed legs of ~10 minutes
            _checkin(comp, team, cps[0], 30)
            _checkin(comp, team, cps[1], 20)
        _checkin(comp, teams[3], cps[0], 5)  # 5 elapsed, mean 10 -> ~5 left

        view = build_judge_checkpoint_view(comp.id, cps[1].id)
        row = next(r for r in view["waiting"] if r["team"].id == teams[3].id)
        assert row["eta_state"] == "eta"
        assert 3 <= row["eta_minutes"] <= 7

    def test_eta_without_estimate_is_on_course(self, client, app):
        _user, comp, _group, _path, cps, teams = _seed(client)
        _checkin(comp, teams[0], cps[0], 12)  # no fallback, <3 samples

        view = build_judge_checkpoint_view(comp.id, cps[1].id)
        row = next(r for r in view["waiting"] if r["team"].id == teams[0].id)
        assert row["eta_state"] == "on_course"
        assert row["last_seen_name"] == cps[0].name

    def test_reverse_direction_uses_directed_previous_stop(self, client, app):
        _user, comp, group, path, cps, teams = _seed(client)
        group.direction = "reverse"  # route is CP3 -> CP2 -> CP1
        path.stops[2].expected_leg_minutes = 30.0  # undirected leg CP2-CP3
        db.session.commit()
        _checkin(comp, teams[0], cps[2], 20)  # at directed previous stop CP3

        view = build_judge_checkpoint_view(comp.id, cps[1].id)
        row = next(r for r in view["waiting"] if r["team"].id == teams[0].id)
        assert row["eta_state"] == "eta"
        assert 8 <= row["eta_minutes"] <= 11


class TestJudgePages:
    def test_tabs_render_for_judge(self, client, app):
        _seed(client)
        for url in ("/judge/", "/judge/teams", "/judge/results"):
            assert client.get(url).status_code == 200

    def test_viewer_forbidden(self, client, app):
        _seed(client, role="viewer")
        assert client.get("/judge/").status_code == 403

    def test_checkpoint_switcher_scopes_session(self, client, app):
        user, comp, _group, _path, cps, _teams = _seed(client)
        db.session.add(
            JudgeCheckpoint(user_id=user.id, checkpoint_id=cps[0].id, competition_id=comp.id, is_default=False)
        )
        db.session.commit()
        resp = client.post("/judge/checkpoint", data={"checkpoint_id": cps[0].id, "next": "/judge/"})
        assert resp.status_code == 302
        body = client.get("/judge/").get_data(as_text=True)
        assert f'value="{cps[0].id}" selected' in body

    def test_judge_lands_on_shell_after_selecting_competition(self, client, app):
        user, comp, _group, _path, _cps, _teams = _seed(client)
        resp = client.post(f"/competitions/select/{comp.id}")
        assert resp.status_code == 302
        assert resp.headers["Location"].endswith("/judge/")


class TestResolveCreatesCheckin:
    def test_scan_records_arrival_once(self, client, app):
        _user, comp, _group, _path, cps, teams = _seed(client)
        first = client.post(
            "/api/scores/resolve",
            json={"team_id": teams[0].id, "checkpoint_id": cps[1].id, "create_checkin": True},
        )
        assert first.status_code == 200
        assert first.get_json()["checkin_created"] is True
        second = client.post(
            "/api/scores/resolve",
            json={"team_id": teams[0].id, "checkpoint_id": cps[1].id, "create_checkin": True},
        )
        assert second.get_json()["checkin_created"] is False
        assert second.get_json()["checkin_exists"] is True
        count = Checkin.query.filter_by(team_id=teams[0].id, checkpoint_id=cps[1].id).count()
        assert count == 1

    def test_resolve_without_flag_does_not_create(self, client, app):
        _user, comp, _group, _path, cps, teams = _seed(client)
        resp = client.post(
            "/api/scores/resolve",
            json={"team_id": teams[0].id, "checkpoint_id": cps[1].id},
        )
        assert resp.status_code == 200
        assert resp.get_json()["checkin_created"] is False
        assert Checkin.query.filter_by(team_id=teams[0].id, checkpoint_id=cps[1].id).count() == 0


class TestBulkEntry:
    def test_grid_requires_flag_for_judges(self, client, app):
        _user, _comp, _group, _path, cps, _teams = _seed(client)
        resp = client.get("/judge/table")
        assert resp.status_code == 302  # bulk not enabled -> back home

    def test_batch_submit_scores_and_checkins(self, client, app):
        _user, comp, group, _path, cps, teams = _seed(client)
        cps[1].bulk_entry_enabled = True
        create_score_field(
            cps[1], "test_score", rule_type="multiplier", rule_params={"factor": 2}
        )
        db.session.commit()

        resp = client.post(
            "/judge/table",
            data={
                f"team_{teams[0].id}_test_score": "10",
                f"team_{teams[1].id}_test_score": "4",
                f"team_{teams[2].id}_test_score": "",
            },
            follow_redirects=True,
        )
        assert resp.status_code == 200

        entry_a = ScoreEntry.query.filter_by(team_id=teams[0].id, checkpoint_id=cps[1].id).first()
        entry_b = ScoreEntry.query.filter_by(team_id=teams[1].id, checkpoint_id=cps[1].id).first()
        assert entry_a.total == 20.0
        assert entry_b.total == 8.0
        assert ScoreEntry.query.filter_by(team_id=teams[2].id, checkpoint_id=cps[1].id).count() == 0
        # Paper stations get an arrival recorded alongside the score.
        assert Checkin.query.filter_by(team_id=teams[0].id, checkpoint_id=cps[1].id).count() == 1

    def test_batch_submit_skips_unchanged_rows(self, client, app):
        _user, comp, _group, _path, cps, teams = _seed(client)
        cps[1].bulk_entry_enabled = True
        create_score_field(cps[1], "pts")
        db.session.commit()
        data = {f"team_{teams[0].id}_pts": "7"}
        client.post("/judge/table", data=data, follow_redirects=True)
        client.post("/judge/table", data=data, follow_redirects=True)
        assert ScoreEntry.query.filter_by(team_id=teams[0].id, checkpoint_id=cps[1].id).count() == 1

    def test_batch_submit_rejects_negative(self, client, app):
        _user, comp, _group, _path, cps, teams = _seed(client)
        cps[1].bulk_entry_enabled = True
        create_score_field(cps[1], "pts")
        db.session.commit()
        client.post("/judge/table", data={f"team_{teams[0].id}_pts": "-3"}, follow_redirects=True)
        assert ScoreEntry.query.filter_by(team_id=teams[0].id, checkpoint_id=cps[1].id).count() == 0

    def test_clearing_a_prefilled_value_removes_it(self, client, app):
        """The grid pre-fills stored values, so an emptied cell is an
        explicit clear: a new latest entry without the field must land
        (it used to be silently ignored, keeping mistyped points)."""
        _user, comp, _group, _path, cps, teams = _seed(client)
        cps[1].bulk_entry_enabled = True
        create_score_field(cps[1], "pts")
        db.session.commit()
        client.post("/judge/table", data={f"team_{teams[0].id}_pts": "8"}, follow_redirects=True)
        client.post("/judge/table", data={f"team_{teams[0].id}_pts": ""}, follow_redirects=True)
        entries = (
            ScoreEntry.query.filter_by(team_id=teams[0].id, checkpoint_id=cps[1].id)
            .order_by(ScoreEntry.created_at.desc(), ScoreEntry.id.desc())
            .all()
        )
        assert len(entries) == 2
        assert "pts" not in (entries[0].raw_fields or {})


class TestButterflyRoutes:
    """Routes that visit the same checkpoint twice (A-B-C-B-D). Check-ins
    record only the FIRST visit per checkpoint, so classification must not
    treat an id-level 'seen' as proof of having passed a later position."""

    def _seed_butterfly(self):
        user = create_user(username="butterfly-judge", role="public")
        comp = create_competition(name="Butterfly Cup")
        add_membership(user, comp, role="judge")
        group = create_group(comp, name="Alpha")
        a, b, c, d = [create_checkpoint(comp, name=f"B-CP{i}") for i in range(4)]
        set_group_route(group, [a, b, c, b, d])
        teams = [create_team(comp, name=f"BT{i}", number=200 + i) for i in range(1, 4)]
        for team in teams:
            assign_team_group(team, group)
        db.session.commit()
        return comp, group, (a, b, c, d), teams

    def test_loop_team_is_waiting_not_missed_at_interior_cp(self, app):
        """A team between B (first visit) and C satisfies 'B in times',
        and B also appears AFTER C on the route; that must not count as
        evidence the team passed C."""
        comp, _group, (a, b, c, _d), teams = self._seed_butterfly()
        _checkin(comp, teams[0], a, 30)
        _checkin(comp, teams[0], b, 15)

        view = build_judge_checkpoint_view(comp.id, c.id)
        waiting_names = {row["team"].name for row in view["waiting"]}
        missed_names = {row["team"].name for row in view["missed"]}
        assert teams[0].name in waiting_names
        assert teams[0].name not in missed_names

    def test_team_that_skipped_to_finish_is_missed(self, app):
        """D occurs only after C, so a D check-in still proves the pass."""
        comp, _group, (a, b, c, d), teams = self._seed_butterfly()
        _checkin(comp, teams[1], a, 40)
        _checkin(comp, teams[1], b, 30)
        _checkin(comp, teams[1], d, 5)

        view = build_judge_checkpoint_view(comp.id, c.id)
        missed_names = {row["team"].name for row in view["missed"]}
        assert teams[1].name in missed_names

    def test_finished_count_includes_teams_that_visited_here(self, app):
        """The label reads 'already finished': a team that passed this
        checkpoint AND finished must be counted, not only teams that
        skipped it (the old count lived inside the missed branch)."""
        comp, _group, (a, b, c, d), teams = self._seed_butterfly()
        for cp, minutes in ((a, 60), (b, 50), (c, 40), (d, 10)):
            _checkin(comp, teams[2], cp, minutes)

        view = build_judge_checkpoint_view(comp.id, c.id)
        assert view["finished_count"] == 1


class TestJudgeValidationEdges:
    def test_set_checkpoint_rejects_open_redirect(self, client, app):
        """The 'next' form value goes through safe_redirect_target
        (pass-1 fix): a crafted external URL must fall back to /judge."""
        _user, _comp, _group, _path, cps, _teams = _seed(client)
        for evil in ("https://evil.example/x", "//evil.example/x"):
            resp = client.post(
                "/judge/checkpoint",
                data={"checkpoint_id": cps[1].id, "next": evil},
            )
            assert resp.status_code == 302
            assert "evil.example" not in resp.headers["Location"]

    def test_api_submit_rejects_negative_dead_time(self, client, app):
        """Pass 1 removed dead_time's exemption from the negative-input
        check: the engine ignores negatives while the display would sum
        them, so accepting one creates a display-vs-scoring mismatch."""
        _user, _comp, _group, _path, cps, teams = _seed(client)
        resp = client.post(
            "/api/scores/submit",
            json={
                "team_id": teams[0].id,
                "checkpoint_id": cps[1].id,
                "fields": {"dead_time": -5},
            },
        )
        assert resp.status_code == 400, resp.data
        assert ScoreEntry.query.filter_by(team_id=teams[0].id).count() == 0
