"""Integration test for /api/scores/resolve enrichment.

The judge UI relies on each field in the response carrying display_label,
hint, widget, and (for mapping rules) widget_choices. This test pins the
JSON contract so we catch regressions before judges hit them on race day.
"""

from __future__ import annotations

from app.extensions import db
from app.models import ScoreRule, SheetConfig
from tests.support import (
    add_membership,
    assign_team_group,
    create_checkpoint,
    create_competition,
    create_group,
    create_team,
    create_user,
    login_as,
)


def _seed_vesla_setup(app):
    """A minimal comp with one CP using the vesla rule shape:
    three pass/fail mapping fields (0/1 -> 0/10) plus a raw izgled field.
    Mirrors the real Ščukanujanje A/D config so the test exercises both
    button and number widgets in one resolve call."""
    admin = create_user(username="judge-ui-admin", role="admin")
    comp = create_competition(name="Judge UI Race")
    add_membership(admin, comp, role="admin")
    group = create_group(comp, name="mGG", prefix="1xx")
    cp = create_checkpoint(comp, name="A", description="Izdelava vesla (test).")
    team = create_team(comp, name="VeslaTeam", number=101)
    assign_team_group(team, group)

    # SheetConfig defines the four fields the judge will see.
    cfg = SheetConfig(
        competition_id=comp.id,
        spreadsheet_id="local:judge-ui",
        spreadsheet_name="Local",
        tab_name=cp.name,
        tab_type="checkpoint",
        checkpoint_id=cp.id,
        config={
            "arrived_header": "Arr",
            "points_header": "Points",
            "dead_time_enabled": False,
            "time_enabled": False,
            "groups": [
                {
                    "group_id": group.id,
                    "name": group.name,
                    "fields": ["dolzina_plavuti", "sirina_plavuti", "sirina_rocaja", "izgled"],
                },
            ],
        },
    )
    db.session.add(cfg)
    db.session.add(
        ScoreRule(
            competition_id=comp.id,
            checkpoint_id=cp.id,
            group_id=group.id,
            rules={
                "field_rules": {
                    # Per-competition polish lives in the rule dict, not
                    # in hardcoded Python dicts.
                    "dolzina_plavuti": {
                        "type": "mapping", "map": {"0": 0, "1": 10},
                        "label": "Dolžina plavuti",
                    },
                    "sirina_plavuti": {
                        "type": "mapping", "map": {"0": 0, "1": 10},
                        "label": "Širina plavuti",
                    },
                    "sirina_rocaja": {
                        "type": "mapping", "map": {"0": 0, "1": 10},
                        "label": "Širina ročaja",
                    },
                    "izgled": {"label": "Izgled", "max": 20},
                },
                "total_fields": ["dolzina_plavuti", "sirina_plavuti", "sirina_rocaja", "izgled"],
            },
        )
    )
    db.session.commit()
    return admin, comp, team, cp


def test_resolve_includes_display_label_hint_widget_for_each_field(client, app):
    with app.app_context():
        admin, comp, team, cp = _seed_vesla_setup(app)
        login_as(client, admin, comp)

        resp = client.post(
            "/api/scores/resolve",
            json={"team_id": team.id, "checkpoint_id": cp.id},
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()

        # Checkpoint description surfaces so the judge sees what to score.
        assert body["checkpoint"]["description"] == "Izdelava vesla (test)."

        fields = {f["key"]: f for f in body["fields"]}
        # All four expected fields are present.
        for k in ("dolzina_plavuti", "sirina_plavuti", "sirina_rocaja", "izgled"):
            assert k in fields, f"Missing field {k!r} in resolve response"

        # Mapping fields render as buttons with the Slovene Ne/Da pass-fail
        # labels and the correct point values attached.
        for k in ("dolzina_plavuti", "sirina_plavuti", "sirina_rocaja"):
            f = fields[k]
            assert f["widget"] == "buttons", f"{k}: expected buttons widget, got {f.get('widget')}"
            assert len(f["widget_choices"]) == 2
            zero = next(c for c in f["widget_choices"] if c["value"] == "0")
            one = next(c for c in f["widget_choices"] if c["value"] == "1")
            assert zero["points"] == 0
            assert one["points"] == 10
            # Slovene labels (not slugs) so judges read them on the field UI.
            assert "Ne" in zero["label"]
            assert "Da" in one["label"]
            # display_label is the human form of the slug.
            assert f["display_label"] and f["display_label"] != k

        # Raw entry keeps the number widget; the per-rule 'max' supplies
        # the 0-20 hint (no hardcoded slug dict needed).
        izgled = fields["izgled"]
        assert izgled["widget"] == "number"
        assert izgled["hint"] == "0-20 tock"
        assert izgled["display_label"] == "Izgled"


def test_resolve_renders_deviation_rule_with_target_hint(client, app):
    """H 'Predmet prostornine 0.6L' on the real ruleset: judge needs the
    target+penalty visible, no buttons because deviation isn't a discrete
    choice."""
    with app.app_context():
        admin = create_user(username="judge-ui-h-admin", role="admin")
        comp = create_competition(name="Judge UI H Race")
        add_membership(admin, comp, role="admin")
        group = create_group(comp, name="PP", prefix="3xx")
        cp = create_checkpoint(comp, name="H", description="Predmet prostornine 0.6L (ocena), 0-50 pts")
        team = create_team(comp, name="HTeam", number=301)
        assign_team_group(team, group)
        cfg = SheetConfig(
            competition_id=comp.id,
            spreadsheet_id="local:judge-ui-h",
            spreadsheet_name="Local",
            tab_name=cp.name,
            tab_type="checkpoint",
            checkpoint_id=cp.id,
            config={
                "points_header": "Points",
                "dead_time_enabled": False,
                "time_enabled": False,
                "groups": [{"group_id": group.id, "name": group.name, "fields": ["prostornina_l"]}],
            },
        )
        db.session.add(cfg)
        db.session.add(
            ScoreRule(
                competition_id=comp.id,
                checkpoint_id=cp.id,
                group_id=group.id,
                rules={
                    "field_rules": {
                        "prostornina_l": {
                            "type": "deviation",
                            "target": 0.6,
                            "max_points": 50,
                            "penalty_points": 2.5,
                            "penalty_distance": 0.05,
                            "min_points": 0,
                            "label": "Prostornina (L)",
                        },
                    },
                    "total_fields": ["prostornina_l"],
                },
            )
        )
        db.session.commit()
        login_as(client, admin, comp)

        resp = client.post(
            "/api/scores/resolve",
            json={"team_id": team.id, "checkpoint_id": cp.id},
        )
        assert resp.status_code == 200, resp.get_json()
        body = resp.get_json()

        field = next(f for f in body["fields"] if f["key"] == "prostornina_l")
        assert field["widget"] == "number"
        # Hint mentions target, penalty distance/points, and max.
        assert "0.6" in field["hint"]
        assert "0.05" in field["hint"]
        assert "2.5" in field["hint"]
        assert "max 50" in field["hint"]
        assert field["display_label"] == "Prostornina (L)"
