#!/usr/bin/env bash
# Online backup of the SQLite database. Safe while the app is running:
# uses sqlite3's .backup command which acquires the appropriate locks
# without blocking writes for more than a brief moment.
#
# Run from cron on the host:
#   0 * * * * /path/to/deploy/backup.sh >> /var/log/lora-kt-backup.log 2>&1
# See docs/backup.md for the full retention/restore drill.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTANCE_DIR="${INSTANCE_DIR:-$SCRIPT_DIR/../instance}"
BACKUP_DIR="${BACKUP_DIR:-$SCRIPT_DIR/../backups}"
RETAIN_DAYS="${RETAIN_DAYS:-14}"

if [[ ! -f "$INSTANCE_DIR/app.db" ]]; then
    echo "$(date -u +%FT%TZ) ERROR: $INSTANCE_DIR/app.db not found" >&2
    exit 1
fi

mkdir -p "$BACKUP_DIR"
stamp="$(date -u +%Y%m%dT%H%M%SZ)"
tmp="$BACKUP_DIR/app-$stamp.db.tmp"
out="$BACKUP_DIR/app-$stamp.db.gz"

# Online backup via sqlite3's backup API (requires sqlite3 on the host).
sqlite3 "$INSTANCE_DIR/app.db" ".backup '$tmp'"
gzip -9 "$tmp"
mv "$tmp.gz" "$out"

echo "$(date -u +%FT%TZ) wrote $(du -h "$out" | cut -f1) -> $out"

# Retention: drop backups older than RETAIN_DAYS days.
find "$BACKUP_DIR" -name 'app-*.db.gz' -mtime "+$RETAIN_DAYS" -delete
