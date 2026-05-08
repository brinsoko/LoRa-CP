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
- ProxyFix is enabled in `app/__init__.py` so `request.scheme` and
  `request.host` reflect the proxied values; required for OAuth
  `redirect_uri` and `_external=True` URLs to come out as `https://`.

### Rate limiting
- `flask-limiter` with in-memory storage, single-process scope.
- `/api/auth/login` and `/login` (HTML form) limited to **10 requests
  per minute, 60 per hour, per IP**. Resets on app restart.

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

## Cookie flags (deferred, not yet set)

The Flask defaults don't set `Secure` / `SameSite` on the session
cookie. Production is HTTPS-only via Caddy, but a stricter posture
would be:

```python
app.config.update(
    SESSION_COOKIE_SECURE=True,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PREFERRED_URL_SCHEME="https",
)
```

Not blocking launch but worth a follow-up. `HTTPONLY` is already true
by default; the missing pieces are `Secure` and `SameSite`.

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
