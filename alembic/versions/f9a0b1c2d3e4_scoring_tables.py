"""Scoring becomes first-class tables (redesign plan phase 2).

Creates score_fields / score_field_groups / timed_segments / group_scoring
and the counts_for_found / dead_time_enabled checkpoint flags, then
converts the legacy sources and drops them:

- SheetConfig.config.groups[].fields (field existence per group) plus
  ScoreRule.rules.field_rules (transforms) -> ScoreField defaults with
  ScoreFieldGroup disable/override rows where groups differ.
- ScoreRule.rules.time_race -> TimedSegment on the group's path
  (endpoints normalized to the path's forward order, deduplicated).
- GlobalScoreRule.rules.found/time -> GroupScoring columns; the found
  exclude_start/end flags become Checkpoint.counts_for_found = False
  (deliberate simplification per the decisions log).
- SheetConfig.config keeps all its keys: they describe the column layout
  of already-published tabs. New publishes regenerate them from
  ScoreField; config.dead_time_enabled additionally seeds the new
  Checkpoint.dead_time_enabled flag.

Every step is guarded/idempotent for the create_all() bootstrap path,
and checkpoints is altered with plain ADD COLUMN only.

Revision ID: f9a0b1c2d3e4
Revises: e8f9a0b1c2d3
Create Date: 2026-07-09
"""

import json
from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "f9a0b1c2d3e4"
down_revision: Union[str, None] = "e8f9a0b1c2d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_META_KEYS = {"label", "hint", "max", "max_input"}


def _tables(insp) -> set[str]:
    return set(insp.get_table_names())


def _columns(insp, table: str) -> set[str]:
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    if "score_fields" not in _tables(insp):
        op.create_table(
            "score_fields",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "competition_id",
                sa.Integer(),
                sa.ForeignKey("competitions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "checkpoint_id",
                sa.Integer(),
                sa.ForeignKey("checkpoints.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("key", sa.String(length=80), nullable=False),
            sa.Column("label", sa.String(length=160), nullable=True),
            sa.Column("hint", sa.String(length=255), nullable=True),
            sa.Column("position", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("rule_type", sa.String(length=20), nullable=False, server_default="none"),
            sa.Column("rule_params", sa.JSON(), nullable=True),
            sa.Column("max_input", sa.Float(), nullable=True),
            sa.Column("counts_in_total", sa.Boolean(), nullable=False, server_default="1"),
            sa.UniqueConstraint("checkpoint_id", "key", name="uq_score_field_checkpoint_key"),
            sa.CheckConstraint(
                "rule_type IN ('none','mapping','interpolate','multiplier','deviation')",
                name="ck_score_field_rule_type",
            ),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_score_fields_competition_id ON score_fields (competition_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_score_fields_checkpoint_id ON score_fields (checkpoint_id)")

    if "score_field_groups" not in _tables(insp):
        op.create_table(
            "score_field_groups",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "score_field_id",
                sa.Integer(),
                sa.ForeignKey("score_fields.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "group_id",
                sa.Integer(),
                sa.ForeignKey("checkpoint_groups.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("enabled", sa.Boolean(), nullable=False, server_default="1"),
            sa.Column("rule_override", sa.JSON(), nullable=True),
            sa.UniqueConstraint("score_field_id", "group_id", name="uq_score_field_group"),
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_score_field_groups_score_field_id ON score_field_groups (score_field_id)"
    )
    op.execute("CREATE INDEX IF NOT EXISTS ix_score_field_groups_group_id ON score_field_groups (group_id)")

    if "timed_segments" not in _tables(insp):
        op.create_table(
            "timed_segments",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "competition_id",
                sa.Integer(),
                sa.ForeignKey("competitions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "path_id",
                sa.Integer(),
                sa.ForeignKey("paths.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "start_checkpoint_id",
                sa.Integer(),
                sa.ForeignKey("checkpoints.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "end_checkpoint_id",
                sa.Integer(),
                sa.ForeignKey("checkpoints.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(length=120), nullable=True),
            sa.Column("max_points", sa.Float(), nullable=False, server_default="100"),
            sa.Column("min_points", sa.Float(), nullable=False, server_default="0"),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_timed_segments_competition_id ON timed_segments (competition_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_timed_segments_path_id ON timed_segments (path_id)")

    if "group_scoring" not in _tables(insp):
        op.create_table(
            "group_scoring",
            sa.Column(
                "group_id",
                sa.Integer(),
                sa.ForeignKey("checkpoint_groups.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "competition_id",
                sa.Integer(),
                sa.ForeignKey("competitions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("found_points_per", sa.Float(), nullable=True),
            sa.Column("race_max_points", sa.Float(), nullable=True),
            sa.Column("race_threshold_minutes", sa.Float(), nullable=True),
            sa.Column("race_penalty_minutes", sa.Float(), nullable=True),
            sa.Column("race_penalty_points", sa.Float(), nullable=True),
            sa.Column("race_min_points", sa.Float(), nullable=True),
            sa.Column("race_dq_multiplier", sa.Float(), nullable=True),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_group_scoring_competition_id ON group_scoring (competition_id)")

    # Guard on table existence: hand-built legacy DBs (see
    # tests/test_alembic_legacy_upgrade.py) may lack checkpoints entirely;
    # db.create_all() at boot then creates it with the flags included.
    if "checkpoints" in _tables(insp):
        checkpoint_columns = _columns(insp, "checkpoints")
        if "counts_for_found" not in checkpoint_columns:
            op.execute("ALTER TABLE checkpoints ADD COLUMN counts_for_found BOOLEAN NOT NULL DEFAULT 1")
        if "dead_time_enabled" not in checkpoint_columns:
            op.execute("ALTER TABLE checkpoints ADD COLUMN dead_time_enabled BOOLEAN NOT NULL DEFAULT 0")

    tables = _tables(insp)
    if "score_rules" in tables or "global_score_rules" in tables:
        _backfill(bind, tables)
    if "score_rules" in tables:
        op.execute("DROP TABLE score_rules")
    if "global_score_rules" in tables:
        op.execute("DROP TABLE global_score_rules")


def _load_json(raw):
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw) or {}
    except Exception:
        return {}


def _rule_columns(rule) -> tuple[str, dict | None, str | None, str | None, float | None]:
    """Split a legacy field-rule dict into (rule_type, params, label, hint, max_input)."""
    if isinstance(rule, list):
        # Chained rules were possible but never used in production data;
        # keep the first transform, best effort.
        rule = rule[0] if rule else {}
    if not isinstance(rule, dict):
        return "none", None, None, None, None
    rule_type = (rule.get("type") or "none").strip().lower()
    if rule_type not in ("mapping", "interpolate", "multiplier", "deviation"):
        rule_type = "none"
    params = {k: v for k, v in rule.items() if k not in _META_KEYS and k != "type"}
    label = rule.get("label")
    hint = rule.get("hint")
    max_input = rule.get("max_input", rule.get("max"))
    try:
        max_input = float(max_input) if max_input is not None else None
    except (TypeError, ValueError):
        max_input = None
    return rule_type, (params or None), label, hint, max_input


def _backfill(bind, tables) -> None:
    groups = bind.execute(
        sa.text(
            "SELECT id, competition_id, name, path_id, direction, position "
            "FROM checkpoint_groups ORDER BY competition_id, position, id"
        )
    ).fetchall()
    group_by_id = {g[0]: g for g in groups}
    groups_by_comp: dict[int, list] = {}
    for g in groups:
        groups_by_comp.setdefault(g[1], []).append(g)

    sheet_rows = bind.execute(
        sa.text(
            "SELECT id, competition_id, checkpoint_id, config FROM sheet_configs "
            "WHERE tab_type = 'checkpoint' AND checkpoint_id IS NOT NULL ORDER BY created_at ASC, id ASC"
        )
    ).fetchall()
    score_rules = []
    if "score_rules" in tables:
        score_rules = bind.execute(
            sa.text("SELECT competition_id, checkpoint_id, group_id, rules FROM score_rules")
        ).fetchall()
    global_rules = []
    if "global_score_rules" in tables:
        global_rules = bind.execute(
            sa.text("SELECT competition_id, group_id, rules FROM global_score_rules")
        ).fetchall()

    def _norm(name):
        return (name or "").strip().casefold()

    group_id_by_comp_name = {(g[1], _norm(g[2])): g[0] for g in groups}

    # --- fields per checkpoint ---------------------------------------
    # fields_by_group[(cp_id)][group_id] = ordered field keys
    fields_by_cp: dict[int, dict[int, list[str]]] = {}
    dead_time_cps: set[int] = set()
    for _cfg_id, comp_id, cp_id, raw_config in sheet_rows:
        config = _load_json(raw_config)
        if config.get("dead_time_enabled"):
            dead_time_cps.add(cp_id)
        for group_def in config.get("groups") or []:
            gid = group_def.get("group_id")
            if gid is None:
                gid = group_id_by_comp_name.get((comp_id, _norm(group_def.get("name"))))
            if gid is None or gid not in group_by_id:
                continue
            keys = [k for k in (group_def.get("fields") or []) if isinstance(k, str) and k.strip()]
            # Latest config wins per (cp, group): sheet_rows are ordered
            # by created_at asc, so later rows overwrite earlier ones.
            fields_by_cp.setdefault(cp_id, {})[gid] = keys

    rules_by_cp_group: dict[tuple[int, int], dict] = {}
    for comp_id, cp_id, gid, raw_rules in score_rules:
        rules_by_cp_group[(cp_id, gid)] = _load_json(raw_rules)

    # Field keys can also exist only in field_rules (no sheet config).
    for (cp_id, gid), rules in rules_by_cp_group.items():
        keys = list((rules.get("field_rules") or {}).keys())
        if keys:
            existing = fields_by_cp.setdefault(cp_id, {}).setdefault(gid, [])
            for key in keys:
                if key not in existing:
                    existing.append(key)

    cp_comp = {
        cp_id: comp_id
        for cp_id, comp_id in bind.execute(sa.text("SELECT id, competition_id FROM checkpoints"))
    }

    for cp_id, per_group in fields_by_cp.items():
        comp_id = cp_comp.get(cp_id)
        if comp_id is None:
            continue
        comp_groups = [g for g in groups_by_comp.get(comp_id, [])]
        # Field order: first group (by position) that lists fields wins.
        ordered_keys: list[str] = []
        for g in comp_groups:
            for key in per_group.get(g[0], []):
                if key not in ordered_keys:
                    ordered_keys.append(key)
        for key in sorted({k for keys in per_group.values() for k in keys} - set(ordered_keys)):
            ordered_keys.append(key)

        for position, key in enumerate(ordered_keys):
            # Default rule: from the first group (by position) that has one.
            default_rule = None
            for g in comp_groups:
                rule = (rules_by_cp_group.get((cp_id, g[0])) or {}).get("field_rules", {}).get(key)
                if rule is not None:
                    default_rule = rule
                    break
            rule_type, params, label, hint, max_input = _rule_columns(default_rule)

            # counts_in_total from the first group that specifies total_fields.
            counts = True
            for g in comp_groups:
                rules = rules_by_cp_group.get((cp_id, g[0])) or {}
                totals = rules.get("total_fields")
                if totals:
                    counts = key in totals
                    break
            if key == "dead_time":
                counts = False

            result = bind.execute(
                sa.text(
                    "INSERT INTO score_fields (competition_id, checkpoint_id, key, label, hint, position, "
                    "rule_type, rule_params, max_input, counts_in_total) "
                    "VALUES (:comp, :cp, :key, :label, :hint, :pos, :rtype, :params, :maxin, :counts)"
                ),
                {
                    "comp": comp_id,
                    "cp": cp_id,
                    "key": key,
                    "label": label,
                    "hint": hint,
                    "pos": position,
                    "rtype": rule_type,
                    "params": json.dumps(params) if params else None,
                    "maxin": max_input,
                    "counts": counts,
                },
            )
            field_id = result.lastrowid

            groups_with_lists = [g for g in comp_groups if g[0] in per_group]
            for g in groups_with_lists:
                gid = g[0]
                enabled = key in per_group.get(gid, [])
                rule = (rules_by_cp_group.get((cp_id, gid)) or {}).get("field_rules", {}).get(key)
                g_rtype, g_params, _l, _h, g_maxin = _rule_columns(rule)
                override = None
                if rule is not None and (g_rtype, g_params, g_maxin) != (rule_type, params, max_input):
                    override = {"rule_type": g_rtype, "rule_params": g_params, "max_input": g_maxin}
                if not enabled or override is not None:
                    bind.execute(
                        sa.text(
                            "INSERT INTO score_field_groups (score_field_id, group_id, enabled, rule_override) "
                            "VALUES (:fid, :gid, :enabled, :override)"
                        ),
                        {
                            "fid": field_id,
                            "gid": gid,
                            "enabled": enabled,
                            "override": json.dumps(override) if override else None,
                        },
                    )

    # --- segments from time_race --------------------------------------
    seen_segments: set[tuple[int, int, int]] = set()  # (path_id, start, end) forward-normalized
    for (cp_id, gid), rules in rules_by_cp_group.items():
        tr = rules.get("time_race") or {}
        try:
            start_id = int(tr.get("start_checkpoint_id")) if tr.get("start_checkpoint_id") else None
            end_id = int(tr.get("end_checkpoint_id")) if tr.get("end_checkpoint_id") else None
        except (TypeError, ValueError):
            start_id = end_id = None
        if not (start_id and end_id):
            continue
        group = group_by_id.get(gid)
        if not group or not group[3]:
            continue
        path_id, direction = group[3], group[4]
        comp_id = group[1]
        # Store endpoints in the path's forward order so a reversed group
        # resolving the same segment swaps them back automatically.
        if direction == "reverse":
            start_id, end_id = end_id, start_id
        key = (path_id, start_id, end_id)
        if key in seen_segments or (path_id, end_id, start_id) in seen_segments:
            continue
        seen_segments.add(key)

        def _num(value, default):
            try:
                return float(value)
            except (TypeError, ValueError):
                return default

        bind.execute(
            sa.text(
                "INSERT INTO timed_segments (competition_id, path_id, start_checkpoint_id, end_checkpoint_id, "
                "name, max_points, min_points) VALUES (:comp, :path, :start, :end, NULL, :maxp, :minp)"
            ),
            {
                "comp": comp_id,
                "path": path_id,
                "start": start_id,
                "end": end_id,
                "maxp": _num(tr.get("max_points"), 100.0),
                "minp": _num(tr.get("min_points"), 0.0),
            },
        )

    # --- group scoring + counts_for_found -----------------------------
    for comp_id, gid, raw_rules in global_rules:
        rules = _load_json(raw_rules)
        found = rules.get("found") or {}
        time_rule = rules.get("time") or {}

        def _num(value):
            try:
                return float(value) if value is not None else None
            except (TypeError, ValueError):
                return None

        bind.execute(
            sa.text(
                "INSERT OR REPLACE INTO group_scoring (group_id, competition_id, found_points_per, "
                "race_max_points, race_threshold_minutes, race_penalty_minutes, race_penalty_points, "
                "race_min_points, race_dq_multiplier) "
                "VALUES (:gid, :comp, :found, :maxp, :threshold, :pen_min, :pen_pts, :minp, :dq)"
            ),
            {
                "gid": gid,
                "comp": comp_id,
                "found": _num(found.get("points_per")),
                "maxp": _num(time_rule.get("max_points")),
                "threshold": _num(time_rule.get("threshold_minutes")),
                "pen_min": _num(time_rule.get("penalty_minutes")),
                "pen_pts": _num(time_rule.get("penalty_points")),
                "minp": _num(time_rule.get("min_points")),
                "dq": _num(time_rule.get("dq_multiplier")),
            },
        )
        for flag, cp_key in (
            ("exclude_start_checkpoint", "start_checkpoint_id"),
            ("exclude_end_checkpoint", "end_checkpoint_id"),
        ):
            if found.get(flag) and time_rule.get(cp_key):
                try:
                    excluded_cp = int(time_rule.get(cp_key))
                except (TypeError, ValueError):
                    continue
                bind.execute(
                    sa.text("UPDATE checkpoints SET counts_for_found = 0 WHERE id = :cp"),
                    {"cp": excluded_cp},
                )

    bind.execute(sa.text("UPDATE checkpoints SET counts_for_found = 0 WHERE is_virtual = 1"))

    # --- checkpoint dead-time flag -------------------------------------
    # SheetConfig.config keeps its groups[].fields / dead_time_enabled
    # keys: they describe the COLUMN LAYOUT of tabs already published to
    # a spreadsheet, and per-cell writes compute offsets from them. From
    # now on they are a derived layout cache (publishes regenerate them
    # from ScoreField), but existing tabs must keep their geometry.
    for cp_id in dead_time_cps:
        bind.execute(
            sa.text("UPDATE checkpoints SET dead_time_enabled = 1 WHERE id = :cp"), {"cp": cp_id}
        )


def downgrade() -> None:
    # The legacy JSON tables cannot be faithfully reconstructed (the
    # conversion is lossy in the other direction on purpose); recreate
    # them empty so old code can boot, and keep the new tables' data.
    op.execute(
        "CREATE TABLE IF NOT EXISTS score_rules ("
        "id INTEGER PRIMARY KEY, competition_id INTEGER NOT NULL, checkpoint_id INTEGER NOT NULL, "
        "group_id INTEGER NOT NULL, rules JSON NOT NULL, created_at DATETIME NOT NULL, "
        "CONSTRAINT uq_score_rule_scope UNIQUE (competition_id, checkpoint_id, group_id))"
    )
    op.execute(
        "CREATE TABLE IF NOT EXISTS global_score_rules ("
        "id INTEGER PRIMARY KEY, competition_id INTEGER NOT NULL, group_id INTEGER NOT NULL, "
        "rules JSON NOT NULL, created_at DATETIME NOT NULL, "
        "CONSTRAINT uq_global_score_rule_scope UNIQUE (competition_id, group_id))"
    )
    op.execute("DROP TABLE IF EXISTS score_field_groups")
    op.execute("DROP TABLE IF EXISTS score_fields")
    op.execute("DROP TABLE IF EXISTS timed_segments")
    op.execute("DROP TABLE IF EXISTS group_scoring")
