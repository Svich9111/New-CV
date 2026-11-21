# Бот (A5)

Telegram-бот: приглашения, уведомления без текста сообщения, оплата. Задачи 1.4, 3.5, 4.4.

## Где код

Бот — часть сервера FastAPI, отдельного процесса нет (общий `backend/pyproject.toml`: те же
настройки, журналы и фильтры, одна выкладка).

| Файл | Что делает |
| --- | --- |
| `backend/app/bot/webhook.py` | `POST /api/v1/telegram/webhook` — служебный маршрут вне контракта клиента; проверка `X-Telegram-Bot-Api-Secret-Token` за постоянное время (нет — 401, неверный — 403, не настроен — 503) |
| `backend/app/bot/handlers.py` | `/start`, `/start <payload>`, `/help` — ответ с кнопкой `web_app`; прочий текст — подсказка с кнопкой, без повтора текста; только личные чаты |
| `backend/app/bot/dedup.py` | Идемпотентность по `update_id` (в памяти процесса, до 10 000 записей; при нескольких экземплярах — Redis, бэклог Б-26) |
| `backend/app/bot/client.py` | Клиент Bot API на httpx: тайм-ауты, повтор на 429 по `retry_after`, ошибки без токена и без исходного исключения |
| `backend/app/bot/notifications.py` | Каркас уведомлений (ФТ-18, задача 3.5): только фиксированные шаблоны, текст сообщения передать нельзя |
| `backend/app/bot/setup.py` | Кнопка меню Mini App, команды, регистрация вебхука — запускает оператор |
| `backend/tests/test_bot.py` | Тесты без сети (`httpx.MockTransport`) |

## Настройки (окружение, см. `.env.example`)

- `FEELINGS_TELEGRAM_BOT_TOKEN` — токен бота от @BotFather.
- `FEELINGS_TELEGRAM_WEBHOOK_SECRET` — 32–256 символов `A-Z a-z 0-9 _ -`, например
  `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
- `FEELINGS_WEBAPP_URL` — https-адрес Mini App.
- `FEELINGS_BOT_USERNAME` — имя бота без «@» (ссылки-приглашения, задача 1.3).

Без любого из них сервис не стартует.

## Проверка на настоящем боте (оператор)

1. В @BotFather: создать тестового бота, `/newapp` или «Configure Mini App» — указать
   `FEELINGS_WEBAPP_URL` (нужно, чтобы ссылки `t.me/<бот>?startapp=<токен>` открывали Mini App).
2. Заполнить `.env`, поднять сервер на https-домене (задача 0.8).
3. `cd backend && uv run python -m app.bot.setup --webhook-url https://<домен>/api/v1/telegram/webhook`
   — кнопка меню, команды, вебхук с секретом.
4. В Telegram: `/start` и ссылка `t.me/<бот>?start=abc` — ответ с кнопкой «Открыть «Чувства AI»»;
   ссылка-приглашение из Mini App открывает Mini App; в журналах сервера нет токена и payload.
