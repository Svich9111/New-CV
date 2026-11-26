# Выкладка тестового стенда (задача 0.8)

> **Стенд — не production.** `FEELINGS_ENV=staging`, отдельный тестовый бот, только выдуманные
> данные и тестовые пользователи. Продуктив — задача 4.7.

Инструкция для оператора. Комплект лежит в `infra/deploy/`. Сервер находится в РФ (152-ФЗ, Д1).
Наружу открыты 80/443, а 22 — только для SSH оператора.

**Только после подтверждения оператора** (RoadMap §4) выполняются:

- миграции;
- регистрация вебхука;
- откат схемы.

Роли БД, ключи и ротация описаны в `docs/ADMIN.md`.

| Файл | Что |
| --- | --- |
| `infra/deploy/bootstrap.sh` | Подготовка Ubuntu 22.04/24.04: Docker из официального репозитория, ufw, пользователь `feelings`, `/opt/feelings` |
| `infra/deploy/docker-compose.prod.yml` | Caddy (HTTPS, Mini App), API, воркеры памяти и базы знаний, PostgreSQL 16 + pgvector, Redis; профиль `ops`: `db-init`, `migrate`, `db-roles` |
| `infra/deploy/Caddyfile`, `caddy.Dockerfile` | Прокси, сертификат, заголовки безопасности, пределы тела, сборка Mini App |
| `infra/backend.Dockerfile` | Образ API и воркеров (пользователь без прав, uid 10001) |
| `infra/deploy/.env.prod.example` | Шаблон переменных: значения пустые, у каждого секрета команда генерации |
| `infra/deploy/gen_secrets.py` | Заполняет пустые секреты и ничего не печатает |
| `infra/db/init-owner.sql` | Однократно суперпользователем: владелец схемы `feelings_owner`, расширение vector |
| `infra/db/roles.sql` | Роли входа API, воркера и администратора |
| `infra/deploy/backup.sh`, `restore-check.sh` | Резервная копия с ротацией 30 дней и проверка восстановления |

Команды ниже выполняются из `/opt/feelings/app` пользователем `feelings`:

```bash
alias dc='docker compose -f infra/deploy/docker-compose.prod.yml'
```

> `dc config`, `docker inspect` и `env` в контейнере показывают значения из `.env` (пароли,
> ключи). Их вывод не прикладывайте к тикетам и чатам. Не запускайте psql с `-a`/`-e`/`-E`.

## 0. До начала (оператор)

1. **Контакты помощи — обязательная проверка (АР-33).** При `staging` API без проверенного
   списка **не стартует**. Это проверено на стенде: `ConfigError: app/help_contacts.json is not
   verified by the operator (АР-33)`, `Application startup failed`. Порядок:
   - внести и проверить линии помощи в `backend/app/help_contacts.json`;
   - поставить `"verified_by_operator": true`;
   - провести изменение через репозиторий (`docs/ADMIN.md` §8).

   Обходить проверку нельзя. `FEELINGS_ENV=dev`/`local`/`test` на стенде не используется: эти
   значения разрешают режим опроса бота, а он удаляет вебхук. Compose это проверяет: точка входа
   API, воркеров и `migrate` при `FEELINGS_ENV`, отличном от `staging`/`production`, завершает
   контейнер с кодом 64 и сообщением `на стенде допустимы только staging и production`.
2. **Тестовый бот** в @BotFather (не продуктивный): токен и имя.
3. **Ключ OpenRouter для стенда.** Без него ответы агента приходят ошибкой, а воркер памяти не
   стартует.

## 1. DNS и порты

| Запись | Значение |
| --- | --- |
| `A стенд.домен` | IPv4 сервера |
| `AAAA стенд.домен` | IPv6 сервера, если есть (иначе не создавать) |
| `CAA домен` (необязательно) | `0 issue "letsencrypt.org"` |

| Порт | Кто слушает | Доступ |
| --- | --- | --- |
| 80/tcp | Caddy | Выдача сертификата Let's Encrypt и переадресация на HTTPS |
| 443/tcp, 443/udp | Caddy | HTTPS и HTTP/3: Mini App, `/api/*`, вебхук бота |
| 22/tcp | sshd | Только оператор (ключ SSH; по возможности ограничить адресами оператора: `ufw allow from <IP> to any port 22`) |
| 5432, 6379, 8000 | PostgreSQL, Redis, API | **Не публикуются**: только внутренние сети Docker (`data` без выхода наружу) |

Перед первым запуском дождитесь, пока `dig +short стенд.домен` вернёт IP сервера.

## 2. Подготовка сервера

```bash
sudo bash bootstrap.sh     # идемпотентно, секретов не содержит
```

Скрипт:

- ставит Docker и compose из официального репозитория;
- настраивает ufw: открыты 22/tcp, 80/tcp, 443/tcp и 443/udp;
- создаёт пользователя `feelings` без пароля, члена группы docker;
- создаёт каталоги `/opt/feelings` (750) и `/opt/feelings/backups` (700).

Затем:

1. Добавьте ключ SSH оператора в `/home/feelings/.ssh/authorized_keys`.
2. Клонируйте репозиторий в `/opt/feelings/app` (ключ развёртывания только на чтение).

Группа docker равносильна root: ключ `feelings` храните как ключ администратора. Вход root по
SSH лучше запретить.

## 3. Секреты

Секреты — только в переменных окружения из `infra/deploy/.env` на сервере (600, владелец
`feelings`). Исключение — пароль суперпользователя БД: он в отдельном файле
`infra/deploy/secrets/postgres_superuser_password` (600), который compose передаёт как docker
secret только `postgres` и `db-init`. Образ postgres задаёт пароль из файла только при первом
создании тома, но сам файл читает при каждом старте (он должен существовать). Пароль нужен
только `db-init` — по сети. Внутри контейнера postgres суперпользователь входит по локальному
сокету без пароля (так работают `backup.sh` и `restore-check.sh`): доступ к Docker на сервере и
так равен доступу суперпользователя БД (группа docker равносильна root, §2). В Git нет ни одного
значения:

- шаблон пустой;
- в compose у обязательных переменных стоит `:?`, и без них он не запускается;
- `backend/app/config.py` по умолчанию держит секреты пустыми, и сервис без них не стартует.

Почему `.env`, а не docker secrets для всего: приложение читает настройки из переменных
окружения (pydantic-settings), а перевод на файлы `*_FILE` — правка backend. Файл-секрет оставлен
там, где образ его уже поддерживает (`POSTGRES_PASSWORD_FILE`) и где важно отделить пароль от
приложения.

```bash
cd /opt/feelings/app
python3 -I infra/deploy/gen_secrets.py      # создаёт .env и secrets/ (600), печатает только имена
# админ-API базы знаний по умолчанию ЗАКРЫТ (FEELINGS_ADMIN_TOKEN_SHA256 пуст); открыть — с флагом:
# python3 -I infra/deploy/gen_secrets.py --admin-token   # токен — в admin-token.txt (600)
nano infra/deploy/.env                       # заполнить значения, отмеченные «вручную» в таблице
```

| Секрет | Владелец | Получение | Кто использует |
| --- | --- | --- | --- |
| Пароль суперпользователя БД (`secrets/postgres_superuser_password`) | Оператор | `gen_secrets.py` или `secrets.token_urlsafe(32)` | `postgres` (первое создание тома), `db-init` |
| `FEELINGS_OWNER_DB_PASSWORD` | Оператор | `secrets.token_urlsafe(32)` | `migrate`, `db-roles` |
| `FEELINGS_API_DB_PASSWORD` | Оператор | `secrets.token_urlsafe(32)` | `api` (роль `feelings_api`) |
| `FEELINGS_WORKER_DB_PASSWORD` | Оператор | `secrets.token_urlsafe(32)` | воркеры (по ценности равен паролю API) |
| `FEELINGS_ADMIN_DB_PASSWORD` | Оператор | `secrets.token_urlsafe(32)` | `api` (админ-API базы знаний) |
| `FEELINGS_TELEGRAM_BOT_TOKEN` | Оператор | @BotFather, вручную | `api` |
| `FEELINGS_TELEGRAM_WEBHOOK_SECRET` | Оператор | `secrets.token_urlsafe(48)` | `api` (проверка вебхука), `app.bot.setup` |
| `FEELINGS_SESSION_SECRET` | Оператор | `secrets.token_urlsafe(48)` | `api` |
| `FEELINGS_ENCRYPTION_KEYS` (+ `_KEY_ID`) | Оператор, резервная копия отдельно от БД | `k1:` + base64url(32 байта) | `api`, `worker-memory` |
| `FEELINGS_MEMORY_FINGERPRINT_KEY` (+ `_OLD`) | Оператор, не удалять никогда (АР-37) | base64url(32 байта) | `worker-memory` |
| `FEELINGS_ADMIN_TOKEN_SHA256` (необязателен; пусто — админ-API закрыт) | Оператор, токен — у администратора | `gen_secrets.py --admin-token`: токен в `admin-token.txt` (передать и удалить), в `.env` только SHA-256 | `api` |
| `OPENROUTER_API_KEY` (обязателен: без него compose не запускается) | Оператор | Кабинет OpenRouter, вручную | `api`, воркеры |

Команды генерации приведены в `.env.prod.example`. Вручную также задаются несекретные значения:

- `FEELINGS_DOMAIN`;
- `FEELINGS_TLS` — почта для Let's Encrypt;
- `FEELINGS_BOT_USERNAME`;
- `FEELINGS_IMAGE_TAG`.

Копию `.env` и `secrets/` сохраните в хранилище секретов отдельно от сервера и от копий БД.

## 4. Установка (первый запуск)

```bash
dc build                                   # образы feelings-backend и feelings-caddy с меткой FEELINGS_IMAGE_TAG
dc up -d postgres redis                    # том pgdata создаётся, суперпользователь — из файла-секрета
dc ps                                      # postgres, redis — healthy

# однократно, суперпользователем: владелец схемы feelings_owner и расширение vector (АР-41)
dc --profile ops run --rm db-init

# ТОЛЬКО по подтверждению оператора: миграции под feelings_owner (не суперпользователь)
dc --profile ops run --rm migrate

# роли входа feelings_api, feelings_worker_login, feelings_admin_login — под feelings_owner
dc --profile ops run --rm db-roles

dc up -d                                   # caddy, api, worker-memory, worker-kb, worker-pipeline, worker-notify
dc ps                                      # все — healthy
curl -fsS https://стенд.домен/api/v1/health    # {"status":"ok"}
```

После `db-init` пароль суперпользователя для работы не нужен. Файл можно перенести в хранилище
секретов: тогда для повторного `db-init` (ротация пароля владельца, восстановление из копии, §10)
его нужно вернуть на место. `dc up` требует, чтобы файл существовал (его читает entrypoint
postgres при каждом старте), — оставьте пустую заглушку с правами 600. Учтите: заглушка не
закрывает суперпользователя — внутри контейнера postgres он входит по сокету без пароля (§3).

`db-init` (`infra/db/init-owner.sql`) кроме владельца схемы и vector:

- создаёт групповые роли `feelings_app`/`feelings_worker`/`feelings_admin`/`feelings_api` от
  имени владельца (как миграция 0001) — нужно для восстановления копии на новый сервер (§10);
- отзывает у PUBLIC права уровня базы `TEMPORARY` и `CREATE` (АР-10) — их не переносит
  `pg_dump`;
- задаёт для всех сеансов владельца `log_statement = none` и `log_min_error_statement = panic`:
  текст запроса с паролем (`db-roles`) не попадёт в журнал PostgreSQL даже при сбое.

**Проверки после запуска:**

- **health:** у всех сервисов `healthy` в `dc ps`:
  - postgres — `pg_isready`;
  - redis — `redis-cli ping`;
  - api — `/api/v1/health`;
  - воркеры — пульс (`python -m app.workers.health`, Б-85): метка последнего успешного
    прохода цикла в `/tmp` контейнера (tmpfs) не старше 60 с (`worker-pipeline`,
    `worker-notify`) или 120 с (`worker-memory`, `worker-kb`), и воркер не на паузе;
  - caddy — админ-интерфейс.
- **Порты:** снаружи (`nmap -Pn стенд.домен`) открыты только 22, 80 и 443; у postgres, redis и
  api вывод `docker port` пуст.
- **Сертификат и заголовки:** `curl -sI https://стенд.домен/` показывает
  `strict-transport-security`, `content-security-policy` и `x-content-type-options`.
- **Причины сбоев:**
  - API не стартует — контакты помощи (шаг 0) или пустой секрет (в журнале имя переменной);
  - **воркер не работает — как отличить причину** (Б-85, Б-90). Воркеры перезапускаются всегда
    (`unless-stopped`, пауза Docker растёт до 1 минуты); причину показывают состояние в `dc ps`,
    код выхода и пульс:

    | `dc ps` | Причина | Что делать |
    | --- | --- | --- |
    | `Restarting (78)` (между перезапусками — `Up` на 1–2 с; растёт `docker inspect -f '{{.RestartCount}}'`) | **ошибка настройки** — код 78 (`EX_CONFIG`): нет или неверна переменная (`OPENROUTER_API_KEY`, `FEELINGS_PIPELINE_PROMPT_SET`, ключи, токен бота), неверное значение настройки, модель отклонила ключ, БД отвергла роль входа (пароль, нет базы, права — SQLSTATE 28P01/28000/3D000/42501) | `dc logs <воркер>`: строка `CRITICAL … configuration error, exit 78 (EX_CONFIG): <класс>: <имя переменной или вид ошибки>` — без значений. Исправить `.env`, затем `dc up -d <воркер>`; сам не пройдёт |
    | `Up (unhealthy)`, пульс `state=waiting` | **временная недоступность при старте** (OpenRouter, сеть/DNS): воркер повторяет в процессе (пауза 1 с · 2ⁿ, до 60 с), не выходит | ждать; в журнале `models_unavailable at start, retry in Ns`. Проходит сама после восстановления сети |
    | `Up (unhealthy)`, пульс `state=paused` | **пауза из-за ошибки настройки во время работы**: модель (`auth`, `quota`, `config`, `model_not_found` — конвейер АР-45 п.2 и память) или токен бота (`http_401`/`http_404` — уведомления). Состояние держится до первого ответа, доказавшего исправную настройку (модель ответила; Telegram принял токен), а не только 5 минут паузы: при пустой очереди `paused` не сменяется на `healthy` сам | исправить ключ/лимит/токен; воркер сам повторяет каждые 5 минут (память — пробным захватом одной задачи). Конвейер и уведомления ждут без расхода попыток; память — не больше одной попытки на задачу за паузу (функции снятия аренды памяти нет), счётчик обнуляется при успехе |
    | `Up (unhealthy)`, пульс `stale` | **цикл не проходит**: БД недоступна (в журнале `… claim failed` / `… pass failed`), цикл завис или все слоты конвейера/уведомлений заняты задачами старше аренды; метка из будущего (часы) — `from the future` | проверить `postgres`; при зависании — `dc restart <воркер>` (аренды вернут работу) |
    | `Restarting (1)` и др. | непредвиденный сбой процесса | `dc logs <воркер>` — трассировка; перезапуск нужен и идёт |

    Пульс смотреть так: `docker inspect --format '{{json .State.Health.Log}}' feelings-worker-notify-1`
    (вывод health check: `unhealthy: state=paused reason=http_401 age=3s`) или
    `dc exec worker-notify python -m app.workers.health`. В файле пульса
    (`/tmp/feelings-worker-heartbeat.json` в контейнере) — только время, состояние и вид причины:
    ни текстов, ни id, ни адресов.
  - уведомления в Telegram не приходят — `dc logs worker-notify`. Неверный токен бота видно
    уже при старте: на стенде и в продуктиве воркер проверяет его запросом `getMe` (параллельно с
    циклом; в разработке — только с `FEELINGS_NOTIFY_CHECK_TOKEN=true`) — 401/404 →
    `Restarting (78)` и строка `CRITICAL … Telegram rejected FEELINGS_TELEGRAM_BOT_TOKEN (HTTP 401)`;
    Telegram недоступен при старте — `bot token check skipped: network`, воркер работает, токен
    проверит первая отправка. Токен, отозванный во время работы, виден как строки уровня ERROR
    `notify message: status=paused reason=http_401` (или `http_404`) и `unhealthy` в `dc ps`
    (пульс `paused` — до первого ответа Telegram, принявшего токен): воркер встаёт на паузу на
    5 минут и повторяет; уведомления ждут (попытки не расходуются) и уйдут после исправления. Исправить токен в `.env`, затем
    `dc up -d worker-notify`; отсрочка хранится в БД и переживает перезапуск — накопившиеся
    уведомления уйдут в течение ~5 минут после исправления, а не сразу. Пауза флуд-лимита Telegram
    (429) — не сбой: `healthy`, пульс `reason=http_429`; долгий лимит из коротких 429 попытки не
    расходует (Б-91). Прочие исходы в журнале — без текстов и адресатов: `status=blocked` (бот
    заблокирован получателем — штатно), `status=retry` (сеть, 5xx, 429 — повтор).
  - **остановка** (`dc stop`, выкладка) штатная, в журнале — `SIGTERM received` и
    `<воркер> stopped`:
    - `worker-notify` (`stop_grace_period: 30s`): начатая отправка дорабатывается и отмечается;
      неотправленное (ждало повтора или конца паузы 429) — аренда снимается сразу без расхода
      попытки (Б-88), новый контейнер берёт его без ожидания конца аренды;
    - `worker-pipeline` (`stop_grace_period: 200s`, Б-88): начатые сообщения дорабатываются до
      записи исхода — каждое ограничено тайм-аутом модели (аренда − 30 с) и окном записи
      (аренда − 15 с), аренда по умолчанию 180 с. Остановка может занять до ~3 минут, если модель
      отвечает медленно; без этого срока Docker убил бы процесс через 10 с, и сообщение
      обработалось бы повторно после аренды. Меняете `FEELINGS_PIPELINE_LEASE_S` — меняйте и
      `stop_grace_period`;
    - `worker-memory`, `worker-kb` — останавливаются между проходами; начатая задача, не
      завершённая за 10 с, повторяется после истечения аренды.
  - **После каждой выкладки и перезагрузки сервера** — `dc ps`: все сервисы `healthy`, ни одного
    `Restarting`/`Exited`. Иначе отправленные сообщения остаются «в обработке» (`worker-pipeline`).

## 5. Вебхук бота — по подтверждению оператора

Бот работает по вебхуку на том же API: `POST /api/v1/telegram/webhook`, подлинность проверяется
заголовком `X-Telegram-Bot-Api-Secret-Token`. Регистрация выполняется внутри контейнера API:
секреты берутся из его окружения и не печатаются.

```bash
dc exec api python -m app.bot.setup --webhook-url https://стенд.домен/api/v1/telegram/webhook
```

Команда задаёт кнопку меню Mini App (`https://стенд.домен/`), команды бота и вебхук. Проверка:
`/start` у бота отвечает кнопкой Mini App. Режим опроса (`app.bot.poll`) при `staging` отказывает:
«for development only».

## 6. Обновление

```bash
cd /opt/feelings/app
infra/deploy/backup.sh                      # копия перед обновлением — всегда
git fetch && git checkout <коммит или тег>
# в .env: FEELINGS_IMAGE_TAG=<короткий хеш коммита> — прежние образы остаются для отката
dc build
# есть новые миграции? — ТОЛЬКО по подтверждению оператора:
dc --profile ops run --rm migrate
dc --profile ops run --rm db-roles          # если менялся infra/db/roles.sql
dc up -d
curl -fsS https://стенд.домен/api/v1/health
```

## 7. Откат

- **Код, без изменений схемы.** В `.env` верните прежнюю метку `FEELINGS_IMAGE_TAG`, выполните
  `git checkout <прежний коммит>` и `dc up -d`. Образы с прежней меткой уже есть локально.
- **Схема менялась — только по подтверждению оператора.**
  1. `dc stop api worker-memory worker-kb worker-pipeline worker-notify`.
  2. `infra/deploy/backup.sh`.
  3. `dc --profile ops run --rm migrate alembic downgrade <ревизия>` (под `feelings_owner`;
     образ — ещё новый: в нём есть шаги отката новых миграций).
  4. `git checkout <прежний коммит>` — вместе с кодом возвращаются compose-файл, Caddyfile и
     `infra/db/*.sql` прежней версии.
  5. В `.env` — прежняя метка `FEELINGS_IMAGE_TAG`, затем `dc up -d`.
  6. `curl -fsS https://стенд.домен/api/v1/health`.

  Если downgrade невозможен — восстановление на том же сервере из копии, снятой перед
  обновлением (§10, «Восстановление на том же сервере»), после шага 4.
- **Вебхук** при откате не трогается.

## 8. Постоянное хранение

| Том | Что | Потеря |
| --- | --- | --- |
| `feelings_pgdata` | База PostgreSQL | Данные стенда: восстанавливать из копии |
| `feelings_caddy_data` | Сертификаты и ключи Caddy | Новый сертификат при запуске (ограничения Let's Encrypt на частоту) |
| `feelings_caddy_config` | Состояние Caddy | Несущественна |

Redis хранит только дневные счётчики, на диск ничего не пишет. `dc down` тома не удаляет; **не
запускайте `dc down -v`** на стенде.

## 9. Ротация паролей и ключей

Описана в `docs/ADMIN.md`:

- пароли ролей — §3: новый пароль в `.env`, `dc --profile ops run --rm db-roles`, `dc up -d`;
- ключи шифрования и отпечатков — §6;
- токены администратора — §7;
- пароль владельца схемы — повторный `db-init` (нужен файл-секрет суперпользователя).

## 10. Резервное копирование и восстановление

Требование ТЗ §10: ежедневно, хранение 30 дней, проверка восстановления раз в квартал. ТЗ §7:
удалённое пользователем уходит и из копий в течение 30 дней.

```bash
infra/deploy/backup.sh                      # pg_dump -Fc → /opt/feelings/backups/feelings-ДАТА-ВРЕМЯ-НС.dump (600)
infra/deploy/restore-check.sh               # свежая копия → временная база → проверки → удаление
infra/deploy/restore-check.sh --db feelings # только проверки рабочей базы (после восстановления)
```

Расписание (`crontab -e` пользователя `feelings`; время UTC):

```cron
17 2 * * *  cd /opt/feelings/app && infra/deploy/backup.sh >> /opt/feelings/backups/backup.log 2>&1
43 3 1 1,4,7,10 *  cd /opt/feelings/app && infra/deploy/restore-check.sh >> /opt/feelings/backups/restore-check.log 2>&1
```

Ротация журналов cron (в них только имена файлов, имена таблиц и числа строк) —
`/etc/logrotate.d/feelings-backup` (root, один раз):

```
/opt/feelings/backups/backup.log /opt/feelings/backups/restore-check.log {
    su feelings feelings
    monthly
    rotate 3
    compress
    missingok
    notifempty
    create 600 feelings feelings
}
```

Проверка: `sudo logrotate -d /etc/logrotate.d/feelings-backup`.

**`backup.sh`:**

- один запуск за раз: `flock` на `backups/.backup.lock`; ручной запуск перед обновлением ждёт
  копию из cron до 30 минут;
- имя уникально (время до наносекунд); существующий файл не перезаписывается;
- пишет копию атомарно (через `.partial`) и проверяет, что архив читается (`pg_restore --list`);
- срок хранения `FEELINGS_BACKUP_KEEP_DAYS` (по умолчанию 30; допустимо целое 2…30, иначе
  отказ): копия удаляется на запуске, когда ей больше (KEEP_DAYS − 1) суток без 2 часов. При
  ежедневном запуске ни одна копия не живёт 30 суток (ТЗ §7) — с запасом на сдвиг времени
  запуска. Если сервер был выключен, просроченное удаляется при следующем запуске;
- печатает только имена файлов.

**`restore-check.sh`** восстанавливает копию тем же порядком, что и настоящее восстановление:
пустая временная база `feelings_restore_check_<время>` → `infra/db/init-owner.sql` (с
`keep_owner_password=1`: пароль владельца не меняется) → `pg_restore --exit-on-error`. Проверяет:

- версию схемы, расширение vector, политики RLS, число строк таблиц (печатает только имена и
  числа);
- права уровня базы: ни PUBLIC, ни `feelings_app`/`feelings_api`/`feelings_worker`/
  `feelings_admin` не могут создавать временные таблицы и схемы (АР-10); `pg_dump` без
  `--create` этих прав не переносит — их возвращает `init-owner.sql`;
- функции vector не исполняют PUBLIC и роли приложения (АР-35);
- объекты приложения во владении `feelings_owner`; `feelings_api` — член только `feelings_app`.

Временная база удаляется в любом случае; рабочую базу проверка не трогает.

Важно:

- Копии лежат на том же сервере, то есть защищают от ошибок, но не от потери сервера. Копирование
  в отдельное хранилище в РФ — задача 4.7. До неё оператор может переносить копии вручную
  (`scp`), не дольше 30 дней.
- Тексты в копии зашифрованы ключами приложения. Без ключей из `.env` копия бесполезна, а с ними
  содержит личные данные: храните отдельно и с правами 600.
- Роли кластера и права уровня базы в `pg_dump` не входят. Их создаёт и задаёт `db-init`
  (`init-owner.sql`), поэтому он выполняется **до** `pg_restore` в обоих сценариях ниже.

### Восстановление на том же сервере — только по подтверждению оператора

Например, когда откат схемы (§7) невозможен. Рабочая база удаляется и создаётся заново.

```bash
cd /opt/feelings/app
git checkout <коммит, с которого снята копия>   # версия схемы копии = версии кода (alembic_version)
# в .env — метка образов этого коммита (FEELINGS_IMAGE_TAG); при необходимости dc build
DUMP=/opt/feelings/backups/feelings-<дата>.dump
dc stop caddy api worker-memory worker-kb worker-pipeline worker-notify
infra/deploy/backup.sh                      # копия текущего состояния — на случай ошибки
dc exec -T postgres psql -X -U postgres -d postgres -v ON_ERROR_STOP=1 \
  -c 'DROP DATABASE feelings WITH (FORCE)' -c 'CREATE DATABASE feelings'
# нужен файл-секрет суперпользователя (§4): вернуть из хранилища, если там заглушка
dc --profile ops run --rm db-init           # владелец, групповые роли, права базы, vector
dc exec -T postgres pg_restore -U postgres -d feelings --exit-on-error < "$DUMP"
dc --profile ops run --rm db-roles          # роли входа (пароли из .env)
infra/deploy/restore-check.sh --db feelings # права базы, vector, владельцы, RLS
dc up -d
curl -fsS https://стенд.домен/api/v1/health
```

`DROP DATABASE` удаляет и настройки владельца в этой базе (`session_preload_libraries`) — их
возвращает `db-init`.

### Восстановление на новый сервер — только по подтверждению оператора

Нужны: копия (`.dump`), `.env` и `secrets/` из хранилища секретов (те же пароли ролей и **те же
ключи шифрования** — иначе тексты не расшифровать).

```bash
# §1–2: DNS на новый IP, bootstrap.sh, клон репозитория в /opt/feelings/app
cd /opt/feelings/app
git checkout <коммит, с которого снята копия>
install -m 600 <из хранилища>/.env infra/deploy/.env
install -m 700 -d infra/deploy/secrets
install -m 600 <из хранилища>/postgres_superuser_password infra/deploy/secrets/
DUMP=/opt/feelings/backups/feelings-<дата>.dump  # скопировать на сервер (scp), права 600
dc build
dc up -d postgres redis                     # новый том; суперпользователь — из файла-секрета
dc --profile ops run --rm db-init           # владелец, групповые роли, права базы, vector
dc exec -T postgres pg_restore -U postgres -d feelings --exit-on-error < "$DUMP"
dc --profile ops run --rm db-roles          # роли входа
infra/deploy/restore-check.sh --db feelings
dc up -d
curl -fsS https://стенд.домен/api/v1/health
```

Вебхук бота указывает на домен, а не на IP: после смены A-записи повторная регистрация не нужна;
при смене домена — §5 (по подтверждению).

## 11. Локальная проверка комплекта (без сервера)

Так комплект проверялся. На стенде эти шаги не выполняются.

1. Генерация секретов во временный каталог: `python3 -I infra/deploy/gen_secrets.py --dir $TMP`.
2. В `$TMP/.env` задать:
   - `FEELINGS_DOMAIN=localhost`;
   - `FEELINGS_TLS=internal` — собственный центр Caddy, без публичного ACME;
   - выдуманный токен бота формата Telegram — нужен только чтобы API прошёл проверку настроек;
   - `FEELINGS_ENV=staging` (другие значения compose не пропустит);
   - выдуманный `OPENROUTER_API_KEY` (обязателен для compose; настоящий не нужен);
   - `FEELINGS_SECRETS_DIR=$TMP/secrets`.
3. Список контактов помощи в репозитории не проверен, поэтому API при `staging` не стартует
   (АР-33) — это проверяется первым. Для остальной проверки — только в `$TMP`, не в репозитории:
   копия `backend/app/help_contacts.json` с `"verified_by_operator": true` и файл
   `$TMP/override.yml`, который подключает её к `api` томом только на чтение
   (`$TMP/help_contacts.json:/srv/backend/app/help_contacts.json:ro`).
4. Запуск:
   `docker compose -f infra/deploy/docker-compose.prod.yml -f $TMP/override.yml --env-file $TMP/.env …`,
   затем `db-init` → `migrate` → `db-roles` → `up -d`.
5. Проверка: `curl -k https://localhost/api/v1/health`; `backup.sh` и `restore-check.sh` с
   `FEELINGS_BACKUP_DIR=$TMP/backups` и `FEELINGS_DEPLOY_ENV_FILE=$TMP/.env`.
6. Уборка: `down -v` и удаление `$TMP`.

За прокси с подменой TLS образы собираются так:

```bash
docker build --network host --secret id=build_ca,src=<CA> --build-arg HTTPS_PROXY=… -f …
```

Секрет `build_ca` используется только при сборке и в образ не попадает. На сервере стенда он не
нужен.

## 12. Запрещено на стенде

- Подключать продуктивного бота или реальную переписку.
- Публиковать порты БД, Redis и API. `ports:` есть только у `caddy`.
- Обходить проверку контактов помощи (в том числе подменой `help_contacts.json` томом, как в
  локальной проверке §11). Ставить `FEELINGS_ENV=dev`/`local`/`test` — compose откажет.
- Включать журнал доступа Caddy или убирать фильтр журнала ошибок в `Caddyfile`: в пути
  `/invites/<токен>` есть токен приглашения, в заголовках — секрет вебхука.
- Запускать несколько процессов API. Защита от повторной доставки обновлений бота и фоновые
  задачи живут в памяти процесса.
- Выполнять `dc down -v`. Запускать миграции без подтверждения оператора.
