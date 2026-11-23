# Образ сервера: API (uvicorn), фоновые воркеры памяти и базы знаний, миграции. Используется
# локально (infra/docker-compose.yml, профиль `workers`) и на тестовом стенде
# (infra/deploy/docker-compose.prod.yml, задача 0.8). Продуктив — задача 4.7: базовые образы
# закрепить по дайджесту (Б-82).
# Контекст сборки — корень репозитория (нужны backend/ и prompts/); что не попадает в контекст —
# backend.Dockerfile.dockerignore (в том числе .env: секреты в образ не кладутся).
FROM python:3.12-slim

# Необязательный корневой сертификат для сборки за прокси с подменой TLS (только сборка:
# BuildKit-секрет `build_ca`, в образ не попадает). На сервере стенда не нужен.
# uv — из PyPI (тот же реестр, что и зависимости; ghcr.io не нужен), версия закреплена.
RUN --mount=type=secret,id=build_ca,required=false \
    if [ -s /run/secrets/build_ca ]; then export PIP_CERT=/run/secrets/build_ca; fi; \
    pip install --no-cache-dir uv==0.11.32

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

WORKDIR /srv/backend
# Зависимости — отдельным слоем, строго по uv.lock, без инструментов разработки.
COPY backend/pyproject.toml backend/uv.lock ./
RUN --mount=type=secret,id=build_ca,required=false \
    if [ -s /run/secrets/build_ca ]; then export SSL_CERT_FILE=/run/secrets/build_ca; fi; \
    uv sync --locked --no-dev --no-install-project

# Код и промпты: app.ai.prompts ищет каталог prompts/ рядом с backend/ (/srv/prompts).
COPY backend/alembic.ini ./
COPY backend/migrations ./migrations
COPY backend/app ./app
COPY prompts /srv/prompts

# Без прав суперпользователя внутри контейнера.
RUN useradd --system --uid 10001 --no-create-home feelings
USER feelings

CMD ["python", "-m", "app.workers.memory"]
