"""Judge-UI presentation helpers.

The judge's scoring form should not show raw slugs ("dolzina_plavuti") or
bare number inputs. This module turns a (field_key, rule) pair into:

  * display_label  - human-readable label
  * hint           - short instructional string ("Cilj 0.6 L, +/- 0.05
                     = -2.5 pts, max 50 pts")
  * widget         - "buttons" for small mapping rules, else "number"
  * widget_choices - for "buttons", a list of {value, label, points}

**Polish metadata lives on each per-competition field_rule.** A rule
entry in ScoreRule.rules.field_rules can carry optional polish keys
alongside the scoring shape:

    "dolzina_plavuti": {
        "type": "mapping", "map": {"0": 0, "1": 10},
        "label": "Dolžina plavuti",     # display label
        "hint": "30-60 cm",             # optional explicit hint override
    },
    "izgled": {
        "label": "Izgled",
        "max": 20,                       # soft cap for raw entry (0-20)
    },
    "vris_correct": {
        "type": "multiplier", "factor": 20,
        "label": "Pravilne vrisane KT",
        "max_input": 10,                 # max input value for multipliers
    }

Resolution order:
  1. The rule dict's own label / hint / max / max_input keys.
  2. The tiny FRAMEWORK_LABELS dict below (only for app-managed slugs
     like dead_time / time / points - these are not per-competition).
  3. Slug-derived fallback (replace underscores, capitalize first letter).

So the same code runs any competition. Slovene labels for the
Ščukanujanje rulebook live in that competition's exported JSON, not in
this module.
"""

from __future__ import annotations

from typing import Any

# App-managed slugs that the framework writes (not authored per
# competition). These get a stable label here; competitions can still
# override via the rule dict if they want different wording.
FRAMEWORK_LABELS: dict[str, str] = {
    "dead_time": "Mrtvi cas (min)",
    "time": "Cas",
    "points": "Tocke",
    "score": "Tocke",
}


def display_label(field_key: str, rule: dict | None = None) -> str:
    """Resolve a Slovene/localized display label for a slug.

    1. rule["label"] if the rule supplies one.
    2. FRAMEWORK_LABELS for the few app-managed slugs.
    3. Slug-derived: 'foo_bar' -> 'Foo bar'.
    """
    if rule and isinstance(rule, dict):
        label = rule.get("label")
        if isinstance(label, str) and label.strip():
            return label.strip()
    if not field_key:
        return ""
    if field_key in FRAMEWORK_LABELS:
        return FRAMEWORK_LABELS[field_key]
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


def _soft_cap_hint_from_rule(rule: dict | None) -> str:
    """Return '0-N tock' when the rule supplies a `max` and is otherwise
    raw (no `type`). Empty string otherwise."""
    if not rule or not isinstance(rule, dict):
        return ""
    if rule.get("type"):
        # Structured rules carry their own derivable hint; don't double up.
        return ""
    # ScoreField.max_input surfaces as "max_input" in the rule dict
    # (field_rule_dict); legacy blobs used "max". Accept both so raw
    # fields keep their soft-cap hint after the phase-2 migration.
    cap = rule.get("max", rule.get("max_input"))
    if cap is None:
        return ""
    return f"0-{_fmt_num(cap)} tock"


def derive_hint(rule: dict | None) -> str:
    """Generate a short Slovene hint describing the rule.

    Priority:
      1. rule["hint"] (explicit per-competition override).
      2. Rule-shape-derived hint (mapping/deviation/multiplier).
      3. Soft-cap hint from rule["max"] for raw fields.

    Returns "" when no useful hint can be derived.
    """
    if not rule or not isinstance(rule, dict):
        return ""
    # 1. Explicit override.
    explicit = rule.get("hint")
    if isinstance(explicit, str) and explicit.strip():
        return explicit.strip()
    # 2. Rule-shape-derived hint.
    rtype = rule.get("type")
    if rtype == "mapping":
        mp = rule.get("map") or {}
        if not mp:
            return ""
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
        base = f"Vsaka enota = {_fmt_num(factor)} tock"
        max_input = rule.get("max_input")
        if max_input is not None:
            total = _fmt_num(max_input * factor) if factor else _fmt_num(max_input)
            return f"{base} (max {max_input}, = {total} tock)"
        return base
    # 3. Soft cap on raw field (rule has no type but supplies max).
    return _soft_cap_hint_from_rule(rule)


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

    items = list(mp.items())
    try:
        items.sort(key=lambda kv: float(kv[0]))
    except (TypeError, ValueError):
        pass

    is_binary_pass_fail = len(items) == 2 and {str(k) for k, _ in items} == {"0", "1"}

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
        choices.append(
            {
                "value": str(key),
                "label": label,
                "points": pts_num,
            }
        )
    return {"widget": "buttons", "widget_choices": choices}


def auto_scoring_text(rule: dict | None, field_keys: list[str] | None = None) -> str:
    """Build a short scoring summary for a CP from its score rule.

    Used as the default body when Checkpoint.scoring_text is NULL. Each
    line is "<label>: <hint>" or just the label if no hint resolves.
    """
    if not rule or not isinstance(rule, dict):
        return ""
    field_rules = rule.get("field_rules") or {}
    ordering = field_keys or rule.get("total_fields") or list(field_rules.keys())
    lines = []
    for key in ordering:
        sub = field_rules.get(key, {})
        label = display_label(key, sub)
        hint = derive_hint(sub)
        if hint:
            lines.append(f"{label}: {hint}")
        else:
            lines.append(label)
    if "time_race" in rule:
        tr = rule["time_race"]
        mp = tr.get("max_points") or 0
        s_cp = tr.get("start_checkpoint_name") or ""
        e_cp = tr.get("end_checkpoint_name") or ""
        if s_cp and e_cp:
            lines.append(f"Hitrostna {s_cp} -> {e_cp}: max {_fmt_num(mp)} tock")
    return "\n".join(lines)


def enrich_field_def(field_def: dict, rule: dict | None) -> dict:
    """Attach display_label / hint / widget keys to a field_def.

    `field_def` must already have at least a "key". Polish metadata is
    pulled from the rule dict (label / hint / max / max_input keys);
    framework-level slugs (dead_time / time) get a stable Slovene label
    from FRAMEWORK_LABELS as a fallback.
    """
    key = field_def.get("key") or ""
    field_def.setdefault("display_label", display_label(key, rule))
    field_def.setdefault("hint", derive_hint(rule))
    field_def.update(derive_widget(rule))
    return field_def


# ---------------------------------------------------------------------------
# Backwards-compat shim. Older tests / external callers reference
# soft_cap_hint(key) on the assumption a hardcoded dict provides caps.
# We now look up the per-rule max, but keep the public name available
# returning "" when no rule is in hand. Direct callers should pass the
# rule dict.
# ---------------------------------------------------------------------------
def soft_cap_hint(field_key: str, rule: dict | None = None) -> str:
    return _soft_cap_hint_from_rule(rule) if rule else ""
