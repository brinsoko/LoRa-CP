"""Judge-UI presentation helpers.

The judge's scoring form should not show raw slugs ("dolzina_plavuti") or
bare number inputs. This module turns a (field_key, rule) pair into:

  * display_label  - a Slovene human-readable label (for example
                     "Dolzina plavuti")
  * hint           - a short instructional string describing the expected
                     input and how points are awarded (for example
                     "Cilj 0.6 L, +/- 0.05 = -2.5 pts, max 50 pts")
  * widget         - "buttons" when the rule is a mapping with a small
                     fixed value set (pass/fail or multi-choice), else
                     "number" for free numeric entry
  * widget_choices - for "buttons", a list of {value, label, points}

Curated entries live in FIELD_LABEL_SL; anything else falls back to
slug.replace("_", " ").capitalize(). Add new field names to FIELD_LABEL_SL
when you onboard new judge fields - keeps labels under source control
without growing the SheetConfig schema.
"""

from __future__ import annotations

from typing import Any

# Curated "soft cap" max for raw-entry fields (rule is empty `{}`, judge
# enters any non-negative number; we just want to remind them of the
# expected range). Keep in sync with the CP descriptions in the rules
# JSON. Values come from the race rulebook, not the rule shape itself.
FIELD_RAW_MAX_SL: dict[str, int] = {
    "izgled": 20,          # Vesla appearance grading
    "tematski_test": 50,   # Tematski test, 0-50 pts
    "logicna_uganka": 50,  # G - Logicna uganka
    "suhadolica": 50,      # I - test Suhadolica
    "lokostrelstvo": 100,  # Lokostrelstvo, 0-100 pts
    "roza 1": 10,          # Dolocevanje rastlin - 10 pts each
    "roza 2": 10,
    "roza 3": 10,
}


def soft_cap_hint(field_key: str) -> str:
    """Hint shown for raw-entry fields whose accepted range is known.

    Returns "" when the field isn't in FIELD_RAW_MAX_SL, so derive_hint
    can keep returning empty for genuinely uncapped fields.
    """
    n = FIELD_RAW_MAX_SL.get(field_key)
    if n is None:
        return ""
    return f"0-{n} tock"


# Curated slug -> Slovene label table. Keep ordered roughly by CP appearance
# (A -> Cilj + virtuals).
FIELD_LABEL_SL: dict[str, str] = {
    # Vesla (A / D depending on group direction)
    "dolzina_plavuti": "Dolzina plavuti",
    "sirina_plavuti": "Sirina plavuti",
    "sirina_rocaja": "Sirina rocaja",
    "izgled": "Izgled",
    # Tematski (A / D)
    "tematski_test": "Tematski test",
    # E
    "gasilci": "Gasilci",
    # F
    "prihod_pod_kotom": "Prihod pod kotom",
    # G / H / I (PP, RR+)
    "logicna_uganka": "Logicna uganka",
    "prostornina_l": "Prostornina (L)",
    "suhadolica": "Test Suhadolica",
    # J
    "signalizacija_correct": "Pravilna signalizacija",
    "fraca": "Fraca (zadetki)",
    # K
    "prva_pomoc": "Prva pomoc",
    # Topo & Vrisovanje (virtual)
    "topo_p1": "Topografija P1",
    "topo_p2": "Topografija P2",
    "topo_p3": "Topografija P3",
    "vris_correct": "Pravilne vrisane KT",
    # Lokostrelstvo (virtual)
    "lokostrelstvo": "Lokostrelstvo (zadetki)",
    # Dolocevanje rastlin (virtual)
    "roza 1": "Roza 1",
    "roza 2": "Roza 2",
    "roza 3": "Roza 3",
    # Time tracking
    "dead_time": "Mrtvi cas (min)",
    "time": "Cas",
}


def display_label(field_key: str) -> str:
    """Return a human-readable Slovene label for a slug.

    Curated entries take precedence; unknown slugs are prettified by
    replacing underscores with spaces and capitalizing the first letter.
    """
    if not field_key:
        return ""
    label = FIELD_LABEL_SL.get(field_key)
    if label:
        return label
    pretty = field_key.replace("_", " ").strip()
    if not pretty:
        return field_key
    return pretty[0].upper() + pretty[1:]


def _fmt_num(value: Any) -> str:
    """Trim trailing zeros so 50.0 prints as 50 and 2.5 stays 2.5."""
    try:
        f = float(value)
    except (TypeError, ValueError):
        return str(value)
    if f == int(f):
        return str(int(f))
    return f"{f:g}"


def derive_hint(rule: dict | None) -> str:
    """Generate a short Slovene hint describing the rule.

    Returns "" when no useful hint can be derived (raw entry with no
    metadata, time_race, or empty rule).
    """
    if not rule or not isinstance(rule, dict):
        return ""
    rtype = rule.get("type")
    if rtype == "mapping":
        mp = rule.get("map") or {}
        if not mp:
            return ""
        # The widget renders buttons, so hint is just the points scale.
        pts = sorted({int(v) for v in mp.values()})
        if pts == [0]:
            return ""
        return f"Max {_fmt_num(max(pts))} tock"
    if rtype == "deviation":
        target = rule.get("target")
        pts_max = rule.get("max_points") or 0
        pts_pen = rule.get("penalty_points") or 0
        dist = rule.get("penalty_distance") or 0
        parts = []
        if target is not None:
            parts.append(f"Cilj {_fmt_num(target)}")
        if pts_pen and dist:
            parts.append(f"+/- {_fmt_num(dist)} = -{_fmt_num(pts_pen)} tock")
        parts.append(f"max {_fmt_num(pts_max)} tock")
        return ", ".join(parts)
    if rtype == "multiplier":
        factor = rule.get("factor") or 0
        return f"Vsaka enota = {_fmt_num(factor)} tock"
    # Empty rule {} -> raw entry, no derivable hint.
    return ""


def derive_widget(rule: dict | None, max_button_choices: int = 6) -> dict:
    """Return UI widget metadata for the judge form.

    Output shape:
      {"widget": "number"}                          # default
      {"widget": "buttons", "widget_choices": [...]} # mapping with <=N values

    Each choice is {"value": "<raw>", "label": "<button text>",
    "points": <int>}. label is a compact pass/fail or "<value> -> <pts>"
    formatting so the judge sees what each button awards.
    """
    if not rule or not isinstance(rule, dict):
        return {"widget": "number"}
    if rule.get("type") != "mapping":
        return {"widget": "number"}
    mp = rule.get("map") or {}
    if not mp or len(mp) > max_button_choices:
        return {"widget": "number"}

    # Determine the order: numeric keys sorted ascending. Fall back to
    # insertion order when keys aren't all numeric.
    items = list(mp.items())
    try:
        items.sort(key=lambda kv: float(kv[0]))
    except (TypeError, ValueError):
        pass

    is_binary_pass_fail = (
        len(items) == 2
        and {str(k) for k, _ in items} == {"0", "1"}
    )

    choices: list[dict] = []
    for key, pts in items:
        pts_num = int(pts) if isinstance(pts, (int, float)) else pts
        if is_binary_pass_fail:
            if str(key) == "0":
                label = "Ne (0)"
            else:
                label = f"Da ({_fmt_num(pts_num)})"
        else:
            label = f"{key} → {_fmt_num(pts_num)}"
        choices.append({
            "value": str(key),
            "label": label,
            "points": pts_num,
        })
    return {"widget": "buttons", "widget_choices": choices}


def enrich_field_def(field_def: dict, rule: dict | None) -> dict:
    """Add display_label/hint/widget keys to a field_def in place and return it.

    `field_def` must already have at least a "key" key (and typically
    "label" + "type" from the resolve endpoint). Hint priority:
      1. Rule-derived hint (mapping/deviation/multiplier).
      2. Soft-cap from FIELD_RAW_MAX_SL for known raw-entry fields.
      3. Empty.
    """
    key = field_def.get("key") or ""
    field_def.setdefault("display_label", display_label(key))
    hint = derive_hint(rule) or soft_cap_hint(key)
    field_def.setdefault("hint", hint)
    field_def.update(derive_widget(rule))
    return field_def
