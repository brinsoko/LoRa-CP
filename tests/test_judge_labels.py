"""Unit tests for the judge-UI label/hint/widget helpers.

The judge form must surface friendly Slovene labels, a short instructional
hint for each rule shape, and render mapping rules as a button group so
judges don't have to memorize which numeric value means 'pass'. This pins
the contract of judge_labels.enrich_field_def so changes don't silently
regress the resolve API response.
"""

from __future__ import annotations

from app.utils.judge_labels import (
    derive_hint,
    derive_widget,
    display_label,
    enrich_field_def,
    soft_cap_hint,
)


def test_display_label_uses_curated_slovene_strings():
    assert display_label("dolzina_plavuti") == "Dolzina plavuti"
    assert display_label("sirina_rocaja") == "Sirina rocaja"
    assert display_label("logicna_uganka") == "Logicna uganka"
    assert display_label("prostornina_l") == "Prostornina (L)"
    assert display_label("dead_time") == "Mrtvi cas (min)"


def test_display_label_falls_back_to_prettified_slug():
    # Unknown slug: snake_case -> space, capitalize first.
    assert display_label("foo_bar_baz") == "Foo bar baz"
    assert display_label("singleword") == "Singleword"
    assert display_label("") == ""


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


def test_derive_hint_for_multiplier_states_per_unit_value():
    hint = derive_hint({"type": "multiplier", "factor": 5})
    assert "5" in hint and "tock" in hint

    hint20 = derive_hint({"type": "multiplier", "factor": 20})
    assert "20" in hint20


def test_derive_hint_empty_for_raw_or_missing_rule():
    assert derive_hint({}) == ""
    assert derive_hint(None) == ""


def test_derive_widget_renders_binary_mapping_as_yes_no_buttons():
    rule = {"type": "mapping", "map": {"0": 0, "1": 10}}
    w = derive_widget(rule)
    assert w["widget"] == "buttons"
    choices = w["widget_choices"]
    assert len(choices) == 2
    # Sorted ascending so 0 comes before 1.
    assert choices[0]["value"] == "0"
    assert choices[0]["points"] == 0
    assert "Ne" in choices[0]["label"]
    assert choices[1]["value"] == "1"
    assert choices[1]["points"] == 10
    assert "Da" in choices[1]["label"]


def test_derive_widget_renders_multi_choice_mapping_as_button_group():
    rule = {"type": "mapping", "map": {"0": 0, "1": 20, "2": 40}}
    w = derive_widget(rule)
    assert w["widget"] == "buttons"
    assert len(w["widget_choices"]) == 3
    # Non-binary labels show 'value -> points' style.
    assert "40" in w["widget_choices"][2]["label"]


def test_derive_widget_falls_back_to_number_for_large_mapping_or_other_types():
    big_map = {str(i): i for i in range(10)}
    assert derive_widget({"type": "mapping", "map": big_map})["widget"] == "number"
    assert derive_widget({"type": "deviation", "target": 1.0, "max_points": 100})["widget"] == "number"
    assert derive_widget({"type": "multiplier", "factor": 5})["widget"] == "number"
    assert derive_widget({})["widget"] == "number"
    assert derive_widget(None)["widget"] == "number"


def test_enrich_field_def_attaches_all_three_keys():
    fd = enrich_field_def(
        {"key": "dolzina_plavuti", "label": "dolzina_plavuti", "type": "number"},
        {"type": "mapping", "map": {"0": 0, "1": 10}},
    )
    assert fd["display_label"] == "Dolzina plavuti"
    assert fd["hint"]  # non-empty
    assert fd["widget"] == "buttons"
    assert len(fd["widget_choices"]) == 2
    # The legacy 'label' and 'type' keys are preserved so old clients keep
    # working.
    assert fd["label"] == "dolzina_plavuti"
    assert fd["type"] == "number"


def test_enrich_field_def_for_unknown_raw_field_has_empty_hint():
    # A field with no rule and no entry in FIELD_RAW_MAX_SL still gets a
    # display label, but the hint stays empty (we don't invent a cap).
    fd = enrich_field_def(
        {"key": "uncharted_field", "label": "uncharted_field", "type": "number"},
        None,
    )
    assert fd["display_label"] == "Uncharted field"
    assert fd["hint"] == ""
    assert fd["widget"] == "number"


def test_enrich_field_def_for_known_raw_field_surfaces_soft_cap_hint():
    fd = enrich_field_def(
        {"key": "izgled", "label": "izgled", "type": "number"},
        None,
    )
    assert fd["display_label"] == "Izgled"
    # izgled is 0-20 per the race rulebook (FIELD_RAW_MAX_SL).
    assert fd["hint"] == "0-20 tock"
    assert fd["widget"] == "number"


def test_enrich_field_def_for_tematski_test_caps_at_50():
    fd = enrich_field_def(
        {"key": "tematski_test", "label": "tematski_test", "type": "number"},
        None,
    )
    assert fd["display_label"] == "Tematski test"
    assert fd["hint"] == "0-50 tock"


def test_soft_cap_hint_returns_empty_for_unknown_slug():
    assert soft_cap_hint("not_in_dict") == ""
    assert soft_cap_hint("") == ""


def test_rule_derived_hint_wins_over_soft_cap():
    # A multiplier field that happens to share a key with a soft-cap entry
    # should show the rule hint, not the soft cap. Use lokostrelstvo which
    # is in FIELD_RAW_MAX_SL=100 and pretend it has a multiplier rule.
    fd = enrich_field_def(
        {"key": "lokostrelstvo", "label": "lokostrelstvo", "type": "number"},
        {"type": "multiplier", "factor": 5},
    )
    # Rule hint mentions the factor, not the 0-100 soft cap.
    assert "5" in fd["hint"]
    assert "0-100" not in fd["hint"]
