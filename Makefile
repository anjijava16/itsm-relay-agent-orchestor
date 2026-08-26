.PHONY: help install dev up down logs migrate seed test lint fmt typecheck eval worker beat clean

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

install: ## Install dependencies with uv
	uv venv && uv pip install -e ".[dev]"

up: ## Start the whole stack
	docker compose up -d --build
	@echo "API      http://localhost:8000/docs"
	@echo "Flower   http://localhost:5555"
	@echo "OS Dash  http://localhost:5601"
	@echo "MinIO    http://localhost:9001"

down: ## Stop everything
	docker compose down

nuke: ## Stop everything and delete volumes
	docker compose down -v

logs: ## Tail API + worker logs
	docker compose logs -f api worker

dev: ## Run the API locally with reload
	uvicorn app.main:app --reload --port 8000

worker: ## Run a Celery worker locally
	celery -A app.workers.celery_app.celery_app worker -Q ingestion,maintenance -l info

beat: ## Run the Celery scheduler locally
	celery -A app.workers.celery_app.celery_app beat -l info

migrate: ## Apply database migrations
	alembic upgrade head

revision: ## Autogenerate a migration: make revision m="add x"
	alembic revision --autogenerate -m "$(m)"

seed: ## Load sample KB articles and tickets
	python scripts/seed_data.py

smoke: ## End-to-end smoke test against a running stack
	python scripts/smoke_test.py

test: ## Run unit tests
	pytest -v --cov=app --cov-report=term-missing

lint: ## Ruff check
	ruff check app tests

fmt: ## Ruff format + fix
	ruff format app tests && ruff check --fix app tests

typecheck: ## mypy
	mypy app

eval: ## Run the golden-set evaluation
	python -m app.evals.runner

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache htmlcov .coverage
