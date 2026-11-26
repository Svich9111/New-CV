# Ревью A6, раунд 2: надёжность воркеров (Б-85/88/90/91), Б-83, Б-84

Ветка `claude/nifty-maxwell-7geamm`, коммит `ee8a5ce` (дифф `397a4b2..ee8a5ce -- backend docs`).
Основание: отчёт р.1 `docs/reviews/workers-b83-b84-review-r1.md`, тест р.1 `docs/reviews/workers-b83-b84-test-r1.md`,
DECISIONS АР-49 п.5, BACKLOG Б-92. Источники: ТЗ §2, §9.1 п.5, §10; CLAUDE.md.

## Прогоны

| Кластер | Набор | Итог |
| --- | --- | --- |
| 5432 (`trust`) | `test_worker_health test_workers_a8 test_pipeline_helpers test_notify_worker test_pipeline_worker test_deploy_a8 test_feed_withheld test_drafts_api` | **326 passed, 2 skipped**: оба пропуска — `password_auth` («кластер без проверки пароля (trust)») |
| 5432, `CI=true` | `test_worker_health -k "rejected or wrong_password or wrong_db_password"` | 8 passed, **2 errors** (`pytest.fail` в фикстуре), как и требовалось |
| 5433 (scram) | тот же набор | **328 passed**, пропусков нет |
| 5432 и 5433 | `test_workers_a8.py` + `test_pipeline_helpers.py` **в версии коммита** (отдельный worktree; в рабочем дереве A8 сейчас правит `test_workers_a8.py`) | 30/31 passed. Два падения на 5433 (`test_stop_releases_every_unsent_lease`: `(6, 3) == (3, 0)`; `UniqueViolation uq_users_telegram_id`) случились, пока на том же кластере шёл параллельный pytest A8. В отдельном прогоне повторить их не удалось: 3/3 и 2/2 passed. Это известное ограничение общей БД, которое теперь записано в docstring помощника |
| ruff / ruff format / mypy (`app`) | — | чисто. В рабочем дереве у незакоммиченной правки A8 есть F401 `_password_checked` в `test_workers_a8.py:66`: это не `ee8a5ce`, но перед коммитом A8 надо поправить |

## Сверка с замечаниями р.1 и ТЗ

| Замечание / требование | Где | Статус |
| --- | --- | --- |
| **MAJOR** р.1: тесты отказа входа красные на trust | `test_worker_health.py`. Несуществующая роль и нет базы (3D000) проверяются на любом кластере (`test_rejected_worker_login_is_config_error_not_retry[no_such_role/no_such_db]`, `test_process_rejected_db_login_exits_78`). Неверный пароль (28P01) вынесен в `test_wrong_password_login_is_config_error` и `test_process_wrong_db_password_exits_78` с фикстурой `password_auth`: на trust это skip, при `CI=true` fail. Ожидаемый SQLSTATE для «роли нет» берётся из пробного входа (`_role_code`). В CI (`.github/workflows`, `POSTGRES_PASSWORD`, вход по TCP) — scram, там пропуска не будет | ✅ |
| MINOR-1: memory/kb после SIGTERM | `memory.py` `run_once(stop)`: проверка перед захватом, между задачами пачки и перед сжатием. `kb.py` — перед захватом и между материалами. Тесты `test_memory_stop_between_extraction_and_compaction`, `test_memory_stopped_before_pass_claims_nothing`, `test_kb_stop_between_documents` | ✅ |
| MINOR-2: `paused` не должен становиться `ok` при пустой очереди | pipeline `_pause_kind` держится до `_resume()` (успех или `LLMRefusal`). notify `_config_reason` держится до `_token_accepted()` (отправлено, 400, 403). memory `_pause_kind` держится до `_model_answered`. Тесты `test_pipeline_paused_with_empty_queue_stays_unhealthy`, `…holds_after_pause_until_model_answers`, `test_notify_paused_holds_after_pause_until_token_accepted` | ✅ (см. MINOR-A) |
| MINOR-2: getMe при старте notify | `notify.py` `check_bot_token` и `run_with_token_check`; тесты 401/404, сеть/502/429, тайм-аут, остановка цикла при 401, работа цикла при недоступном Telegram | ✅ |
| MINOR-3: пульс при занятых слотах | `health.any_within`; pipeline и notify `run_forever`: `elif self.paused or any_within(...)`. Тест `test_busy_slots_pulse_only_while_a_task_is_within_lease[pipeline/notify]` | ✅ |
| MINOR-4: notify без пульса при падающем захвате; SIGTERM memory/kb | `test_notify_no_pulse_while_database_unavailable`, `test_memory_kb_sigterm_exit_0_and_logged` (×4, плюс строка `worker stopped`, замечание A8) | ✅ |
| MINOR-5: метка из будущего | `health.check`: `age < -FUTURE_SKEW_S` (5 с) даёт `from the future`; состояние вне `STATES` печатается как `other` (замечание A8). Тест `test_check_future_timestamp_and_unknown_state` | ✅ |
| A8 MINOR-2: memory healthy при отклонённом ключе | `memory.py` `_note`/`_pulse`/`_limit`. Strict-xfail A8 снят, тест зелёный. Тест A7 `test_memory_rejected_model_key_pauses_then_probes_one` | ✅. Остаток (до одной попытки на задачу за паузу) — Б-92 |
| Б-84 мелочь: поля 202 | `test_drafts_api.py:470` `(None, None)` | ✅ |
| Б-83 мелочь: помощник и параллельный прогон | `_pipeline_helpers.py`: снимает аренду до `AssertionError` (`pipeline_release(…, NULL, false)` под `feelings_worker`), в docstring — «параллельные прогоны не поддерживаются». Тест `test_pipeline_helpers.py` | ✅ |
| Б-83 мелочь: строка BACKLOG | сокращена до итога | ✅ |
| §9.1 п.5: журнал и ConfigError без токена | `check_bot_token` пишет только `http_<код>`/`network`/`timeout`. ConfigError содержит имя переменной и код HTTP (`from None`). URL с токеном у `httpx` скрывает `install_log_redaction`, как и раньше. Тесты проверяют отсутствие токена и `description` | ✅ |
| Сверх ТЗ | `FEELINGS_NOTIFY_CHECK_TOKEN` — новая настройка, записана в АР-49 п.5, DEPLOY и ADMIN. АР-49 по-прежнему «Предложено» | ⚠️ условие (как в р.1) |

## Оценка вопросов раунда

**getMe только на staging/production по умолчанию.** Не ослабление:
- `Settings.env` по умолчанию `production`. `token_check_enabled` выключает проверку только для явных `dev/local/test`; любое другое или неизвестное значение её включает (fail-safe).
- Стенд пропускает только `staging|production` (`docker-compose.prod.yml:51`, выход 64), поэтому на стенде проверку выключить нельзя ни случайно, ни через `.env`: переменная в compose не пробрасывается.
- Тесты процессов явно ставят `FEELINGS_NOTIFY_CHECK_TOKEN=false` (`_proc_env`), чтобы не ходить в настоящий Telegram с поддельным токеном. Саму проверку покрывают модульные тесты с `MockTransport`.
- Ошибку на стенде это не скрывает: 401/404 дают код 78, а сеть/5xx/429/тайм-аут — `bot token check skipped`, после чего токен проверит первая отправка и состояние станет `paused`. Это верно: недоступность Telegram не ошибка настройки.
- Не покрыта только связка `_main → token_check_enabled → run_with_token_check` (`pragma: no cover`) — MINOR-C.

**`paused` до подтверждения и вечный unhealthy.**
- Для неверного ключа или токена вечного ложного unhealthy нет: исправление `.env` требует пересоздания контейнера (`dc up -d`), и состояние сбрасывается.
- Остаётся один случай — `quota`: оператор пополняет баланс без перезапуска, очередь пуста. Тогда pipeline и memory остаются `paused`, пока не придёт первое сообщение или не закончится сессия. У memory пустая очередь — обычное состояние, так что это может длиться часы. См. MINOR-A.
- Обратная сторона: после перезапуска с по-прежнему неверным ключом pipeline и memory `healthy` до первого вызова, потому что ключ при старте не проверяется. См. MINOR-B.

**Пауза memory и пробный захват limit=1.**
- Корректно. На паузе захвата нет, пульс идёт.
- После паузы захватывается одна задача (и извлечение, и сжатие — по одной). Если модель недоступна (`unavailable`), пауза не продлевается, но `paused` держится и пробы идут каждые `poll_s` по одной.
- Захваченные до отказа задачи не начинаются, пишется строка `left until lease expiry`. Попытки расходуются: до одной на задачу и по одной на пробу. Это честно описано в АР-49 п.5, DEPLOY §4 и Б-92 (нужна `memory_release`, миграция A1).
- `_model_answered`: `refused` или хотя бы один вызов без ошибки. `skipped`/`empty` без вызова модели паузу не снимают — консервативно и правильно.

**any_within.**
- Время начала записывается при создании задачи, то есть чуть позже начала аренды в БД, поэтому оценка мягче на доли секунды.
- Если пауза и все слоты заняты — `paused` и так unhealthy.
- Удаление из словаря через `add_done_callback(lambda t: running.pop(t, None))` безопасно.

**FUTURE_SKEW_S = 5 с.**
- Проверка стоит после проверки состояния и до проверки свежести.
- Допуск 5 с разумен: часы в контейнере общие, а `ts` округляется до мс.

**Правка A0 в стражe A8 (`28000` или `28P01`).**
- По существу это не ослабление. На scram PostgreSQL на несуществующую роль отвечает 28P01, это видно на 5433. Прежнее требование `28000` на scram и в CI было ложно красным.
- Главные утверждения теста остались: код 78, строка `configuration error, exit 78`, нет секретов, нет трассировки.
- Формально проверка стала менее точной: на конкретном кластере принимается любой из двух кодов. A8 уже приводит её к точному коду через `_role_code` (незакоммиченная правка в рабочем дереве) — так и надо (MINOR-D).

## Находки

- **[MINOR-A] `docs/DEPLOY.md:785` (строка `state=paused`), `docs/ADMIN.md:724`.** После пополнения лимита (`quota`) без перезапуска pipeline и memory при пустой очереди остаются `Up (unhealthy)` до первого вызова модели, а у memory это может длиться часы. В таблице написано только «исправить ключ/лимит/токен; воркер сам повторяет», и оператор не узнает, что `healthy` сам не вернётся.
  **Ожидание:** в графе действия добавить: «после исправления лимита без смены `.env` — `dc restart worker-pipeline worker-memory`, иначе `paused` держится до первого сообщения». Либо снимать `paused` по бесплатной проверке ключа (MINOR-B). Источник: ТЗ §10 (мониторинг), АР-49 п.5.

- **[MINOR-B] `backend/app/workers/pipeline.py:65-71` (docstring), DECISIONS АР-49 п.5.** Обоснование «проверить ключ при старте нельзя без платного вызова» спорно. У OpenRouter есть бесплатный `GET /api/v1/key` с авторизацией ключом: ответ показывает, принят ли ключ, и остаток лимита. С ним pipeline и memory могли бы при старте выходить с кодом 78 на 401, как notify с `getMe`, а `quota` снимать без платного вызова и без ожидания сообщения. Сейчас после перезапуска с отклонённым ключом воркер `healthy`, пока не придёт первое сообщение.
  **Ожидание:** A7/A3 проверяют этот эндпоинт. Если он подходит — проверка при старте по образцу `run_with_token_check` (недоступность — пропуск); если нет — оставить как есть, уточнив формулировку в АР-49. Не блокирует: сообщения не теряются, пауза без расхода попытки.

- **[MINOR-C] `backend/app/workers/notify.py:565-570` (`_main`, `pragma: no cover`).** Связку `token_check_enabled(settings)` → `run_with_token_check(..., check_token=…)` не проверяет ни один тест: процессы запускаются с `FEELINGS_NOTIFY_CHECK_TOKEN=false`. Если флаг потеряется при передаче, проверка на стенде молча выключится.
  **Ожидание:** тест процесса notify с `FEELINGS_ENV=staging` (или `…CHECK_TOKEN=true`) против локального подменённого Bot API (если базовый URL настраивается), ожидание: код 78 и `Telegram rejected FEELINGS_TELEGRAM_BOT_TOKEN (HTTP 401)` без токена. Если базовый URL не настраивается — хотя бы тест, что `_main` передаёт результат `token_check_enabled` (monkeypatch `run_with_token_check`).

- **[MINOR-D] `backend/tests/test_workers_a8.py:540` (в `ee8a5ce`).** `assert "SQLSTATE 28000" in out or "SQLSTATE 28P01" in out` принимает любой из двух кодов на любом кластере.
  **Ожидание:** точный код кластера через `_role_code()`, как в `test_worker_health.py`. A8 это уже делает в рабочем дереве; при коммите убрать неиспользуемый импорт `_password_checked` (ruff F401).

- **[MINOR-E] `docs/BACKLOG.md:90` (Б-84).** В строке всё ещё «Лента и 202 — сделано, ждёт ревью». Б-84 принят в р.1, в р.2 закрыт последний MINOR. **Ожидание:** «сделано (ревью A6 р.1–2)», выгрузка — открыта (A1). На усмотрение A0.

Проверено, замечаний нет:
- `run_with_token_check`: при 401 выставляется `stop`, цикл дорабатывает начатое и снимает аренды, затем ConfigError выходит наружу; при SIGTERM проба отменяется в `finally`; ConfigError БД из цикла выходит через `await loop`.
- `_token_accepted` при 400/403: Telegram разобрал запрос с этим токеном. При 429 и 5xx он не вызывается.
- В `Heartbeat.working` берётся последнее состояние (`paused` не перетирается на `ok` во время длинной задачи memory). `starting` превращается в `ok`; `waiting` до первого захвата недостижим.
- Б-92 и п.5 АР-49 точно описывают остаточный расход попыток memory; страж `test_b83_a8` (голова 0016) отмечен.
- В изменённых файлах нет SQL в обработчиках и нет текстов, id или секретов в журналах.

## Условие приёмки

Как в р.1: АР-49 (теперь с п.5) в статусе «Предложено, на подтверждение оператора». Пока оператор не
подтвердит, задачу (часть A) готовой не считать.

## Итог

| Часть | Вердикт |
| --- | --- |
| A — воркеры (Б-85/88/90/91, АР-49 п.5) | ПРИНЯТО (MINOR-A…D; условие — подтверждение АР-49 оператором) |
| B — Б-84 | ПРИНЯТО (MINOR-E — документ) |
| C — Б-83 | ПРИНЯТО |

MAJOR р.1 закрыт, MINOR-1…5 р.1 и A8 MINOR-2 закрыты, BLOCKER и MAJOR нет.

ВЕРДИКТ: ПРИНЯТО
