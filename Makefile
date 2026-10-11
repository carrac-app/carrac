# Carrac — developer commands. Run `make help` for the list.

# Optional, gitignored hook for your own targets and overrides (extra deploy
# or ops targets, a different PYTHON, ...). Absent on a fresh clone.
-include local.mk

# pytest-xdist worker cap: 4 keeps a full run from saturating a shared box
# (`-n auto` spawns one worker per CPU). Override: `make test PYTEST_WORKERS=auto`.
PYTEST_WORKERS ?= 4

PYTHON := $(or $(wildcard .venv/bin/python3),$(wildcard venv/bin/python3),python3)

.PHONY: help lint dev-install audit test test-js test-sdk test-quant test-cov dev configure dev-down dev-logs migration db-history

help: ## Show this help
	@grep -hE '^[a-zA-Z_-]+:.*?## .*$$' Makefile | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

lint: ## Run ruff (Python) and eslint (JS)
	@printf "[1/2] ruff (Python)...\n"
	@$(PYTHON) -m ruff check .
	@printf "[2/2] eslint (JS)...\n"
	@npx --yes eslint@8 --ignore-pattern '*.min.js' static/js/
	@printf "Lint passed\n"

dev-install: ## Install the dev tooling (ruff, pytest, pip-audit) at the pinned versions
	@$(PYTHON) -m pip install -r requirements-dev.txt

audit: ## Check Python dependencies for known vulnerabilities
	@$(PYTHON) -m pip_audit -r requirements.lock --no-deps --disable-pip

test-js: ## Run the JavaScript unit tests
	@node tests/test_sync_js.mjs
	@node tests/test_drawings_js.mjs
	@node tests/test_backtester_js.mjs
	@node tests/test_toolbar_ux_js.mjs
	@node --test tests/js/*.test.cjs tests/js/*.check.mjs tests/parity/*.test.mjs

test: lint test-js ## Run lint, the JavaScript tests and the full pytest suite
	@$(PYTHON) -m pytest tests/ -q --tb=long -n $(PYTEST_WORKERS)

test-sdk: ## Run the Python and TypeScript SDK test suites
	@python3 -m venv .sdk-venv
	@.sdk-venv/bin/pip install -q -e "./sdk-python[dev]"
	@cd sdk-python && $(CURDIR)/.sdk-venv/bin/python -m pytest tests -q
	@cd sdk-typescript && npm ci --no-audit --no-fund && npm test && npm run typecheck

test-quant: ## Run the quant engine tests only
	@$(PYTHON) -m pytest tests/test_quant.py -v --tb=short

test-cov: ## Run the suite with a coverage report
	@$(PYTHON) -m pytest tests/ -q --tb=short --cov --cov-report=term-missing -m "not integration and not slow"

dev: ## Bring up the local dev stack and print the login (docker-compose.dev.yml)
	@./scripts/quickstart.sh

configure: ## Walk through the optional keys (data, AI, email, notifications) and write them into .env
	@./scripts/configure.sh

dev-down: ## Stop the local dev stack (data persists)
	docker compose -f docker-compose.dev.yml down

dev-logs: ## Follow the local dev app and worker logs
	docker compose -f docker-compose.dev.yml logs -f carrac worker

migration: ## Create a new migration: make migration msg="add foo column"
	@if [ -z "$(msg)" ]; then echo "Usage: make migration msg=\"description\""; exit 1; fi
	@$(PYTHON) -m alembic revision -m "$(msg)"

db-history: ## Show the migration history
	@$(PYTHON) -m alembic history --verbose
