# Security model & deferred items

## What's enforced today

### Auth
- Password hashing via Werkzeug's PBKDF2 default.
- Session cookies via Flask-Login's defaults. Flask serializes sessions
  with `itsdangerous` keyed off `SECRET_KEY` — so rotating that key
  invalidates every session.
- CSRF: custom token in `app/utils/csrf.py`, session-bound, verified on
  every state-changing request (`POST/PUT/PATCH/DELETE`) using
  `secrets.compare_digest`. Exemptions live in
  [app/utils/csrf.py](../app/utils/csrf.py); `/api/auth/login` is
  exempt because the HTML login form proxies through the API endpoint
  and brings its own form-level token.

### Authorization
- Per-competition roles: `admin`, `judge`, `viewer` stored in
  `competition_members`. `@json_roles_required(...)` is the JSON gate;
  `@roles_required(...)` is the HTML gate.
- `/api/checkins/<id>` GET/PUT/PATCH require `judge` or `admin`.
  DELETE requires `admin`.
- `/api/ingest`: when `LORA_WEBHOOK_SECRET` is configured, callers
  must either present `X-Webhook-Secret` matching it, or be a
  logged-in `admin`/`judge` of the target competition. Authenticated
  viewers and non-members are rejected. In dev (default
  `CHANGE_LATER`) the endpoint is open — the prod startup guard in
  `config.py` refuses to boot with that value.

### Transport
- Caddy terminates TLS via Let's Encrypt (see `deploy/Caddyfile`).
- HSTS, X-Content-Type-Options, X-Frame-Options, Referrer-Policy
  are set globally. The `Server` header is stripped.
- ProxyFix is **gated behind `TRUST_PROXY_HEADERS`** in `config.py`.
  It defaults to on when `FLASK_ENV=production` (so requests behind
  Caddy get the right `request.scheme`/`request.host` for OAuth
  `redirect_uri` and `_external=True` URLs), and off otherwise.
  The flag MUST be off when the Flask process is reachable directly,
  otherwise clients can spoof `X-Forwarded-Host` / `X-Forwarded-Proto`
  and manipulate any external URL the app emits. Caddy in front of
  prod is responsible for stripping inbound `X-Forwarded-*` before
  injecting its own.

### Session cookies
- `SESSION_COOKIE_HTTPONLY=True` always (XSS can't read the cookie).
- `SESSION_COOKIE_SECURE=True` in production (cookie never goes over
  plain HTTP). Off in dev so `flask run` over `http://localhost`
  still authenticates.
- `SESSION_COOKIE_SAMESITE=Lax` (default; overridable via env).
  Blocks cross-site form POSTs from carrying the cookie while still
  allowing top-level navigations into the app (so external links and
  OAuth callbacks work).

### Rate limiting
- `flask-limiter` with in-memory storage, single-process scope.
- `/api/auth/login` and `/login` (HTML form) limited to **10 requests
  per minute, 60 per hour, per IP**. Same limit applies to
  `/api/auth/password` so an authenticated attacker can't brute-force
  the current-password check. Resets on app restart.

### CDN integrity
- All `<script>` and `<link>` tags loaded from jsdelivr/unpkg use SRI
  hashes (`integrity="sha384-..."` or sha256 for Leaflet).
- Pinned versions: Bootstrap 5.3.2, Leaflet 1.9.4, SortableJS 1.15.2,
  Chart.js 4.4.1, swagger-ui-dist 5.18.2.
- Dynamic ESM imports in `firmware_flash.html` are **not** SRI-protected
  (limitation of import maps + the test-flow nature of the page);
  acceptable since that page is admin-only.

### Path traversal
- `/api/docs/<filename>` and `/docs/*` routes use `werkzeug.utils.safe_join`
  and require `@json_login_required`. Previously open + filtered by a
  string `replace("..", "")` which was bypassed by `....//`.

### Race-condition safety
- `Checkin` insert paths in `app/resources/ingest.py` and
  `app/resources/checkins.py` are wrapped in `db.session.begin_nested()`
  so a concurrent caller inserting first and triggering the
  `uq_team_checkpoint` constraint surfaces as 409 "duplicate" instead
  of 500.

## CSP (deferred)

No Content-Security-Policy header is sent. CDN scripts now have SRI
which mitigates supply-chain compromise; CSP would additionally guard
against XSS via injected `<script>`. Templates use a fair amount of
inline JS (Bootstrap init, theme switch, map init) so a strict CSP
would need either:
- `'unsafe-inline'` in `script-src` (mostly defeats CSP), or
- `'nonce-...'` hooked into a Flask `after_request`, or
- Refactor inline scripts into static files.

Not blocking launch. Track as follow-up.

## Other deferred items

See [runbook.md](runbook.md#whats-not-addressed-for-v1-known-deferred-items)
for the full list.
