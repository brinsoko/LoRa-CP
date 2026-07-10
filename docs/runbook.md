# Operations Runbook

What to do when things go wrong (or when you just need to do a thing).
You are the sole operator; this is the page to read at 6am during a
race.

> **Where stuff lives**
> - App container: `web` in `deploy/docker-compose.prod.yml`
> - Sheets writer: `sheets-worker` in the same compose file (exactly ONE
>   replica; it drains the sheets_sync_jobs outbox, web only enqueues).
>   Restart it together with `web` after a deploy.
> - Reverse proxy: `caddy` (HTTPS termination)
> - Database: `instance/app.db` (SQLite)
> - Logs: `docker logs lora-kt-web` (or whatever you named the
>   container — see compose file)
> - Health: `http://localhost/health` (cheap), `http://localhost/ready`
>   (touches DB)

---

## Common operations

### Restart the app

```bash
cd deploy
docker compose -f docker-compose.prod.yml restart web sheets-worker
```

The Caddy container can stay up. If you also need to restart Caddy:
```bash
docker compose -f docker-compose.prod.yml restart caddy
```

### Pull a new image and redeploy

```bash
cd deploy
docker compose -f docker-compose.prod.yml pull web
docker compose -f docker-compose.prod.yml up -d web
```

The container restarts in place; existing connections drop briefly.

### Check the app is healthy

```bash
curl -fsS https://<your-domain>/health   # 200 if process is up
curl -fsS https://<your-domain>/ready    # 200 if DB is reachable, 503 if not
docker compose -f docker-compose.prod.yml ps   # all containers should be 'running'
```

### Tail logs

```bash
docker logs -f --tail 200 <web-container-name>
```

Production logs at INFO level — every request is in werkzeug's access
log. For DEBUG, set `FLASK_DEBUG=1` in `deploy/.env` and restart;
remember to switch off afterwards.

---

## Backup & restore

See [backup.md](backup.md) for the full picture, but the short version:

**Trigger an ad-hoc backup right now:**
```bash
deploy/backup.sh
```

**Restore from a backup:**
```bash
cd deploy
docker compose -f docker-compose.prod.yml stop web sheets-worker
mv ../instance/app.db ../instance/app.db.broken
gunzip -k ../backups/app-<TIMESTAMP>.db.gz
mv ../backups/app-<TIMESTAMP>.db ../instance/app.db
docker compose -f docker-compose.prod.yml start web sheets-worker
curl -fsS http://localhost/ready
```

---

## Rotating secrets

### LORA_WEBHOOK_SECRET

LoRa devices and the Web NFC tools authenticate using this header. Roll
it like this:

1. Generate a new secret:
   ```bash
   openssl rand -hex 32
   ```
2. Update `deploy/.env`:
   ```
   LORA_WEBHOOK_SECRET=<new>
   ```
3. Restart the app: `docker compose ... restart web`.
4. Update every device firmware / phone app to use the new value.

A logged-in admin/judge can still ingest without the secret while you
roll devices, so plan a window where both old and new are accepted by
the proxy if needed (custom Caddy rule).

### SECRET_KEY (Flask session)

Rotating this **invalidates every active session** — every user has to
log in again. Don't do this mid-race.

```bash
openssl rand -hex 32
```
Update `deploy/.env`, restart.

### DEVICE_CARD_SECRET (NFC HMAC)

Rotating this invalidates every previously-written NFC tag's HMAC.
Don't rotate during a race. Don't rotate without a plan to rewrite
every team's tags.

### Google OAuth client secret

Rotate in [Google Cloud Console](https://console.cloud.google.com/apis/credentials):
1. Generate a new client secret.
2. Update `GOOGLE_OAUTH_CLIENT_SECRET` in `deploy/.env`.
3. Restart `web`.
4. Revoke the old secret in the GCP console.

Sign-ins remain valid (sessions persist), but no new OAuth logins until
restart.

---

## Emergency DB surgery

When you need to fix data directly (e.g. judge fat-fingered a check-in
on a checkpoint that's now closed and audit log won't help):

1. **Take a backup first.** Always.
   ```bash
   deploy/backup.sh
   ```
2. **Stop writes** while you work; both the app and the sheets worker
   hold the DB open:
   ```bash
   docker compose -f deploy/docker-compose.prod.yml stop web sheets-worker
   ```
3. **Open the DB** with sqlite3:
   ```bash
   sqlite3 instance/app.db
   sqlite> .schema checkins
   sqlite> SELECT * FROM checkins WHERE id = 12345;
   sqlite> UPDATE checkins SET timestamp = '2026-05-09 14:32:00' WHERE id = 12345;
   sqlite> .quit
   ```
4. **Restart**:
   ```bash
   docker compose -f deploy/docker-compose.prod.yml start web sheets-worker
   ```
5. **Add an audit note** through the app once it's back up so future-you
   knows what changed.

---

## Performance / load

### "Database is locked" errors

SQLite's default `journal_mode=DELETE` serializes readers and writers.
Under race-day load (LoRa ingest + judges + Sheets sync) you may see
this. Enabling WAL mode is the single biggest improvement; it's a
one-time DB-level setting.

```bash
sqlite3 instance/app.db
sqlite> PRAGMA journal_mode=WAL;
sqlite> PRAGMA wal_autocheckpoint=1000;
sqlite> .quit
```

WAL persists in the DB file header, so you only need to do this once.
After enabling, you'll see `instance/app.db-wal` and `instance/app.db-shm`
files alongside the main DB — make sure backups capture all three (the
backup.sh script already uses `.backup` which handles this correctly).

### Sheets API quotas

Each check-in and score enqueues a Google Sheets write. Free quota is
60 writes per minute per user. If logs show `429 Too Many Requests`
from gspread:

- Confirm `SHEETS_SYNC_ENABLED=1` is wanted; setting it to `0` disables
  Sheets entirely (all data still hits the DB).
- Writes go through the durable outbox (`sheets_sync_jobs` table), so
  quota errors never wedge the app or lose data. The `sheets-worker`
  process retries with exponential backoff and coalesces repeat writes
  for the same row via dedup keys; Sheets catches up once quota resets.

### Sheets lagging or stuck jobs

The sheets admin page (`/sheets/`) has a health panel showing the
pending-job count and a table of failed (dead-lettered) jobs with
per-job retry and delete buttons. If jobs pile up:

- Check the worker is running:
  `docker compose -f docker-compose.prod.yml ps sheets-worker`.
- Tail its logs: `docker logs -f <sheets-worker-container-name>`.
- Jobs that exhaust their retries land in status `failed` with the
  last error attached; fix the cause (usually credentials or a deleted
  tab), then retry them from the panel.

Exactly ONE `sheets-worker` replica must run; a second replica would
race the first on job claims and double-write rows.

---

## What's NOT addressed for v1 (known deferred items)

These items came up in the prelaunch audit and were intentionally
deferred. Listed here so they don't get lost.

| Item | Why deferred | Severity |
|---|---|---|
| Production Google OAuth client secret rotation | Dev-only setup right now | High when launching prod |
| Sentry / error reporting | Single-user laptop op, manual log review fine | Low |
| Multiple gunicorn workers | Single laptop, blocking Sheets call now async | Low |
| Postgres migration | SQLite + WAL handles current scale | Low |
| Postgres-grade migrations review | Schema changes go through Alembic now (create_all only seeds fresh installs); older revisions never rehearsed on Postgres | Low |
| Subresource Integrity on dynamic ESM imports (firmware flasher) | Admin-only tool | Low |
| `google_sa.json` mount in deploy compose | Sheets sync defaults to enabled but file isn't mounted; sync silently no-ops | Medium — flip `SHEETS_SYNC_ENABLED=0` in `.env` if not using Sheets |

See [security.md](security.md) for the threat-model context behind a few
of these.

---

## Quick reference

| Symptom | Likely cause | Action |
|---|---|---|
| `/ready` returns 503 | DB unreachable | Check disk, check `instance/app.db` permissions |
| Login form gives 429 | Someone hammered login | Wait 1 minute, or restart `web` to flush in-memory limiter |
| Checkin returns 409 "duplicate" | Same team/checkpoint already recorded | Use override=replace, or fix in audit |
| Ingest returns 403 "Invalid webhook secret" | Device using old secret | Rotate device firmware or restore old secret |
| OAuth redirect_uri error | ProxyFix not seeing X-Forwarded-Proto | Check Caddy is sending headers; verify `app.wsgi_app = ProxyFix(...)` is in code |
| Sheets columns blank | `google_sa.json` not mounted, or quota exhausted, or `SHEETS_SYNC_ENABLED=0` | Check `deploy/.env`, check container logs for 429s |
