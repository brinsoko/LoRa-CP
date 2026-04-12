# Export / Import / Merge Competitions

> **Experimental.** This feature is functional but still maturing. Back up
> your database before importing or merging. The JSON schema (currently
> version `1.0.0`) may change in future releases.

---

## Overview

LoRa-CP supports exporting a full competition as a JSON file, importing that
file to create a new competition, and merging imported data into an existing
competition with conflict resolution.

**Use cases:**

- Back up a competition before making destructive changes.
- Transfer a competition setup between servers.
- Merge data from a staging environment into production.
- Clone a competition as a starting point for a new event.

---

## Export

### Via the API

```bash
curl -b cookies.txt \
  "http://localhost:5001/api/competition/1/export" \
  -o competition_backup.json
```

**Requirements:** Admin role for the competition.

**What is exported:**

| Section | Contents |
|---|---|
| `competition` | Name, settings (public_results, hide_gps_map) |
| `teams` | Name, number, organization, DNF flag |
| `groups` | Name, prefix, description, position |
| `checkpoints` | Name, location, description, coordinates |
| `checkins` | Team name, checkpoint name, timestamp |
| `devices` | Device number, name, note, model, active flag |
| `scores` | Team name, checkpoint name, raw fields, total |
| `rfid_cards` | UID, team name, number |
| `team_groups` | Team-to-group assignments with active flag |
| `group_checkpoint_links` | Group-to-checkpoint links with position |

**What is NOT exported:**

- User accounts and competition memberships
- Audit events
- Google Sheets configuration
- Firmware files
- Score rules (only score entries are exported)

### JSON structure

```json
{
  "schema_version": "1.0.0",
  "exported_at": "2025-10-17T14:30:00Z",
  "competition": {
    "name": "Fall Rally 2025",
    "settings": {
      "public_results": false,
      "hide_gps_map": false
    }
  },
  "teams": [
    {"name": "Alpha", "number": 101, "organization": "Troop 1", "dnf": false}
  ],
  "groups": [
    {"name": "Category A", "prefix": "1xx", "description": null, "position": 0}
  ],
  "checkpoints": [
    {"name": "Forest Gate", "location": "North", "description": null, "easting": 14.5, "northing": 46.1}
  ],
  "checkins": [
    {"team_name": "Alpha", "checkpoint_name": "Forest Gate", "timestamp": "2025-10-17T14:30:00"}
  ],
  "devices": [],
  "scores": [],
  "rfid_cards": [],
  "team_groups": [
    {"team_name": "Alpha", "group_name": "Category A", "active": true}
  ],
  "group_checkpoint_links": [
    {"group_name": "Category A", "checkpoint_name": "Forest Gate", "position": 0}
  ]
}
```

---

## Import (Create New Competition)

Importing creates a brand-new competition from the JSON file. The importing
user is automatically added as an admin of the new competition.

### Via the API (JSON body)

```bash
curl -b cookies.txt -X POST \
  http://localhost:5001/api/competition/import \
  -H "Content-Type: application/json" \
  -d @competition_backup.json
```

### Via the API (file upload)

```bash
curl -b cookies.txt -X POST \
  http://localhost:5001/api/competition/import \
  -F "file=@competition_backup.json"
```

### Response

```json
{
  "ok": true,
  "competition_id": 5,
  "competition_name": "Fall Rally 2025",
  "warnings": []
}
```

**Behavior:**

- If a competition with the same name already exists, the imported one gets
  a timestamp suffix (e.g., `Fall Rally 2025 (imported 20251017-143000)`).
- All entities are created fresh with new IDs.
- Relationships are resolved by name (team names, checkpoint names, group
  names), not by ID.
- RFID cards with UIDs that already exist in the database are skipped.
- Schema version mismatches produce a warning but do not block the import.

---

## Merge (Into Existing Competition)

Merging adds data from an exported JSON file into an existing competition.
It is a two-step process: dry run to detect conflicts, then apply with
conflict resolutions.

### Step 1: Dry run (detect conflicts)

Send the JSON **without** a `resolutions` field:

```bash
curl -b cookies.txt -X POST \
  http://localhost:5001/api/competition/1/merge \
  -H "Content-Type: application/json" \
  -d @competition_backup.json
```

Response:

```json
{
  "ok": true,
  "dry_run": true,
  "conflicts": [
    {
      "entity_type": "team",
      "identifier": "Alpha",
      "local": {"name": "Alpha", "number": 101, "organization": "Troop 1"},
      "imported": {"name": "Alpha", "number": 102, "organization": "Troop 1"},
      "differences": {
        "number": {"local": 101, "imported": 102}
      }
    }
  ],
  "warnings": []
}
```

Conflicts are detected for teams, checkpoints, and groups when an entity with
the same name exists but has different field values.

### Step 2: Apply with resolutions

Add a `resolutions` object to the JSON body. Keys are `"entity_type:name"`
and values are one of:

| Resolution | Effect |
|---|---|
| `keep_local` | Keep the existing local data (default if not specified) |
| `use_imported` | Overwrite local data with imported values |
| `skip` | Do not touch this entity at all |

```bash
curl -b cookies.txt -X POST \
  http://localhost:5001/api/competition/1/merge \
  -H "Content-Type: application/json" \
  -d '{
    "schema_version": "1.0.0",
    "competition": {"name": "Fall Rally 2025", "settings": {}},
    "teams": [
      {"name": "Alpha", "number": 102, "organization": "Troop 1", "dnf": false},
      {"name": "Bravo", "number": 201, "organization": "Troop 2", "dnf": false}
    ],
    "groups": [],
    "checkpoints": [],
    "checkins": [],
    "resolutions": {
      "team:Alpha": "use_imported"
    }
  }'
```

Response:

```json
{
  "ok": true,
  "dry_run": false,
  "summary": {
    "added": {"teams": 1, "checkpoints": 0, "groups": 0, "checkins": 0},
    "updated": {"teams": 1, "checkpoints": 0, "groups": 0},
    "skipped": 0
  },
  "warnings": []
}
```

**Merge behavior:**

- Entities that do not exist locally are always added.
- Entities that exist with identical data are left unchanged.
- Check-ins are added only if no check-in exists for that team+checkpoint
  combination.
- The competition name and settings are **not** changed during a merge.
- Devices from the import file are **not** merged (only teams, groups,
  checkpoints, and check-ins).

---

## Tips

- **Always export before merging** so you have a rollback point.
- **Review conflicts carefully** in the dry-run response before applying.
- The merge resolves entities by **name**, not ID. If you renamed a team
  between export and merge, it will be treated as a new entity.
- For large competitions, the export file can be several MB. This is normal.
- The `schema_version` field enables forward compatibility. If the version
  changes, imports will still work but may produce warnings.
