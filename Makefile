.PHONY: install lint test db-up db-down backend-lint backend-test frontend-lint frontend-test

install:
	cd backend && uv sync
	cd frontend && npm ci

lint: backend-lint frontend-lint
test: backend-test frontend-test

backend-lint:
	cd backend && uv run ruff check . && uv run ruff format --check . && uv run mypy app

backend-test:
	cd backend && uv run pytest -q

frontend-lint:
	cd frontend && npm run lint

frontend-test:
	cd frontend && npm test

db-up:
	docker compose -f infra/docker-compose.yml up -d

db-down:
	docker compose -f infra/docker-compose.yml down
