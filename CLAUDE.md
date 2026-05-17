# LoRa KT — Claude Code project notes

Flask + SQLite + Alembic + Babel app for tracking scout-orienteering
("Taborniki") competitions over LoRa radio + RFID. Production launch
planned for late May 2026.

## Working environment

- **Python interpreter:** `venv/bin/python` (3.13). Single venv at the
  project root.
- **Lint:** `venv/bin/ruff check .` (config in `pyproject.toml`,
  rules `F, I, UP, B, E, W`).
- **Tests:** `venv/bin/python -m pytest -q`. Full suite is ~70s,
  ~400 tests, ~23 skipped (Sheets tests need a service-account file).
  Test subprocesses use `sys.executable` (not a hardcoded venv path)
  so they work in CI too.
- **Translations:** `venv/bin/pybabel extract -F babel.cfg -o messages.pot app scripts`,
  then `venv/bin/pybabel update -d app/translations -l sl`, then
  `venv/bin/pybabel compile -d app/translations`. **Always pass
  `app scripts` explicitly to `extract`** — the bare `babel.cfg` glob
  slurps in venv contents.
- **Migrations:** Alembic, batch mode for SQLite. Two-step deploy:
  fresh installs use `db.create_all()` + `alembic stamp head`;
  in-place upgrades use `alembic upgrade head`.

## Pre-approved commands

The following are allowed without prompting (see
`.claude/settings.local.json`): `pytest`, `ruff`, `pybabel`, `alembic`,
`git status/diff/log/add/commit/restore/branch/checkout`, file edits via
the Edit/Write tools. Use them freely during long-running work.

## Conventions

- **Commit messages:** no `Co-Authored-By` trailers. Lead with a short
  summary, blank line, then a body that explains *why*.
- **Tests live under `tests/`.** Use the helpers in `tests/support.py`
  (`create_user`, `create_competition`, `add_membership`, `login_as`,
  etc.) rather than building fixtures from scratch.
- **Per-competition role checks** must use `CompetitionMember.role`
  for the active competition. The global `User.role` field is reserved
  for `superadmin` (system bypass) and `public`. Never write per-comp
  roles into `User.role`.
- **i18n:** wrap user-facing strings with `_()` from `flask_babel`.
  Use named placeholders (`_("Hi %(name)s", name=x)`) — never f-strings
  or `_(variable)`, which Babel can't extract.
- **Schema changes** go through Alembic. Don't add boot-time
  `ALTER TABLE` blocks to `app/__init__.py`.
- **Time policy:** all timestamps stored in UTC; display in
  `Europe/Ljubljana` (set in `app/utils/time.py`). Templates pull
  the display timezone from the `display_timezone` context var, never
  hardcode it.

## Audit branch

The branch `llm_audit` accumulates pre-launch hardening. PRs into
`master` happen at coherent checkpoints, not every commit. Tests must
pass + ruff must be clean before any commit lands.
