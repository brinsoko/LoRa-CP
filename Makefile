# ----------------------------
# LoRa KT project Makefile
# ----------------------------

VENV_BIN        := $(if $(wildcard venv/bin/python),venv/bin/,)
PYTHON          ?= $(VENV_BIN)python
PIP             ?= $(VENV_BIN)pip
PYTEST          ?= $(VENV_BIN)pytest
PYBABEL         ?= $(VENV_BIN)pybabel
FLASK           ?= $(PYTHON) -m flask
COMPOSE         ?= docker compose
PORT            ?= 5001
FLASK_APP       ?= app:create_app
export FLASK_APP
export FLASK_ENV ?= development

SEED_SKIP_DEMO  ?= 0
SEED_TEAMS_CSV  ?=
TEST_ARGS       ?=
BASE_URL        ?= http://127.0.0.1:5001

.PHONY: help
help:
	@echo ""
	@echo "Common targets:"
	@echo "  make install           - install runtime requirements"
	@echo "  make install-dev       - install runtime + dev/test requirements"
	@echo "  make run               - run Flask dev server on :$(PORT)"
	@echo "  make shell             - open Python REPL with project venv/interpreter"
	@echo "  make seed              - seed demo/test data"
	@echo "  make seed-fresh        - drop/create local DB, then seed"
	@echo "  make admin             - create/update admin user via scripts/create_admin.py"
	@echo "  make db-init           - create tables if missing"
	@echo "  make db-rebuild        - interactive local DB rebuild"
	@echo "  make test              - run full pytest suite"
	@echo "  make test-fast         - run core suites"
	@echo "  make test-matrix       - run endpoint matrix suite"
	@echo "  make test-extended     - run extended regression suite"
	@echo "  make cov               - run tests with coverage"
	@echo "  make i18n-compile      - compile translations (.po -> .mo)"
	@echo "  make openapi-check     - validate docs/openapi.json parses"
	@echo "  make smoke-int         - run integer-input smoke script"
	@echo "  make stress-help       - show stress test script options"
	@echo "  make erd               - render ERD"
	@echo "  make up/down/logs/sh   - docker compose helpers"
	@echo "  make clean             - remove caches"
	@echo ""

.PHONY: install install-dev
install:
	$(PIP) install -r requirements.txt

install-dev:
	$(PIP) install -r requirements.txt -r requirements-dev.txt

.PHONY: run
run:
	$(FLASK) run --debug --port $(PORT)

.PHONY: shell
shell:
	$(PYTHON)

.PHONY: seed seed-fresh admin erd
seed:
	$(PYTHON) scripts/seed_db.py $(if $(filter 1 true yes,$(SEED_SKIP_DEMO)),--skip-demo,) $(if $(SEED_TEAMS_CSV),--teams-csv $(SEED_TEAMS_CSV),)

seed-fresh:
	$(PYTHON) scripts/seed_db.py --fresh $(if $(filter 1 true yes,$(SEED_SKIP_DEMO)),--skip-demo,) $(if $(SEED_TEAMS_CSV),--teams-csv $(SEED_TEAMS_CSV),)

admin:
	ADMIN_USER=$(or $(user),admin) ADMIN_PASS=$(or $(pass),admin123) ADMIN_ROLE=$(or $(role),admin) $(PYTHON) scripts/create_admin.py

erd:
	$(PYTHON) scripts/render_erd.py

.PHONY: db-init db-rebuild db-reset
db-init:
	@printf '%s\n' "from app import create_app" \
	               "from app.extensions import db" \
	               "app = create_app()" \
	               "with app.app_context():" \
	               "    db.create_all()" \
	               "print('Done.')" | $(PYTHON) -

db-rebuild:
	$(PYTHON) scripts/rebuild_db.py

db-reset: db-rebuild

.PHONY: test test-fast test-matrix test-extended cov
test:
	$(PYTEST) tests $(TEST_ARGS)

test-fast:
	$(PYTEST) tests/test_lora_cp.py tests/test_lora_cp_extended.py $(TEST_ARGS)

test-matrix:
	$(PYTEST) tests/test_endpoint_matrix.py $(TEST_ARGS)

test-extended:
	$(PYTEST) tests/test_lora_cp_extended.py $(TEST_ARGS)

cov:
	$(PYTEST) --cov=app --cov-report=term-missing tests $(TEST_ARGS)

.PHONY: i18n-compile openapi-check smoke-int stress-help
i18n-compile:
	$(PYBABEL) compile -d app/translations

openapi-check:
	@printf '%s\n' "import json, pathlib" \
	               "json.loads(pathlib.Path('docs/openapi.json').read_text())" \
	               "print('docs/openapi.json OK')" | $(PYTHON) -

smoke-int:
	BASE_URL=$(BASE_URL) ./scripts/int_input_smoke_tests.sh

stress-help:
	$(PYTHON) tests/stress_test.py --help

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

.PHONY: clean
clean:
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +
	find . -type d -name ".pytest_cache" -prune -exec rm -rf {} +
