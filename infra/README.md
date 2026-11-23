# Инфраструктура (A7)

| Файл | Что |
| --- | --- |
| `docker-compose.yml` | Локальная разработка: PostgreSQL+pgvector и Redis (`make db-up`); профиль `workers` — миграции, роли входа и воркеры памяти и базы знаний (`make dev-up`) |
| `db/init-owner.sql` | Однократно суперпользователем (и перед восстановлением копии): владелец схемы `feelings_owner` (миграции не под суперпользователем), групповые роли, права уровня базы, расширение vector (АР-41); автотест — `backend/tests/test_owner_migrations.py` |
| `db/roles.sql` | Роли входа воркера, администратора и API с паролями из окружения (Б-69, Б-75, АР-10); запускается после `alembic upgrade head` |
| `backend.Dockerfile` (+ `.dockerignore`) | Образ сервера: API, воркеры, миграции (локально и на стенде) |
| `deploy/` | Тестовый стенд (0.8): `docker-compose.prod.yml`, `Caddyfile`, `caddy.Dockerfile`, `bootstrap.sh`, `backup.sh`, `restore-check.sh`, `.env.prod.example`, `gen_secrets.py` — инструкция `docs/DEPLOY.md` |
| `initdb/` | Первичная инициализация локального кластера (БД `feelings_test`) |

Инструкция администратора — `docs/ADMIN.md`, выкладка стенда — `docs/DEPLOY.md`. Продуктив — задача 4.7.

## Версия PostgreSQL и pgvector

Одна закреплённая версия образа — `pgvector/pgvector:0.8.0-pg16` — в CI (`.github/workflows/ci.yml`),
на стенде (`deploy/docker-compose.prod.yml`) и в локальном `docker-compose.yml`: проверки прав функций
расширения vector (АР-35; `test_owner_migrations.py`, `test_rls_a8.py`) идут на той же версии, что и
стенд (ревью 0.8, MINOR-11). Исключение — тестовый кластер облачной сессии агентов
(`/var/tmp/feelings-pgdata`, `/var/tmp/feelings-ci-scram`): pgvector из пакетов системы (0.6.0, меньше
функций расширения). Тесты не зависят от числа функций — проверяют, что ни одна не исполнима для
PUBLIC и ролей приложения. Обновление версии — одной правкой во всех трёх файлах, с прогоном CI;
закрепление по дайджесту — Б-82 (задача 4.7).
