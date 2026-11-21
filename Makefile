.PHONY: install lint test db-up db-down backend-lint backend-test frontend-lint frontend-test

install:
	cd backend && uv sync --locked
	cd frontend && npm ci

lint: backend-lint frontend-lint
test: backend-test frontend-test

backend-lint:
	cd backend && uv run --locked ruff check . ../evals && uv run --locked ruff format --check . ../evals && uv run --locked mypy app ../evals/run.py

backend-test:
	cd backend && uv run --locked pytest -q

frontend-lint:
	cd frontend && npm run lint

frontend-test:
	cd frontend && npm test

db-up:
	docker compose -f infra/docker-compose.yml up -d

db-down:
	docker compose -f infra/docker-compose.yml down
