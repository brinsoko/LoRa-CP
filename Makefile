# ----------------------------
# LoRa / Flask project Makefile
# ----------------------------

# --- Config (tweak if needed) ---
PYTHON          ?= python3
PIP             ?= pip3
FLASK           ?= flask
COMPOSE         ?= docker compose
PORT            ?= 5001
FLASK_APP       ?= app:create_app          # app factory
export FLASK_APP
export FLASK_ENV=development

# Default target
.PHONY: help
help:
	@echo ""
	@echo "Common targets:"
	@echo "  make install         - pip install -r requirements.txt"
	@echo "  make run             - run Flask dev server on :$(PORT)"
	@echo "  make seed            - seed demo data"
	@echo "  make seed-fresh      - DROP+CREATE tables, then seed"
	@echo "  make admin           - creates/updates admin:admin123"
	@echo "  make erd             - render ERD to scripts/erd.png"
	@echo "  make db-migrate msg='message' - autogenerate migration"
	@echo "  make db-upgrade      - apply migrations"
	@echo "  make db-downgrade n=1- revert n steps (default 1)"
	@echo "  make db-reset        - DANGER: drop+create ALL tables"
	@echo "  make up              - docker compose up"
	@echo "  make down            - docker compose down"
	@echo "  make logs            - docker compose logs -f web"
	@echo "  make sh              - shell into web container"
	@echo ""

# --- Local (host) workflow ---
.PHONY: install
install:
	$(PIP) install -r requirements.txt

.PHONY: run
run:
	$(FLASK) run --port $(PORT)

.PHONY: seed
seed:
	$(PYTHON) scripts/seed_db.py

.PHONY: seed-fresh
seed-fresh:
	$(PYTHON) scripts/seed_db.py --fresh

.PHONY: admin
# Usage:
#   make admin                   # creates/updates admin:admin123
#   make admin user=alice pass=secret role=judge

admin:
	PYTHONPATH=. ADMIN_USER=$(user) ADMIN_PASS=$(pass) ADMIN_ROLE=$(role) $(PYTHON) scripts/create_admin.py

.PHONY: erd
erd:
	$(PYTHON) scripts/render_erd.py

# --- Flask-Migrate (if enabled in your app factory) ---
# Usage: make db-migrate msg="add lora device"
.PHONY: db-init db-migrate db-upgrade db-downgrade
db-init:
	$(FLASK) db init
db-migrate:
	@test "$(msg)" != "" || (echo "Set msg=\"your message\"" && exit 2)
	$(FLASK) db migrate -m "$(msg)"
db-upgrade:
	$(FLASK) db upgrade
db-downgrade:
	$(FLASK) db downgrade -n $(n)

# --- NO-MIGRATIONS emergency reset (uses SQLAlchemy directly) ---
# DANGER: drops and recreates ALL tables
.PHONY: db-reset
db-reset:
	@echo "!!! DANGER: Dropping and recreating ALL tables !!!"
	@read -p "Type 'yes' to continue: " ans; \
	if [ "$$ans" = "yes" ]; then \
		$(PYTHON) - <<-PY
		from app import create_app
		from app.extensions import db

		app = create_app()
		with app.app_context():
		    db.drop_all()
		    db.create_all()
		print("Done.")
		PY
	else echo "Cancelled."; fi

# --- Docker compose helpers ---
.PHONY: build up down logs sh
build:
	$(COMPOSE) build
up:
	$(COMPOSE) up
down:
	$(COMPOSE) down
logs:
	$(COMPOSE) logs -f web
sh:
	$(COMPOSE) exec web bash

# --- Cleanup ---
.PHONY: clean
clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
