# Backup & Restore

The application database (SQLite) lives at `instance/app.db` inside the
`web` container, mounted from the host's `instance/` directory in the
prod compose file. If that volume is lost, every check-in, score, audit
event, and competition is gone with it. The audit log lets you
reconstruct *what* happened, but only inside one DB; it can't bring the
database back.

## What needs to be backed up

| Path | Why |
|---|---|
| `instance/app.db` | The whole live database. |
| `instance/app.db-wal`, `instance/app.db-shm` | Only present if WAL is enabled (see runbook). Skip if absent. |
| `data/` | Firmware uploads and any persisted blobs. |
| `deploy/.env` | Secrets and DOMAIN/ACME_EMAIL. **Encrypt before storing offsite.** |
| `google_sa.json` | Sheets service-account key. **Same.** |

The OS-level config (Caddyfile, compose files) lives in this Git repo and
is implicitly backed up.

## Daily backup (recommended)

`sqlite3 .backup` is the safe way to copy a live database - it uses
SQLite's online backup API and works correctly even while the app is
writing.

Drop this script at `deploy/backup.sh` and `chmod +x` it:

```bash
#!/usr/bin/env bash
set -euo pipefail

INSTANCE_DIR="${INSTANCE_DIR:-$(dirname "$0")/../instance}"
BACKUP_DIR="${BACKUP_DIR:-$(dirname "$0")/../backups}"
RETAIN_DAYS="${RETAIN_DAYS:-14}"

mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
out="$BACKUP_DIR/app-$stamp.db"

sqlite3 "$INSTANCE_DIR/app.db" ".backup '$out'"
gzip -9 "$out"

# Prune old backups
find "$BACKUP_DIR" -name 'app-*.db.gz' -mtime "+$RETAIN_DAYS" -delete
```

Schedule via host cron - the laptop runs the container, so cron lives
on the laptop, not in the container:

```cron
# Every hour during race weekend, daily otherwise.
0 * * * * /home/<user>/lora-kt/deploy/backup.sh >> /var/log/lora-kt-backup.log 2>&1
```

For the day of the race, change to every 15 minutes:

```cron
*/15 * * * * /home/<user>/lora-kt/deploy/backup.sh
```

## Off-host backup

A backup that lives on the same disk as the DB doesn't survive a disk
failure. Pick one (in order of preference for this setup):

1. **rsync to a USB stick or second machine** at the end of each day.
2. **`rclone copy backups/ remote:lora-kt-backups/`** to Google Drive,
   Dropbox, or any S3-compatible bucket. The `.db.gz` files are small
   (a few MB even with thousands of check-ins).
3. **scp to a phone or laptop** at minimum.

Encrypt `.env` and `google_sa.json` separately if they leave the host -
e.g. `age -p` or `gpg -c`.

## Restore drill

Run this **before race day** so you know it works:

```bash
# 1. Stop everything that writes to the DB (web AND the sheets worker)
docker compose -f deploy/docker-compose.prod.yml stop web sheets-worker

# 2. Move the live DB out of the way (don't delete!)
mv instance/app.db instance/app.db.broken

# 3. Decompress the backup of choice
gunzip -k backups/app-YYYYMMDDTHHMMSSZ.db.gz
mv backups/app-YYYYMMDDTHHMMSSZ.db instance/app.db

# 4. Restart and verify
docker compose -f deploy/docker-compose.prod.yml start web sheets-worker
curl -fsS http://localhost/ready
```

Smoke-test by logging in, listing check-ins, and confirming the
expected counts.

## What this doesn't cover

- **Live replication.** For zero-downtime recovery look at
  [Litestream](https://litestream.io). Overkill for a single-laptop
  scout race; fine to skip.
- **Cross-machine consistency.** SQLite is single-host. If the laptop
  itself is lost, you restore on a new machine from the off-host
  backup.
- **Volume snapshots.** Docker volume drivers don't snapshot SQLite
  safely; always go through `sqlite3 .backup`.
