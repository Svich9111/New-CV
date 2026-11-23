# Инфраструктура (A7)

| Файл | Что |
| --- | --- |
| `docker-compose.yml` | Локальная разработка: PostgreSQL+pgvector и Redis (`make db-up`); профиль `workers` — миграции, роли входа и воркеры памяти и базы знаний (`make dev-up`) |
| `db/init-owner.sql` | Однократно суперпользователем: владелец схемы `feelings_owner` (миграции не под суперпользователем), расширение vector (АР-41) |
| `db/roles.sql` | Роли входа воркера, администратора и API с паролями из окружения (Б-69, Б-75, АР-10); запускается после `alembic upgrade head` |
| `backend.Dockerfile` (+ `.dockerignore`) | Образ сервера: API, воркеры, миграции (локально и на стенде) |
| `deploy/` | Тестовый стенд (0.8): `docker-compose.prod.yml`, `Caddyfile`, `caddy.Dockerfile`, `bootstrap.sh`, `backup.sh`, `restore-check.sh`, `.env.prod.example`, `gen_secrets.py` — инструкция `docs/DEPLOY.md` |
| `initdb/` | Первичная инициализация локального кластера (БД `feelings_test`) |

Инструкция администратора — `docs/ADMIN.md`, выкладка стенда — `docs/DEPLOY.md`. Продуктив — задача 4.7.
