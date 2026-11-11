# Shortcuts for the commands in CLAUDE.md. Run `make` to list them.
.DEFAULT_GOAL := help
.PHONY: help install dev test lint format check migrate migration current docker docker-run

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  make %-12s %s\n", $$1, $$2}'

install: ## Install dependencies (uv sync)
	uv sync

dev: ## Run the API with reload on :8000 (docs at /docs)
	uv run fastapi dev app/main.py

test: ## Run tests
	uv run pytest -q

lint: ## Lint + format check (what CI runs)
	uv run ruff check .
	uv run ruff format --check .

format: ## Auto-fix lint and format
	uv run ruff check --fix .
	uv run ruff format .

check: lint test ## Everything CI checks, before you push

migrate: ## Apply migrations to DATABASE_URL (shared Supabase: get a team OK first)
	uv run alembic upgrade head

migration: ## New empty migration: make migration m="add_x_to_y"
	@test -n "$(m)" || { echo 'usage: make migration m="verb_noun"'; exit 1; }
	uv run alembic revision -m "$(m)"

current: ## Show which migration the database is on
	uv run alembic current

docker: ## Build the production image
	docker build -t recruitment-backend .

docker-run: docker ## Run the image on :8000 with your local .env
	docker run --rm -p 8000:8000 --env-file .env recruitment-backend
