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

   Обходить проверку нельзя. `FEELINGS_ENV=dev`/`local` на стенде не используется: эти значения
   разрешают режим опроса бота, а он удаляет вебхук.
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
secret только `postgres` (при первом создании тома) и `db-init`. В Git нет ни одного значения:

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
| `FEELINGS_ADMIN_TOKEN_SHA256` | Оператор, токен — у администратора | `gen_secrets.py`: токен в `admin-token.txt` (передать и удалить), в `.env` только SHA-256 | `api` |
| `OPENROUTER_API_KEY` | Оператор | Кабинет OpenRouter, вручную | `api`, воркеры |

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

dc up -d                                   # caddy, api, worker-memory, worker-kb
dc ps                                      # все — healthy
curl -fsS https://стенд.домен/api/v1/health    # {"status":"ok"}
```

После `db-init` пароль суперпользователя для работы не нужен. Файл можно перенести в хранилище
секретов: тогда для повторного `db-init` его нужно вернуть на место, а `dc up` требует, чтобы файл
существовал, — оставьте пустую заглушку с правами 600.

**Проверки после запуска:**

- **health:** у всех сервисов `healthy` в `dc ps`:
  - postgres — `pg_isready`;
  - redis — `redis-cli ping`;
  - api — `/api/v1/health`;
  - воркеры — процесс жив и роль входа подключается к БД;
  - caddy — админ-интерфейс.
- **Порты:** снаружи (`nmap -Pn стенд.домен`) открыты только 22, 80 и 443; у postgres, redis и
  api вывод `docker port` пуст.
- **Сертификат и заголовки:** `curl -sI https://стенд.домен/` показывает
  `strict-transport-security`, `content-security-policy` и `x-content-type-options`.
- **Причины сбоев:**
  - API не стартует — контакты помощи (шаг 0) или пустой секрет (в журнале имя переменной);
  - воркер памяти остановился после пяти попыток — `OPENROUTER_API_KEY` (`dc logs worker-memory`).

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
  1. `dc stop api worker-memory worker-kb`.
  2. `infra/deploy/backup.sh`.
  3. `dc --profile ops run --rm migrate alembic downgrade <ревизия>` (под `feelings_owner`).
  4. Прежние образы, затем `dc up -d`.

  Если downgrade невозможен — восстановление из копии, снятой перед обновлением (раздел 10).
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

## 10. Резервное копирование и проверка восстановления

Требование ТЗ §10: ежедневно, хранение 30 дней, проверка восстановления раз в квартал.

```bash
infra/deploy/backup.sh          # pg_dump -Fc → /opt/feelings/backups/feelings-ДАТА.dump (600), удаление старше 30 дней
infra/deploy/restore-check.sh   # свежая копия → временная база → проверки → временная база удаляется
```

Расписание (`crontab -e` пользователя `feelings`; время UTC):

```cron
17 2 * * *  cd /opt/feelings/app && infra/deploy/backup.sh >> /opt/feelings/backups/backup.log 2>&1
43 3 1 1,4,7,10 *  cd /opt/feelings/app && infra/deploy/restore-check.sh >> /opt/feelings/backups/restore-check.log 2>&1
```

**`backup.sh`:**

- проверяет, что архив читается (`pg_restore --list`);
- пишет копию атомарно (через `.partial`);
- печатает только имена файлов.

**`restore-check.sh`:**

- восстанавливает копию в `feelings_restore_check_<время>` с `--exit-on-error`;
- проверяет версию схемы, расширение vector и политики RLS;
- печатает имена таблиц и число строк — без содержимого;
- удаляет временную базу в любом случае;
- рабочую базу не трогает.

Важно:

- Копии лежат на том же сервере, то есть защищают от ошибок, но не от потери сервера. Копирование
  в отдельное хранилище в РФ — задача 4.7. До неё оператор может переносить копии вручную
  (`scp`), не дольше 30 дней.
- Тексты в копии зашифрованы ключами приложения. Без ключей из `.env` копия бесполезна, а с ними
  содержит личные данные: храните отдельно и с правами 600.
- Роли кластера в `pg_dump` не входят. При восстановлении на новый сервер порядок такой:
  1. `dc up -d postgres`;
  2. `db-init`;
  3. `pg_restore` (по подтверждению);
  4. `db-roles`.

## 11. Локальная проверка комплекта (без сервера)

Так комплект проверялся. На стенде эти шаги не выполняются.

1. Генерация секретов во временный каталог: `python3 -I infra/deploy/gen_secrets.py --dir $TMP`.
2. В `$TMP/.env` задать:
   - `FEELINGS_DOMAIN=localhost`;
   - `FEELINGS_TLS=internal` — собственный центр Caddy, без публичного ACME;
   - выдуманный токен бота формата Telegram — нужен только чтобы API прошёл проверку настроек;
   - `FEELINGS_ENV=local`, потому что список контактов не проверен; отказ при `staging` проверяется
     отдельно;
   - `FEELINGS_SECRETS_DIR=$TMP/secrets`.
3. Запуск:
   `docker compose -f infra/deploy/docker-compose.prod.yml --env-file $TMP/.env …`, затем
   `db-init` → `migrate` → `db-roles` → `up -d`.
4. Проверка: `curl -k https://localhost/api/v1/health`.
5. Уборка: `down -v` и удаление `$TMP`.

За прокси с подменой TLS образы собираются так:

```bash
docker build --network host --secret id=build_ca,src=<CA> --build-arg HTTPS_PROXY=… -f …
```

Секрет `build_ca` используется только при сборке и в образ не попадает. На сервере стенда он не
нужен.

## 12. Запрещено на стенде

- Подключать продуктивного бота или реальную переписку.
- Публиковать порты БД, Redis и API. `ports:` есть только у `caddy`.
- Обходить проверку контактов помощи. Ставить `FEELINGS_ENV=dev`/`local`.
- Включать журнал доступа Caddy: в пути `/invites/<токен>` есть токен приглашения.
- Запускать несколько процессов API. Защита от повторной доставки обновлений бота и фоновые
  задачи живут в памяти процесса.
- Выполнять `dc down -v`. Запускать миграции без подтверждения оператора.
