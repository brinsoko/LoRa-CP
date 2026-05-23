#!/usr/bin/env python3
"""Import team members from a registration CSV and (optionally) delete
teams that didn't register.

The CSV is the raw export from the Google Forms registration sheet, with
the standard Slovenian column headers:
    Ime ekipe                       (team name)
    Ime in priimek vodje ekipe      (leader name)
    Ime in priimek člana 2          (member 2 name)
    Ime in priimek člana 3          (member 3 name)

Matching: team names are matched case-insensitively, with surrounding
whitespace stripped. The CSV winner is the database team — we don't
rename DB teams to match CSV, we just attach members.

Member sanitisation: cells equal to "", "X", "/", "N/A" (case-insensitive)
are treated as placeholders and skipped, so a 2-member team doesn't end
up with a phantom "X" member in row 3.

Idempotency: teams that already have any members are skipped with a
warning. Pass --force to clear existing members first.

Usage (run inside the prod web container):
    docker compose -f docker-compose.prod.yml exec web \\
        python /app/scripts/import_team_members.py \\
            --competition-id 10 \\
            --csv "/app/data/Ščukanujanje 2026 Prijavnica (Odzivi) - 20. 5. 2026.csv" \\
            --delete "BAM,Perke"

Dry-run is the default. Add --apply to commit.
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import create_app  # noqa: E402
from app.extensions import db  # noqa: E402
from app.models import Team, TeamMember  # noqa: E402

_PLACEHOLDER_VALUES = {"", "x", "/", "n/a", "na", "-"}
LEADER_ROLE = "vodja"

COL_TEAM_NAME = "Ime ekipe"
COL_LEADER = "Ime in priimek vodje ekipe"
COL_MEMBER_2 = "Ime in priimek člana 2"
COL_MEMBER_3 = "Ime in priimek člana 3"


def _normalize(s: str) -> str:
    return (s or "").strip().casefold()


def _clean_member_name(raw: str) -> str | None:
    name = (raw or "").strip()
    if name.casefold() in _PLACEHOLDER_VALUES:
        return None
    return name


def _parse_csv(csv_path: Path) -> dict[str, list[tuple[str, str | None]]]:
    """Return {normalized_team_name: [(member_name, role), ...]} in roster
    order — leader first (role='vodja'), then members 2 and 3 with no role.
    Rows without a team name are skipped."""
    out: dict[str, list[tuple[str, str | None]]] = {}
    with csv_path.open() as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get(COL_TEAM_NAME) or "").strip()
            if not name:
                continue
            members: list[tuple[str, str | None]] = []
            leader = _clean_member_name(row.get(COL_LEADER) or "")
            if leader:
                members.append((leader, LEADER_ROLE))
            for col in (COL_MEMBER_2, COL_MEMBER_3):
                m = _clean_member_name(row.get(col) or "")
                if m:
                    members.append((m, None))
            out[_normalize(name)] = members
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Import team members from a registration CSV.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--competition-id", type=int, required=True)
    parser.add_argument("--csv", type=Path, required=True)
    parser.add_argument(
        "--delete",
        default="",
        help="Comma-separated team names to delete (case-insensitive).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Clear existing members before re-importing (default: skip teams with members).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit changes. Without this, runs in dry-run mode.",
    )
    args = parser.parse_args(argv)

    if not args.csv.exists():
        print(f"ERROR: CSV not found: {args.csv}", file=sys.stderr)
        return 2

    csv_roster = _parse_csv(args.csv)
    print(f"Parsed {len(csv_roster)} team(s) from {args.csv}")

    delete_names = {
        _normalize(n)
        for n in (args.delete.split(",") if args.delete else [])
        if n.strip()
    }

    app = create_app()
    with app.app_context():
        teams = Team.query.filter_by(competition_id=args.competition_id).all()
        if not teams:
            print(
                f"ERROR: no teams found in competition_id={args.competition_id}",
                file=sys.stderr,
            )
            return 1

        db_names = {_normalize(t.name) for t in teams}

        adds: list[tuple[Team, list[tuple[str, str | None]]]] = []
        skipped_existing: list[Team] = []
        unmatched_db: list[Team] = []
        to_delete: list[Team] = []

        for t in teams:
            key = _normalize(t.name)
            if key in delete_names:
                to_delete.append(t)
                continue
            if key not in csv_roster:
                unmatched_db.append(t)
                continue
            roster = csv_roster[key]
            if t.members and not args.force:
                skipped_existing.append(t)
                continue
            adds.append((t, roster))

        unmatched_csv = sorted(csv_roster.keys() - db_names - delete_names)
        not_found_delete = sorted(delete_names - db_names)

        print()
        print("PLAN:")
        print(f"  Teams to populate with members: {len(adds)}")
        for t, roster in adds:
            line = ", ".join(f"{n}" + (f" [{r}]" if r else "") for n, r in roster)
            print(f"    + {t.name!r} (id={t.id}): {line}")

        print(f"  Teams skipped (already have members): {len(skipped_existing)}")
        for t in skipped_existing:
            print(
                f"    = {t.name!r} (id={t.id}): {len(t.members)} member(s) — pass --force to overwrite"
            )

        print(f"  DB teams with no CSV registration: {len(unmatched_db)}")
        for t in unmatched_db:
            print(f"    ? {t.name!r} (id={t.id})")

        print(f"  CSV registrations with no DB team: {len(unmatched_csv)}")
        for name in unmatched_csv:
            print(f"    ? {name}")

        print(f"  Teams to delete: {len(to_delete)}")
        for t in to_delete:
            print(
                f"    - {t.name!r} (id={t.id}): "
                f"{len(t.members)} member(s), cascade deletes follow"
            )

        if not_found_delete:
            print(f"  WARN: --delete names not present in DB: {not_found_delete}")

        if not args.apply:
            print()
            print("Dry-run. Re-run with --apply to commit.")
            return 0

        for t, roster in adds:
            if args.force:
                for m in list(t.members):
                    db.session.delete(m)
                db.session.flush()
            for pos, (name, role) in enumerate(roster):
                db.session.add(
                    TeamMember(
                        team_id=t.id,
                        name=name[:160],
                        role=role[:80] if role else None,
                        position=pos,
                    )
                )

        for t in to_delete:
            db.session.delete(t)

        db.session.commit()
        print()
        print(
            f"Applied: populated {len(adds)} team(s), deleted {len(to_delete)} team(s)."
        )
        return 0


if __name__ == "__main__":
    sys.exit(main())
