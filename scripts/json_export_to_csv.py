#!/usr/bin/env python3
"""
Convert a competition export JSON (from /api/transfer/export) into one CSV
per logical table - drop the folder into Google Drive, open each CSV as a
sheet/tab, and you have a working manual replacement for sheets sync.

Usage:
  venv/bin/python scripts/json_export_to_csv.py path/to/export.json out/
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys


def _w(path: str, header: list[str], rows: list[list]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(header)
        w.writerows(rows)
    print(f"  wrote {len(rows):5d} rows -> {path}")


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("json_path")
    p.add_argument("out_dir")
    args = p.parse_args()

    with open(args.json_path, encoding="utf-8") as f:
        data = json.load(f)

    os.makedirs(args.out_dir, exist_ok=True)
    print(f"exporting to {args.out_dir}/")

    # 1. competition meta (key=value pairs so it fits a 2-col sheet)
    comp = data.get("competition", {}) or {}
    settings = comp.get("settings") or {}
    rows = [["name", comp.get("name", "")]]
    for k, v in sorted(settings.items()):
        rows.append([f"settings.{k}", json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v])
    rows.append(["exported_at", data.get("exported_at", "")])
    rows.append(["schema_version", data.get("schema_version", "")])
    _w(os.path.join(args.out_dir, "01_competition.csv"), ["key", "value"], rows)

    # 2. groups
    rows = [
        [g.get("name", ""), g.get("prefix", ""), g.get("position", ""), g.get("description", "")]
        for g in data.get("groups", []) or []
    ]
    _w(os.path.join(args.out_dir, "02_groups.csv"), ["name", "prefix", "position", "description"], rows)

    # 3. teams (members joined into one column)
    rows = []
    for t in data.get("teams", []) or []:
        members = t.get("members") or []
        members_sorted = sorted(members, key=lambda m: m.get("position") or 0)
        member_names = " | ".join(m.get("name", "") for m in members_sorted if m.get("name"))
        rows.append(
            [
                t.get("number", ""),
                t.get("name", ""),
                t.get("organization", ""),
                "YES" if t.get("dnf") else "",
                member_names,
            ]
        )
    _w(os.path.join(args.out_dir, "03_teams.csv"), ["number", "name", "organization", "dnf", "members"], rows)

    # 4. team -> group assignments
    rows = [
        [tg.get("team_name", ""), tg.get("group_name", ""), "YES" if tg.get("active") else ""]
        for tg in data.get("team_groups", []) or []
    ]
    _w(os.path.join(args.out_dir, "04_team_groups.csv"), ["team", "group", "active"], rows)

    # 5. checkpoints
    rows = [
        [
            c.get("name", ""),
            c.get("description", ""),
            c.get("location", ""),
            c.get("easting", ""),
            c.get("northing", ""),
            "YES" if c.get("is_virtual") else "",
            c.get("judges_note", ""),
            c.get("scoring_text", ""),
        ]
        for c in data.get("checkpoints", []) or []
    ]
    _w(
        os.path.join(args.out_dir, "05_checkpoints.csv"),
        ["name", "description", "location", "easting", "northing", "is_virtual", "judges_note", "scoring_text"],
        rows,
    )

    # 6. group -> checkpoint links (route definition)
    rows = sorted(
        (
            [link.get("group_name", ""), link.get("position", ""), link.get("checkpoint_name", "")]
            for link in data.get("group_checkpoint_links", []) or []
        ),
        key=lambda r: (r[0], r[1] if r[1] != "" else 0),
    )
    _w(os.path.join(args.out_dir, "06_group_routes.csv"), ["group", "position", "checkpoint"], rows)

    # 7. RFID cards
    rows = [[c.get("team_name", ""), c.get("uid", ""), c.get("number", "")] for c in data.get("rfid_cards", []) or []]
    _w(os.path.join(args.out_dir, "07_rfid_cards.csv"), ["team", "uid", "number"], rows)

    # 8. check-ins, sorted by time
    rows = sorted(
        (
            [
                ci.get("timestamp", ""),
                ci.get("team_name", ""),
                ci.get("checkpoint_name", ""),
                ci.get("created_by_username", "") or "",
                ci.get("created_by_dev_num", "") or "",
            ]
            for ci in data.get("checkins", []) or []
        ),
        key=lambda r: r[0],
    )
    _w(
        os.path.join(args.out_dir, "08_checkins.csv"),
        ["timestamp", "team", "checkpoint", "created_by_user", "created_by_dev_num"],
        rows,
    )

    # 9. scores - raw_fields varies per CP, so expand all unique keys as columns
    scores = data.get("scores", []) or []
    field_keys: list[str] = []
    seen: set[str] = set()
    for s in scores:
        for k in (s.get("raw_fields") or {}).keys():
            if k not in seen:
                seen.add(k)
                field_keys.append(k)
    header = ["created_at", "team", "checkpoint", "total", "judge"] + [f"field.{k}" for k in field_keys]
    rows = sorted(
        (
            [
                s.get("created_at", ""),
                s.get("team_name", ""),
                s.get("checkpoint_name", ""),
                s.get("total", "") if s.get("total") is not None else "",
                s.get("judge_username", "") or "",
                *[(s.get("raw_fields") or {}).get(k, "") for k in field_keys],
            ]
            for s in scores
        ),
        key=lambda r: r[0],
    )
    _w(os.path.join(args.out_dir, "09_scores.csv"), header, rows)

    # 10. score rules - JSON blob per (checkpoint, group), one row each
    rows = [
        [sr.get("checkpoint_name", ""), sr.get("group_name", ""), json.dumps(sr.get("rules") or {}, ensure_ascii=False)]
        for sr in data.get("score_rules", []) or []
    ]
    _w(os.path.join(args.out_dir, "10_score_rules.csv"), ["checkpoint", "group", "rules_json"], rows)

    # 11. global score rules (per group)
    rows = [
        [gr.get("group_name", ""), json.dumps(gr.get("rules") or {}, ensure_ascii=False)]
        for gr in data.get("global_score_rules", []) or []
    ]
    _w(os.path.join(args.out_dir, "11_global_score_rules.csv"), ["group", "rules_json"], rows)

    # 12. sheet configs (templates the Google Sheets sync uses)
    rows = [
        [
            sc.get("tab_name", ""),
            sc.get("tab_type", ""),
            sc.get("checkpoint_name", "") or "",
            json.dumps(sc.get("config") or {}, ensure_ascii=False),
        ]
        for sc in data.get("sheet_configs", []) or []
    ]
    _w(os.path.join(args.out_dir, "12_sheet_configs.csv"), ["tab_name", "tab_type", "checkpoint", "config_json"], rows)

    # 13. devices (LoRa)
    rows = [
        [d.get("dev_num", ""), d.get("name", ""), "YES" if d.get("active") else "", d.get("note", "")]
        for d in data.get("devices", []) or []
    ]
    _w(os.path.join(args.out_dir, "13_devices.csv"), ["dev_num", "name", "active", "note"], rows)

    # 14. paths (schema >= 1.1.0) - the authoritative course definition;
    # one row per stop in traversal order, with the leg-minutes estimate.
    rows = []
    for p in data.get("paths", []) or []:
        for stop in sorted(p.get("stops") or [], key=lambda s: s.get("position", 0)):
            rows.append(
                [
                    p.get("name"),
                    stop.get("position"),
                    stop.get("checkpoint_name"),
                    stop.get("expected_leg_minutes"),
                    p.get("notes") or "",
                ]
            )
    _w(os.path.join(args.out_dir, "14_paths.csv"),
       ["path", "position", "checkpoint", "expected_leg_minutes", "notes"], rows)

    # 15-17. phase-2 scoring sections (schema >= 1.2.0); empty for old files
    rows = [
        [f.get("checkpoint_name"), f.get("key"), f.get("label"), f.get("rule_type"),
         json.dumps(f.get("rule_params") or {}, ensure_ascii=False), f.get("counts_in_total")]
        for f in data.get("score_fields", []) or []
    ]
    _w(os.path.join(args.out_dir, "15_score_fields.csv"),
       ["checkpoint", "key", "label", "rule_type", "rule_params_json", "counts_in_total"], rows)

    rows = [
        [s.get("path_name"), s.get("start_checkpoint_name"), s.get("end_checkpoint_name"),
         s.get("max_points"), s.get("min_points")]
        for s in data.get("timed_segments", []) or []
    ]
    _w(os.path.join(args.out_dir, "16_timed_segments.csv"),
       ["path", "from", "to", "max_points", "min_points"], rows)

    rows = [
        [g.get("group_name"), g.get("found_points_per"), g.get("race_max_points"),
         g.get("race_threshold_minutes"), g.get("race_penalty_minutes"),
         g.get("race_penalty_points"), g.get("race_min_points"), g.get("race_dq_multiplier")]
        for g in data.get("group_scoring", []) or []
    ]
    _w(os.path.join(args.out_dir, "17_group_scoring.csv"),
       ["group", "found_points_per", "race_max_points", "race_threshold_minutes",
        "race_penalty_minutes", "race_penalty_points", "race_min_points", "race_dq_multiplier"], rows)

    print()
    print(f"done - {len(os.listdir(args.out_dir))} files in {args.out_dir}/")
    print("Next: upload the whole folder to Google Drive, then in Sheets:")
    print("  - either open each CSV individually (one Sheet per file), or")
    print("  - create one Sheet and use File > Import > Upload (one tab at a time)")


if __name__ == "__main__":
    sys.exit(main())
