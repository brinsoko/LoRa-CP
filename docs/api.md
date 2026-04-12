# API Usage Guide

LoRa-CP exposes a JSON API alongside its HTML views. All API routes live under
`/api/` (except CSV exports and a few legacy paths).

- **OpenAPI spec:** served at `/docs/openapi.json`
- **Swagger UI:** available at `/docs`

## Authentication

The API uses cookie-based sessions. Log in first, then use the session cookie
for subsequent requests.

```bash
# Log in and save the session cookie
curl -c cookies.txt -X POST http://localhost:5001/api/auth/login \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"admin123"}'

# Use the session for authenticated requests
curl -b cookies.txt http://localhost:5001/api/teams
```

Roles are scoped per competition. After login, switch to a competition via the
UI or set the competition in the session. Most examples below assume you have
an active session with the correct competition selected.

### Webhook authentication

The `/api/ingest` endpoint uses a separate header-based secret instead of
session auth:

```bash
curl -X POST http://localhost:5001/api/ingest \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-secret" \
  -d '{"competition_id":1,"dev_id":1,"payload":"A1B2C3D4"}'
```

---

## Teams

### List teams

```bash
curl -b cookies.txt "http://localhost:5001/api/teams"

# With filters
curl -b cookies.txt "http://localhost:5001/api/teams?q=patrol&sort=number_asc&group_id=2"
```

Query parameters: `q` (search name/org/number), `group_id`, `sort`
(`name_asc`, `name_desc`, `number_asc`, `number_desc`).

### Get a single team

```bash
curl -b cookies.txt "http://localhost:5001/api/teams/1"
```

### Create a team

```bash
curl -b cookies.txt -X POST http://localhost:5001/api/teams \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Alpha Patrol",
    "number": 101,
    "organization": "Scout Troop 1",
    "group_id": 1
  }'
```

### Update a team (partial)

```bash
curl -b cookies.txt -X PATCH http://localhost:5001/api/teams/1 \
  -H "Content-Type: application/json" \
  -d '{"number": 102}'
```

### Update a team (full replace)

```bash
curl -b cookies.txt -X PUT http://localhost:5001/api/teams/1 \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Alpha Patrol",
    "number": 102,
    "organization": "Scout Troop 1"
  }'
```

### Delete a team

```bash
# Simple delete (fails if team has check-ins)
curl -b cookies.txt -X DELETE http://localhost:5001/api/teams/1

# Force delete with check-ins
curl -b cookies.txt -X DELETE http://localhost:5001/api/teams/1 \
  -H "Content-Type: application/json" \
  -d '{"force": true, "confirm_text": "Delete"}'
```

### Randomize team numbers

Assigns random numbers within each group's prefix range to unnumbered teams.

```bash
# All groups
curl -b cookies.txt -X POST http://localhost:5001/api/teams/randomize \
  -H "Content-Type: application/json" \
  -d '{}'

# Specific group
curl -b cookies.txt -X POST http://localhost:5001/api/teams/randomize \
  -H "Content-Type: application/json" \
  -d '{"group_id": 1}'
```

---

## Checkpoints

### List checkpoints

```bash
curl -b cookies.txt "http://localhost:5001/api/checkpoints"
```

### Create a checkpoint

```bash
curl -b cookies.txt -X POST http://localhost:5001/api/checkpoints \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Forest Gate",
    "location": "North entrance",
    "easting": 14.5,
    "northing": 46.1,
    "group_ids": [1, 2]
  }'
```

### Update a checkpoint

```bash
curl -b cookies.txt -X PATCH http://localhost:5001/api/checkpoints/1 \
  -H "Content-Type: application/json" \
  -d '{"easting": 14.51, "northing": 46.11}'
```

### Delete a checkpoint

```bash
curl -b cookies.txt -X DELETE http://localhost:5001/api/checkpoints/1
```

Fails with 409 if the checkpoint has existing check-ins.

### Bulk import checkpoints

```bash
curl -b cookies.txt -X POST http://localhost:5001/api/checkpoints/import \
  -H "Content-Type: application/json" \
  -d '{
    "items": [
      {"name": "CP-1", "easting": 14.5, "northing": 46.1, "action": "upsert"},
      {"name": "CP-2", "easting": 14.6, "northing": 46.2, "action": "create"}
    ]
  }'
```

`action` can be `create`, `update`, or `upsert` (default).

---

## Groups (Checkpoint Groups)

### List groups

```bash
curl -b cookies.txt "http://localhost:5001/api/groups"
```

### Create a group

```bash
curl -b cookies.txt -X POST http://localhost:5001/api/groups \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Category A",
    "prefix": "1xx",
    "description": "Senior scouts",
    "checkpoint_ids": [1, 2, 3]
  }'
```

The `prefix` field uses a digits+x pattern (e.g., `1xx` means numbers
100-199, `3xxx` means 3000-3999). This is used for team number randomization.

### Update a group

```bash
curl -b cookies.txt -X PATCH http://localhost:5001/api/groups/1 \
  -H "Content-Type: application/json" \
  -d '{"name": "Category A - Updated", "checkpoint_ids": [1, 2, 3, 4]}'
```

### Delete a group

```bash
curl -b cookies.txt -X DELETE http://localhost:5001/api/groups/1
```

Fails with 409 if any teams are actively assigned to the group.

### Reorder groups

```bash
curl -b cookies.txt -X POST http://localhost:5001/api/groups/order \
  -H "Content-Type: application/json" \
  -d '{"group_ids": [3, 1, 2]}'
```

Must include all group IDs for the current competition.

---

## Check-ins

### List check-ins (paginated)

```bash
curl -b cookies.txt "http://localhost:5001/api/checkins?page=1&per_page=50&sort=new"

# With filters
curl -b cookies.txt "http://localhost:5001/api/checkins?team_id=1&checkpoint_id=2&date_from=2025-10-01&date_to=2025-10-02"
```

Sort options: `new` (newest first, default), `old` (oldest first), `team`
(alphabetical by team name).

### Create a check-in

```bash
curl -b cookies.txt -X POST http://localhost:5001/api/checkins \
  -H "Content-Type: application/json" \
  -d '{
    "team_id": 1,
    "checkpoint_id": 2,
    "timestamp": "2025-10-17T14:30:00"
  }'
```

Returns 409 if the team already checked in at that checkpoint. Use
`"override": "replace"` to update the timestamp of an existing check-in.

### Update a check-in

```bash
curl -b cookies.txt -X PATCH http://localhost:5001/api/checkins/1 \
  -H "Content-Type: application/json" \
  -d '{"timestamp": "2025-10-17T14:35:00"}'
```

### Delete a check-in

```bash
curl -b cookies.txt -X DELETE http://localhost:5001/api/checkins/1
```

### Export check-ins as CSV

```bash
curl -b cookies.txt "http://localhost:5001/api/checkins/export.csv?sort=new" -o checkins.csv
```

---

## Export / Import / Merge

These endpoints transfer full competition data as JSON. See
[export-import.md](export-import.md) for the complete guide.

### Export a competition

```bash
curl -b cookies.txt "http://localhost:5001/api/competition/1/export" -o competition.json
```

### Import a competition (creates new)

```bash
# From JSON body
curl -b cookies.txt -X POST http://localhost:5001/api/competition/import \
  -H "Content-Type: application/json" \
  -d @competition.json

# From file upload
curl -b cookies.txt -X POST http://localhost:5001/api/competition/import \
  -F "file=@competition.json"
```

### Merge into existing competition

```bash
# Step 1: Dry run to detect conflicts
curl -b cookies.txt -X POST http://localhost:5001/api/competition/1/merge \
  -H "Content-Type: application/json" \
  -d @competition.json

# Step 2: Apply with conflict resolutions
curl -b cookies.txt -X POST http://localhost:5001/api/competition/1/merge \
  -H "Content-Type: application/json" \
  -d '{
    "schema_version": "1.0.0",
    "competition": {"name": "..."},
    "teams": [...],
    "groups": [...],
    "checkpoints": [...],
    "resolutions": {
      "team:Alpha Patrol": "use_imported",
      "checkpoint:Forest Gate": "keep_local",
      "group:Category A": "skip"
    }
  }'
```

---

## Ingest (Device Messages)

The ingest endpoint accepts LoRa device messages and automatically creates
check-ins when the payload matches an RFID card UID.

```bash
curl -X POST http://localhost:5001/api/ingest \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-secret" \
  -d '{
    "competition_id": 1,
    "dev_id": 1,
    "payload": "A1B2C3D4",
    "rssi": -62.5,
    "snr": 9.0
  }'
```

You can also send GPS data:

```bash
curl -X POST http://localhost:5001/api/ingest \
  -H "Content-Type: application/json" \
  -H "X-Webhook-Secret: your-secret" \
  -d '{
    "competition_id": 1,
    "dev_id": 1,
    "gps_lat": 46.05,
    "gps_lon": 14.51
  }'
```

Duplicate messages (same competition + device + payload within 10 seconds) are
automatically deduplicated.

---

## Users

### List users (current competition)

```bash
curl -b cookies.txt "http://localhost:5001/api/users"
```

### Create a user

```bash
curl -b cookies.txt -X POST http://localhost:5001/api/users \
  -H "Content-Type: application/json" \
  -d '{
    "username": "judge1",
    "password": "securepass123",
    "role": "judge"
  }'
```

Roles: `viewer`, `judge`, `admin`.

---

## Health Check

```bash
curl http://localhost:5001/health
# {"ok": true}
```

---

## Error Format

All errors follow this structure:

```json
{
  "error": "error_code",
  "detail": "Human-readable description."
}
```

Common error codes: `validation_error`, `not_found`, `duplicate`, `conflict`,
`forbidden`, `no_competition`, `invalid_request`.
