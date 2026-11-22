# Образ сервера для ЛОКАЛЬНОЙ разработки: фоновые воркеры памяти и базы знаний и миграции
# (infra/docker-compose.yml, профиль `workers`). Продуктивный образ — задача 4.7.
# Контекст сборки — корень репозитория (нужны backend/ и prompts/); что не попадает в контекст —
# backend.Dockerfile.dockerignore (в том числе .env: секреты в образ не кладутся).
FROM python:3.12-slim

COPY --from=ghcr.io/astral-sh/uv:0.11.32 /uv /usr/local/bin/uv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

WORKDIR /srv/backend
# Зависимости — отдельным слоем, строго по uv.lock, без инструментов разработки.
COPY backend/pyproject.toml backend/uv.lock ./
RUN uv sync --locked --no-dev --no-install-project

# Код и промпты: app.ai.prompts ищет каталог prompts/ рядом с backend/ (/srv/prompts).
COPY backend/alembic.ini ./
COPY backend/migrations ./migrations
COPY backend/app ./app
COPY prompts /srv/prompts

# Без прав суперпользователя внутри контейнера.
RUN useradd --system --uid 10001 --no-create-home feelings
USER feelings

CMD ["python", "-m", "app.workers.memory"]
