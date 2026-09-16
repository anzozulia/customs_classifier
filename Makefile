# UKTZED v2 — the commands you run more than once. Nothing here hides anything:
# every target is the command you would have typed.
#
# Container targets need .env (copy .env.example). Host targets (dev, test, lint,
# fmt, spike) need a local venv with the pyproject dependencies installed.

COMPOSE ?= docker compose

.DEFAULT_GOAL := help
.PHONY: help dev up down logs migrate ingest user test lint fmt spike

help: ## show this list
	@grep -hE '^[a-z][a-z-]*:.*##' $(MAKEFILE_LIST) | sed -E 's/:[^#]*## /\t/' | expand -t 12

dev: ## run the API with reload + the Vite dev server; ctrl-C stops both
	trap 'kill 0' EXIT INT TERM; npm --prefix web run dev & uvicorn app.main:app --reload --port 8000

up: ## build and start caddy + app + db (migrate runs first, to completion)
	$(COMPOSE) up -d --build && $(COMPOSE) ps

down: ## stop the stack and KEEP the data (`docker compose down -v` destroys pg_data)
	$(COMPOSE) down

logs: ## follow the app log
	$(COMPOSE) logs -f --tail=100 app

migrate: ## alembic upgrade head, in a one-shot container against the running db
	$(COMPOSE) run --rm migrate

ingest: ## load data/uktzed_hierarchical.json (idempotent per content hash; expect 14187 / 10490)
	$(COMPOSE) run --rm app python -m app.cli ingest data/uktzed_hierarchical.json

user: ## create a login — make user USERNAME=anton (prints a generated password once)
	$(COMPOSE) exec app python -m app.cli create-user $(or $(USERNAME),$(error USERNAME is required, e.g. `make user USERNAME=anton`))

test: ## run the test suite
	pytest -q

lint: ## ruff check + format check, exactly as CI runs it
	ruff check . && ruff format --check .

fmt: ## autoformat and apply the safe lint fixes
	ruff format . && ruff check --fix .

spike: ## M0 smoke run — drives ClassifierServer.respond() against the real API, prints PASS/FAIL
	OPENAI_AGENTS_DISABLE_TRACING=1 python scripts/spike_m0.py
