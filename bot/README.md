# Бот (A5)

Telegram-бот: приглашения, уведомления без текста сообщения, оплата. Задачи 1.4, 3.5, 4.4.

## Где код

Бот — часть сервера FastAPI, отдельного процесса нет (общий `backend/pyproject.toml`: те же
настройки, журналы и фильтры, одна выкладка).

| Файл | Что делает |
| --- | --- |
| `backend/app/bot/webhook.py` | `POST /api/v1/telegram/webhook` — служебный маршрут вне контракта клиента; проверка `X-Telegram-Bot-Api-Secret-Token` за постоянное время (нет — 401, неверный — 403, не настроен — 503) |
| `backend/app/bot/processing.py` | Общий путь обработки одного обновления (вебхук и опрос): идемпотентность по `update_id`, срок обработки 20 с, временные сбои — «не подтверждать» |
| `backend/app/bot/poll.py` | **Только разработка:** опрос `getUpdates` вместо вебхука (бот без публичного сервера) |
| `backend/app/bot/check.py` | Проверка токена: `getMe` — имя бота, признак рабочего токена, состояние вебхука |
| `backend/app/bot/handlers.py` | `/start`, `/start <payload>`, `/help` — ответ с кнопкой `web_app`; прочий текст — подсказка с кнопкой, без повтора текста; только личные чаты |
| `backend/app/bot/dedup.py` | Идемпотентность по `update_id` (в памяти процесса, до 10 000 записей; при нескольких экземплярах — Redis, бэклог Б-26) |
| `backend/app/bot/client.py` | Клиент Bot API на httpx: тайм-ауты, повтор на 429 по `retry_after`, ошибки без токена и без исходного исключения |
| `backend/app/bot/notifications.py` | Каркас уведомлений (ФТ-18, задача 3.5): только фиксированные шаблоны, текст сообщения передать нельзя |
| `backend/app/bot/setup.py` | Кнопка меню Mini App, команды, регистрация вебхука — запускает оператор; `--no-webhook` — без вебхука |
| `backend/tests/test_bot.py` | Тесты без сети (`httpx.MockTransport`) |

## Настройки (окружение, см. `.env.example`)

- `FEELINGS_TELEGRAM_BOT_TOKEN` — токен бота от @BotFather.
- `FEELINGS_TELEGRAM_WEBHOOK_SECRET` — 32–256 символов `A-Z a-z 0-9 _ -`, например
  `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
- `FEELINGS_WEBAPP_URL` — https-адрес Mini App.
- `FEELINGS_BOT_USERNAME` — имя бота без «@» (ссылки-приглашения, задача 1.3).

Без любого из них сервис не стартует.

Утилитам без сервера нужно меньше: `app.bot.check` — только токен; `app.bot.poll` и
`app.bot.setup --no-webhook` — токен и `FEELINGS_WEBAPP_URL` (https). Секрет вебхука, секрет
сессии и БД им не нужны (проверка `check_dev_bot_config`; проверка сервера `check_bot_config`
не ослаблена). Значения берутся из окружения или из `backend/.env`.

## Живая проверка без сервера — режим разработки (оператор)

**Режим разработки; в продуктиве — вебхук.** Опрос `getUpdates` нужен, пока нет публичного
https-сервера (до задачи 0.8). `app.bot.poll` удаляет вебхук бота — запускайте его только для
**тестового** бота. Одновременно с вебхуком опрос не работает (Telegram отвечает 409), два опроса
одного бота — тоже.

> **ВАЖНО: на продуктивном сервере `FEELINGS_ENV` обязателен и не равен `dev`, `test` или `local`
> (например, `FEELINGS_ENV=prod`).** Опрос запускается только при `FEELINGS_ENV` из списка
> `dev`, `test`, `local` (регистр не важен); при любом другом значении (`prod`, `prd`, `live`,
> `staging`, пустое) он отказывает до любого вызова Bot API. Если `FEELINGS_ENV` не задан
> вовсе, действует значение по умолчанию `production` (задача 2.1, этап 2): опрос не стартует,
> поэтому для разработки `FEELINGS_ENV=dev` задаётся явно (`.env.example`).

0. В @BotFather: создать тестового бота; «Configure Mini App» / `/newapp` — указать
   `FEELINGS_WEBAPP_URL` (нужно для ссылок `t.me/<бот>?startapp=<токен>`; на ответ `/start`
   с кнопкой не влияет).
1. Окружение — переменные или `backend/.env` (не коммитится; так токен не попадёт в историю
   оболочки). Токен не вставлять в чат и не передавать аргументом команды:

   ```bash
   cd backend
   export FEELINGS_TELEGRAM_BOT_TOKEN=...          # от @BotFather
   export FEELINGS_WEBAPP_URL=https://<адрес Mini App>
   export FEELINGS_BOT_USERNAME=<имя_бота>          # по желанию: check сверит имя
   ```

2. Токен рабочий: `uv run python -m app.bot.check` → «Токен рабочий: бот @<имя>», ссылка
   `https://t.me/<имя>`, состояние вебхука. Неверный токен — «Токен не работает: … 401»,
   код выхода 1.
3. Команды и кнопка меню без вебхука: `uv run python -m app.bot.setup --no-webhook` →
   «Готово: кнопка меню и команды заданы; вебхук не менялся».
4. Опрос: `uv run python -m app.bot.poll 2>&1 | tee /tmp/bot-poll.log`
   (`--drop-pending` — сбросить накопившиеся обновления). При старте — строка «режим
   разработки … в продуктиве — вебхук», затем `webhook deleted`, `polling getUpdates`.
5. В Telegram, в личном чате с ботом:
   - `/start` → приветствие с кнопкой «Открыть «Чувства AI»» (открывает Mini App);
   - ссылка `https://t.me/<имя>?start=abc` → «Запустить» → тот же ответ с кнопкой;
   - `/help` → то же приветствие с кнопкой, что и `/start`;
   - любой текст (например «проверка_123») → подсказка с кнопкой, текст не повторяется;
   - кнопка меню слева от поля ввода — «Открыть» (Mini App);
   - ссылка-приглашение `https://t.me/<имя>?startapp=<токен>` открывает Mini App (нужен шаг 0).
6. Остановить: Ctrl+C в терминале запуска (или SIGTERM именно процессу python, не `pkill -f` по шаблону — при `uv run` сигнал получат оба процесса и строки остановки не попадут в журнал) → `polling stopped`; `processed updates confirmed` — только если смещение ещё не подтверждено.
   Повторный запуск не отвечает второй раз на уже обработанные сообщения.
7. Журнал: в каждой строке обработки — только `telegram update <id> message: start|help|hint`;
   нет токена, payload и текстов:

   ```bash
   grep -c "$FEELINGS_TELEGRAM_BOT_TOKEN" /tmp/bot-poll.log     # 0
   grep -c "${FEELINGS_TELEGRAM_BOT_TOKEN#*:}" /tmp/bot-poll.log # 0
   grep -cE "abc|проверка_123" /tmp/bot-poll.log                 # 0
   grep -cF "/bot***/" /tmp/bot-poll.log                        # > 0: URL в журнале httpx очищен
   rm /tmp/bot-poll.log
   ```

После проверки вебхук остаётся удалённым — для перехода на сервер выполните шаги ниже.

## Проверка на настоящем боте (оператор)

1. В @BotFather: создать тестового бота, `/newapp` или «Configure Mini App» — указать
   `FEELINGS_WEBAPP_URL` (нужно, чтобы ссылки `t.me/<бот>?startapp=<токен>` открывали Mini App).
2. Заполнить `.env`, поднять сервер на https-домене (задача 0.8).
3. `cd backend && uv run python -m app.bot.setup --webhook-url https://<домен>/api/v1/telegram/webhook`
   — кнопка меню, команды, вебхук с секретом.
4. В Telegram: `/start` и ссылка `t.me/<бот>?start=abc` — ответ с кнопкой «Открыть «Чувства AI»»;
   ссылка-приглашение из Mini App открывает Mini App; в журналах сервера нет токена и payload.
