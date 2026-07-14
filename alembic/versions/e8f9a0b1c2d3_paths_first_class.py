"""Paths become first-class: paths/path_stops tables, group path_id+direction.

Replaces the CheckpointGroupLink ordered-membership model and the
CheckpointGroup.reverse flag (redesign plan, phase 1). Backfill:

- Each group's effective directed route (link order, flipped if reverse)
  becomes a Path. Groups whose effective route equals an existing path's
  stop sequence share that path (direction=forward); groups whose route is
  the exact reverse share it with direction=reverse. So "same course, both
  ways" collapses onto one Path row.
- New paths are stored in the direction of the first group that uses them
  and named after that group.

Every step is guarded/idempotent: fresh installs bootstrap the full schema
via db.create_all() (see the initial migration), so this revision must
no-op cleanly when paths already exist and checkpoint_group_links never
did. checkpoint_groups is altered with plain ADD COLUMN, never
batch_alter_table; a batch rebuild of this table CASCADE-wiped
team_groups/global_score_rules in prod once (see d7e8f9a0b1c2's note).

Revision ID: e8f9a0b1c2d3
Revises: d7e8f9a0b1c2
Create Date: 2026-07-09
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision: str = "e8f9a0b1c2d3"
down_revision: Union[str, None] = "d7e8f9a0b1c2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables(insp) -> set[str]:
    return set(insp.get_table_names())


def _columns(insp, table: str) -> set[str]:
    return {c["name"] for c in insp.get_columns(table)}


def upgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    if "paths" not in _tables(insp):
        op.create_table(
            "paths",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "competition_id",
                sa.Integer(),
                sa.ForeignKey("competitions.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("name", sa.String(length=120), nullable=False),
            sa.Column("notes", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.UniqueConstraint("competition_id", "name", name="uq_path_competition_name"),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_paths_competition_id ON paths (competition_id)")

    if "path_stops" not in _tables(insp):
        op.create_table(
            "path_stops",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column(
                "path_id",
                sa.Integer(),
                sa.ForeignKey("paths.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "checkpoint_id",
                sa.Integer(),
                sa.ForeignKey("checkpoints.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.Column("expected_leg_minutes", sa.Float(), nullable=True),
            sa.UniqueConstraint("path_id", "position", name="uq_path_stop_position"),
        )
    op.execute("CREATE INDEX IF NOT EXISTS ix_path_stops_path_id ON path_stops (path_id)")
    op.execute("CREATE INDEX IF NOT EXISTS ix_path_stops_checkpoint_id ON path_stops (checkpoint_id)")

    group_columns = _columns(insp, "checkpoint_groups")
    if "path_id" not in group_columns:
        # Plain ADD COLUMN: no table rebuild, no cascade risk. The FK is
        # declared inline; SQLite accepts REFERENCES on ADD COLUMN.
        op.execute(
            "ALTER TABLE checkpoint_groups ADD COLUMN path_id INTEGER "
            "REFERENCES paths (id) ON DELETE SET NULL"
        )
    if "direction" not in group_columns:
        # Name the CHECK like the model does (ck_group_direction), so a
        # future batch-mode migration can reference/drop it on upgraded
        # DBs the same way as on fresh installs.
        op.execute(
            "ALTER TABLE checkpoint_groups ADD COLUMN direction VARCHAR(10) "
            "NOT NULL DEFAULT 'forward' "
            "CONSTRAINT ck_group_direction CHECK (direction IN ('forward','reverse'))"
        )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_checkpoint_groups_path_id ON checkpoint_groups (path_id)"
    )

    # Backfill only when the legacy link table exists (i.e. a real in-place
    # upgrade; fresh installs never had it).
    if "checkpoint_group_links" in _tables(insp):
        _backfill_paths(bind, had_reverse="reverse" in group_columns)
        op.execute("DROP TABLE checkpoint_group_links")

    # Retire the reverse flag. Plain DROP COLUMN (SQLite >= 3.35); if the
    # local SQLite is too old the column is left behind unused; nothing
    # reads it anymore, and leaving it is safer than a batch rebuild.
    if "reverse" in _columns(inspect(bind), "checkpoint_groups"):
        try:
            op.execute("ALTER TABLE checkpoint_groups DROP COLUMN reverse")
        except Exception:
            pass


def _backfill_paths(bind, had_reverse: bool) -> None:
    now = bind.execute(sa.text("SELECT CURRENT_TIMESTAMP")).scalar()

    reverse_col = "reverse" if had_reverse else "0 AS reverse"
    groups = bind.execute(
        sa.text(
            f"SELECT id, competition_id, name, {reverse_col} FROM checkpoint_groups "  # noqa: S608
            "ORDER BY competition_id, position, id"
        )
    ).fetchall()
    links = bind.execute(
        sa.text(
            "SELECT group_id, checkpoint_id FROM checkpoint_group_links "
            "ORDER BY group_id, position, checkpoint_id"
        )
    ).fetchall()

    order_by_group: dict[int, list[int]] = {}
    for group_id, checkpoint_id in links:
        order_by_group.setdefault(group_id, []).append(checkpoint_id)

    # (competition_id, stop tuple) -> path id, for forward-order matching.
    path_by_sequence: dict[tuple[int, tuple[int, ...]], int] = {}

    for group_id, comp_id, name, reverse in groups:
        already = bind.execute(
            sa.text("SELECT path_id FROM checkpoint_groups WHERE id = :gid"),
            {"gid": group_id},
        ).scalar()
        if already:
            continue
        route = order_by_group.get(group_id) or []
        if not route:
            continue
        effective = tuple(reversed(route)) if reverse else tuple(route)

        forward_key = (comp_id, effective)
        reverse_key = (comp_id, tuple(reversed(effective)))
        if forward_key in path_by_sequence:
            path_id = path_by_sequence[forward_key]
            direction = "forward"
        elif reverse_key in path_by_sequence:
            path_id = path_by_sequence[reverse_key]
            direction = "reverse"
        else:
            result = bind.execute(
                sa.text(
                    "INSERT INTO paths (competition_id, name, notes, created_at) "
                    "VALUES (:comp_id, :name, NULL, :now)"
                ),
                {"comp_id": comp_id, "name": name, "now": now},
            )
            path_id = result.lastrowid
            for position, checkpoint_id in enumerate(effective):
                bind.execute(
                    sa.text(
                        "INSERT INTO path_stops (path_id, checkpoint_id, position) "
                        "VALUES (:path_id, :checkpoint_id, :position)"
                    ),
                    {"path_id": path_id, "checkpoint_id": checkpoint_id, "position": position},
                )
            path_by_sequence[forward_key] = path_id
            direction = "forward"

        bind.execute(
            sa.text("UPDATE checkpoint_groups SET path_id = :path_id, direction = :direction WHERE id = :group_id"),
            {"path_id": path_id, "direction": direction, "group_id": group_id},
        )


def downgrade() -> None:
    bind = op.get_bind()
    insp = inspect(bind)

    if "checkpoint_group_links" not in _tables(insp):
        op.create_table(
            "checkpoint_group_links",
            sa.Column(
                "group_id",
                sa.Integer(),
                sa.ForeignKey("checkpoint_groups.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column(
                "checkpoint_id",
                sa.Integer(),
                sa.ForeignKey("checkpoints.id", ondelete="CASCADE"),
                primary_key=True,
            ),
            sa.Column("position", sa.Integer(), nullable=False),
            sa.UniqueConstraint("checkpoint_id", "group_id", name="uq_cp_group"),
        )

    if "reverse" not in _columns(insp, "checkpoint_groups"):
        op.execute(
            "ALTER TABLE checkpoint_groups ADD COLUMN reverse BOOLEAN NOT NULL DEFAULT 0"
        )

    # Rebuild links from each group's directed route. Direction folds back
    # into link order; a path visiting a checkpoint twice collapses to one
    # link (the old schema cannot represent revisits).
    if "path_stops" in _tables(insp):
        rows = bind.execute(
            sa.text(
                "SELECT g.id, g.direction, s.checkpoint_id "
                "FROM checkpoint_groups g "
                "JOIN path_stops s ON s.path_id = g.path_id "
                "ORDER BY g.id, s.position"
            )
        ).fetchall()
        route_by_group: dict[int, list[int]] = {}
        direction_by_group: dict[int, str] = {}
        for group_id, direction, checkpoint_id in rows:
            route_by_group.setdefault(group_id, []).append(checkpoint_id)
            direction_by_group[group_id] = direction
        for group_id, route in route_by_group.items():
            # path_stops hold the path's forward order. Old code computes
            # the effective route as reversed(links) when reverse=1, so a
            # reverse group must store its links in forward (path) order
            # and only flip the flag. Reversing the route here too would
            # double-encode the direction (old code would then show the
            # route forwards), and a later re-upgrade would lose the
            # reverse direction entirely.
            if direction_by_group.get(group_id) == "reverse":
                bind.execute(
                    sa.text("UPDATE checkpoint_groups SET reverse = 1 WHERE id = :gid"),
                    {"gid": group_id},
                )
            seen: set[int] = set()
            position = 0
            for checkpoint_id in route:
                if checkpoint_id in seen:
                    continue
                seen.add(checkpoint_id)
                bind.execute(
                    sa.text(
                        "INSERT OR IGNORE INTO checkpoint_group_links (group_id, checkpoint_id, position) "
                        "VALUES (:group_id, :checkpoint_id, :position)"
                    ),
                    {"group_id": group_id, "checkpoint_id": checkpoint_id, "position": position},
                )
                position += 1

    # Leave path_id/direction columns behind (plain columns, nothing reads
    # them on the old code); dropping them would require a batch rebuild.
    op.execute("DROP TABLE IF EXISTS path_stops")
    op.execute("DROP TABLE IF EXISTS paths")
