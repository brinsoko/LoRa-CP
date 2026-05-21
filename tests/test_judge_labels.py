"""Unit tests for the judge-UI label/hint/widget helpers.

Polish metadata (label, hint, max, max_input) lives on each per-
competition field_rule entry - this module's job is to resolve it into
the shape the judge form needs, with sensible slug-derived fallbacks
when a competition does not supply polish.
"""

from __future__ import annotations

from app.utils.judge_labels import (
    derive_hint,
    derive_widget,
    display_label,
    enrich_field_def,
)


def test_display_label_uses_rule_label_when_present():
    # Per-competition label on the rule wins over any fallback.
    fd = enrich_field_def(
        {"key": "dolzina_plavuti", "label": "dolzina_plavuti", "type": "number"},
        {"label": "Dolžina plavuti", "type": "mapping", "map": {"0": 0, "1": 10}},
    )
    assert fd["display_label"] == "Dolžina plavuti"


def test_display_label_falls_back_to_framework_labels_for_app_slugs():
    # dead_time / time are app-managed slugs - framework supplies them.
    assert display_label("dead_time") == "Mrtvi cas (min)"
    assert display_label("time") == "Cas"
    assert display_label("points") == "Tocke"


def test_display_label_slug_fallback_for_unknown_field_with_no_rule():
    # No rule, not in FRAMEWORK_LABELS - prettify the slug.
    assert display_label("uncharted_field") == "Uncharted field"
    assert display_label("") == ""


def test_derive_hint_returns_explicit_hint_override_first():
    rule = {
        "type": "mapping",
        "map": {"0": 0, "1": 10},
        "hint": "Specifične dimenzije, glej pravilnik",
    }
    assert derive_hint(rule) == "Specifične dimenzije, glej pravilnik"


def test_derive_hint_for_mapping_returns_max_points():
    rule = {"type": "mapping", "map": {"0": 0, "1": 10}}
    hint = derive_hint(rule)
    assert "10" in hint and "tock" in hint


def test_derive_hint_for_deviation_includes_target_and_max():
    rule = {
        "type": "deviation",
        "target": 0.6,
        "max_points": 50,
        "penalty_points": 2.5,
        "penalty_distance": 0.05,
        "min_points": 0,
    }
    hint = derive_hint(rule)
    assert "Cilj 0.6" in hint
    assert "0.05" in hint
    assert "2.5" in hint
    assert "max 50" in hint


def test_derive_hint_for_multiplier_includes_max_input_when_specified():
    rule = {"type": "multiplier", "factor": 20, "max_input": 10}
    hint = derive_hint(rule)
    assert "Vsaka enota = 20" in hint
    assert "max 10" in hint
    assert "200" in hint


def test_derive_hint_for_multiplier_without_max_input_states_factor_only():
    hint = derive_hint({"type": "multiplier", "factor": 5})
    assert hint == "Vsaka enota = 5 tock"


def test_derive_hint_for_raw_field_with_max_returns_soft_cap():
    rule = {"label": "Izgled", "max": 20}
    assert derive_hint(rule) == "0-20 tock"


def test_derive_hint_empty_for_raw_or_missing_rule_without_max():
    assert derive_hint({}) == ""
    assert derive_hint(None) == ""
    # Rule with label but no max -> still no derivable hint.
    assert derive_hint({"label": "Izgled"}) == ""


def test_derive_widget_renders_binary_mapping_as_yes_no_buttons():
    rule = {"type": "mapping", "map": {"0": 0, "1": 10}}
    w = derive_widget(rule)
    assert w["widget"] == "buttons"
    choices = w["widget_choices"]
    assert len(choices) == 2
    assert choices[0]["value"] == "0"
    assert choices[0]["points"] == 0
    assert "Ne" in choices[0]["label"]
    assert choices[1]["value"] == "1"
    assert choices[1]["points"] == 10
    assert "Da" in choices[1]["label"]


def test_derive_widget_falls_back_to_number_for_non_mapping_or_large_mappings():
    big_map = {str(i): i for i in range(10)}
    assert derive_widget({"type": "mapping", "map": big_map})["widget"] == "number"
    assert derive_widget({"type": "deviation", "target": 1.0, "max_points": 100})["widget"] == "number"
    assert derive_widget({"type": "multiplier", "factor": 5})["widget"] == "number"
    assert derive_widget({})["widget"] == "number"
    assert derive_widget(None)["widget"] == "number"


def test_enrich_field_def_attaches_label_hint_widget_from_rule():
    """Per-rule polish drives the full enrichment payload - no need for
    a hardcoded slug dictionary."""
    fd = enrich_field_def(
        {"key": "dolzina_plavuti", "label": "dolzina_plavuti", "type": "number"},
        {
            "type": "mapping",
            "map": {"0": 0, "1": 10},
            "label": "Dolžina plavuti",
        },
    )
    assert fd["display_label"] == "Dolžina plavuti"
    assert fd["hint"]
    assert fd["widget"] == "buttons"
    assert len(fd["widget_choices"]) == 2


def test_enrich_field_def_for_raw_field_with_max_shows_soft_cap_hint():
    fd = enrich_field_def(
        {"key": "izgled", "label": "izgled", "type": "number"},
        {"label": "Izgled", "max": 20},
    )
    assert fd["display_label"] == "Izgled"
    assert fd["hint"] == "0-20 tock"
    assert fd["widget"] == "number"


def test_enrich_field_def_for_unknown_slug_with_no_rule_falls_back_cleanly():
    """A field rule that doesn't supply polish - no label, no max, no
    hint - still produces a usable enrichment via slug derivation."""
    fd = enrich_field_def(
        {"key": "uncharted_field", "label": "uncharted_field", "type": "number"},
        None,
    )
    assert fd["display_label"] == "Uncharted field"
    assert fd["hint"] == ""
    assert fd["widget"] == "number"


def test_enrich_field_def_for_multiplier_with_max_input_includes_total():
    """vris_correct-style: multiplier rule that supplies max_input gets
    the "max 10 (= 200 tock)" hint, no hardcoded dict needed."""
    fd = enrich_field_def(
        {"key": "vris_correct", "label": "vris_correct", "type": "number"},
        {
            "type": "multiplier",
            "factor": 20,
            "label": "Pravilne vrisane KT",
            "max_input": 10,
        },
    )
    assert fd["display_label"] == "Pravilne vrisane KT"
    assert "Vsaka enota = 20" in fd["hint"]
    assert "max 10" in fd["hint"]
    assert "200" in fd["hint"]


def test_explicit_hint_override_wins_over_rule_shape():
    """When the competition supplies an explicit 'hint' the resolver
    skips the shape-derived hint - lets organizers write specific
    instructions when the auto-derived text isn't clear enough."""
    fd = enrich_field_def(
        {"key": "prostornina_l", "label": "prostornina_l", "type": "number"},
        {
            "type": "deviation",
            "target": 0.6,
            "max_points": 50,
            "penalty_points": 2.5,
            "penalty_distance": 0.05,
            "label": "Prostornina (L)",
            "hint": "Ocenite prostornino predmeta - cilj 0.6 L",
        },
    )
    assert fd["hint"] == "Ocenite prostornino predmeta - cilj 0.6 L"
