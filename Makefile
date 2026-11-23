.PHONY: install lint test db-up db-down dev-up dev-down db-roles backend-lint backend-test frontend-lint frontend-test

install:
	cd backend && uv sync --locked
	cd frontend && npm ci

lint: backend-lint frontend-lint
test: backend-test frontend-test

backend-lint:
	cd backend && uv run --locked ruff check . ../evals && uv run --locked ruff format --check . ../evals && uv run --locked mypy app ../evals/run.py ../evals/compare.py ../evals/adapters

backend-test:
	cd backend && uv run --locked pytest -q

frontend-lint:
	cd frontend && npm run lint

frontend-test:
	cd frontend && npm test

db-up:
	docker compose -f infra/docker-compose.yml up -d

db-down:
	docker compose -f infra/docker-compose.yml --profile workers down

# Локальное окружение целиком (нужен .env из .env.example): БД, Redis, миграции, роли входа
# (infra/db/roles.sql) и воркеры памяти и базы знаний. Только для разработки (docs/ADMIN.md).
COMPOSE_DEV = docker compose -f infra/docker-compose.yml --env-file .env --profile workers

dev-up:
	$(COMPOSE_DEV) up -d --build

dev-down:
	$(COMPOSE_DEV) down

# Миграции и роли входа в локальной БД без запуска воркеров (повторный запуск — смена паролей).
db-roles:
	$(COMPOSE_DEV) run --rm --build db-roles
