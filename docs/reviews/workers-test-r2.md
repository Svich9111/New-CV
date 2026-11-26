# Тестирование: надёжность воркеров, раунд 2 — A8

Что проверялось: ветка `claude/nifty-maxwell-7geamm`, коммит `ee8a5ce`
(`git diff 397a4b2 ee8a5ce -- backend`): тесты отказа входа на любом кластере; `paused` держится до
подтверждающего ответа (pipeline, notify, memory); `getMe` при старте notify; пауза memory при
отклонённом ключе (Б-92); пульс при занятых слотах; метка из будущего; остановка memory/kb.

Источники: `docs/TZ.md` §9.1 п.5, §10; `docs/DECISIONS.md` АР-49 п.1–5; `docs/BACKLOG.md` Б-85, Б-90,
Б-92; мой отчёт р.1 `docs/reviews/workers-b83-b84-test-r1.md` (MAJOR-1, MINOR-2, замечания); ревью р.1
`docs/reviews/workers-b83-b84-review-r1.md` (MINOR-1…5).

Новые тесты — `backend/tests/test_workers_a8.py`, раздел «раунд 2 (ee8a5ce)», 18 случаев. Ещё в файле
ужесточён страж `test_each_worker_exits_78_on_rejected_login_without_secret_values` (см. §2.1) и
исправлен устаревший docstring `test_memory_rejected_model_key_during_work_is_unhealthy`. В файле
теперь 48 случаев, xfail нет. Продуктовый код не менялся, коммитов нет, `frontend/` не трогался.
Роли `feelings_*` не менялись.

## 1. Прогоны

| Команда | Итог |
| --- | --- |
| `make lint` | ruff `All checks passed!`, `213 files already formatted`; mypy `Success: no issues found in 98 source files`; eslint + tsc без ошибок, `Типы API соответствуют api/openapi.yaml.` |
| `mypy --strict tests/test_workers_a8.py` (ошибки по этому файлу) | нет |
| Отказ входа, 5432 (trust): `test_worker_health.py test_workers_a8.py -k "rejected or wrong_password or wrong_db_password or exits_78"` | `20 passed, 2 skipped`; SKIPPED — `test_wrong_password_login_is_config_error` и `test_process_wrong_db_password_exits_78`, причина: `кластер без проверки пароля (trust): отказ входа по паролю не проверить — нужен scram, см. CLAUDE.md` |
| То же, 5433 (scram) | `22 passed` — пропусков нет, оба теста неверного пароля выполнены |
| То же с `CI=true`, 5432: `-k "wrong_password or wrong_db_password"` | `2 errors`: `Failed: кластер без проверки пароля (trust)…` из фикстуры `password_auth`, код выхода не 0, тихого пропуска нет |
| Раунд 2, 5433 (scram), целевые `-k` по 18 новым случаям и стражу | `22 passed` |
| `test_workers_a8.py` целиком, 5432 | `48 passed`. Новые случаи гонялись подряд: 4 из 4 прогонов зелёные после исправления выборки в моём тесте (см. ниже), чувствительные ко времени — ещё 6 из 6 |
| Полный `make test` (5432, один раз, 10 мин 24 с) | backend: `6147 passed, 2 skipped, 16 xfailed, 2 warnings`. 2 skipped — оба теста неверного пароля (trust), xfail стало на 1 меньше, чем в р.1: снят MINOR-2. frontend (vitest): `Test Files 27 passed`, `Tests 523 passed`. Итог: `exit=0` |

Первая версия `test_pipeline_paused_survives_unavailable_model_until_answer` раз из пяти падала
`'ok' is not in list`: сэмплер останавливался раньше, чем успевал записать последнее `ok`. Ошибка в
тесте, не в продукте: последнее состояние теперь дописывается после остановки. После этого тест
зелёный 10 раз из 10.

## 2. Критерии → тест / команда → результат

### 2.1 Тесты отказа входа (MAJOR-1 р.1; АР-49 п.2; Б-90)

| Критерий | Тест / команда | Результат |
| --- | --- | --- |
| На 5432 (trust) тесты отказа входа зелёные, неверный пароль — skip с причиной | прогон из §1 | пройден: 20 passed, 2 skipped с причиной |
| На 5433 (scram) выполняется всё | прогон из §1 | пройден: 22 passed, 0 skipped |
| `CI=true` на trust — падение, не пропуск | прогон из §1 | пройден: 2 errors, `Failed: …` |
| CI действительно идёт на scram | `.github/workflows/ci.yml`: контейнер `-e POSTGRES_PASSWORD=postgres`, подключение по TCP `localhost:5432`. В образе для `host` — scram, GitHub Actions выставляет `CI=true` | подтверждено чтением конфигурации |
| Правка A0 в моём страже (принимать 28000 **или** 28P01) верна для scram | `test_missing_role_sqlstate_matches_cluster_auth`: прямой вход asyncpg ролью, которой нет. На 5432 — `28000`, на 5433 — `28P01`, и это совпадает с `_role_code()` (пробный вход с неверным паролем). Значения пароля в тексте ошибки нет | пройден на обоих кластерах: на scram PostgreSQL отвечает 28P01, 28000 там не получить |
| Смысл стража не ослаблен | Вместо «любой из двух» страж теперь требует ровно код этого кластера: `f"SQLSTATE {await _role_code()}"`. Ответ 28P01 на trust был бы ошибкой, и тест бы её поймал. Код 78, строка CRITICAL и отсутствие секретов проверяются, как раньше | ужесточён; 4× пройден на 5432 и на 5433 |

Вывод по правке A0: она верна. На scram несуществующая роль даёт 28P01, и оба кода входят в набор
ошибок настройки (АР-49 п.2). В её виде страж принимал оба кода на любом кластере, то есть был
немного слабее, чем мог быть. Теперь ожидаемый код привязан к кластеру.

### 2.2 `paused` держится до подтверждающего ответа (АР-49 п.5; р.1 MINOR-2; ревью р.1 MINOR-2)

| Критерий | Тест | Результат |
| --- | --- | --- |
| notify: после 401 `paused` **не** снимают 429, 502 и сбой сети, снимает только ответ, принявший токен (403 или 400) | `test_notify_paused_kept_by_429_5xx_network_cleared_only_by_accepted_token[403/400]`, настоящая БД: Bot API отвечает по сценарию 401 → 429 → 502 → ConnectError → 403/400. Перед каждым запросом записан `config_paused`: `[False, True, True, True, True]`, после каждого прохода пульс `paused`. После 403/400 — `ok`, health — healthy | пройден ×2 |
| pipeline: после паузы, когда модель недоступна, `paused` держится и во время отсрочки повтора, ответ модели переводит в `ok` | `test_pipeline_paused_survives_unavailable_model_until_answer`, настоящая БД: safety отвечает `LLMAuthError`, затем `LLMUnavailable`, затем отвечает. Между «недоступна» и ответом в выборке пульса только `paused`, сообщение `delivered` | пройден |
| pipeline/notify при пустой очереди после паузы — unhealthy | `test_worker_health.py::test_pipeline_paused_with_empty_queue_stays_unhealthy`, `::test_notify_paused_holds_after_pause_until_token_accepted` (A7) | пройдены |
| memory: во время пробной задачи пульс длинной работы пишет `paused`, а не `ok` | `test_memory_probe_job_pulse_stays_paused_while_working`: `_pause_kind="auth"`, проба идёт 0,1 с, внутри `working` файл пульса — `paused` | пройден |

### 2.3 getMe при старте notify (АР-49 п.5)

| Критерий | Тест | Результат |
| --- | --- | --- |
| Процесс: `staging` → 401, `production` → 404 — код 78, токена в выводе нет | `test_notify_process_getme_rejected_exits_78_without_token[staging-401/production-404]`: настоящий процесс `app.workers.notify`, Bot API — локальный HTTP-сервер (base_url подменён в процессе запуска), БД недоступна. Код 78, `Telegram rejected FEELINGS_TELEGRAM_BOT_TOKEN (HTTP 401/404)`, `getMe` ровно один. В выводе нет токена, пароля, ключей, описания Telegram (`DESCR-A8`) и `Traceback` | пройден ×2 |
| Сеть/5xx/429 — пропуск, процесс работает | `test_notify_process_getme_unavailable_keeps_running[502/429]`: процесс жив через 1 с после `getMe`, `bot token check skipped: http_502/http_429`, SIGTERM → 0, `notify worker stopped`, секретов нет | пройден ×2 |
| Сеть недоступна, тайм-аут | `test_worker_health.py::test_bot_token_check_unreachable_is_skipped`, `::test_bot_token_check_timeout_is_skipped`, `::test_run_with_token_check_unreachable_telegram_keeps_loop` (A7) | пройдены |
| По умолчанию только staging/production; dev — без `getMe` | `test_notify_process_dev_does_not_call_getme`: `FEELINGS_ENV=dev`, флаг не задан, «Telegram» ответил бы 401 — за 1,5 с запросов нет, процесс жив, SIGTERM → 0. Таблица окружений — `test_token_check_on_stand_and_production_by_default` (A7) | пройден |

### 2.4 memory при отклонённом ключе (АР-49 п.5; Б-92; р.1 MINOR-2)

| Критерий | Тест | Результат |
| --- | --- | --- |
| Остаток пачки — без вызовов модели; на паузе нет ни захватов, ни вызовов | `test_memory_rejected_key_real_db_probe_one_and_one_attempt_per_pause`: **настоящая БД**, настоящие `memory_claim_extraction` и `run_extraction`, модель-заглушка, 3 агента. Чужие задачи общей БД заранее заняты арендой на 2 мин, захват проверяет, что в выдаче только свои. Проход 1: `limit=10`, захвачено 3, вызов модели 1 (`auth`), попытки `[1,1,1]`, пульс `paused/auth`, unhealthy. Три прохода на паузе — без захвата и вызовов | пройден |
| После паузы — пробный захват одной задачи | Тот же тест: после паузы `limit=1`, захвачена 1, вызов 1. Ещё раз `auth` — пауза снова | пройден |
| Не больше одной попытки на задачу за паузу (Б-92) | Тот же тест: за пробу сумма попыток выросла ровно на 1, у каждой задачи — не больше чем на 1. После трёх окон максимум ≤ 3 | пройден |
| Сбой сети на пробе `paused` не снимает | Тот же тест: проба → `unavailable`, пульс `paused/auth`, unhealthy | пройден |
| Ответ модели снимает паузу и обнуляет счётчик; дальше — полные пачки | Тот же тест: проба → `done`, пульс `ok`, дальше `limit=10`, у всех трёх агентов `memory_attempts = 0`, healthy. В файле пульса нет текста реплик | пройден |
| Модульный сценарий A7 | `test_worker_health.py::test_memory_rejected_model_key_pauses_then_probes_one`. Мой р.1 `test_memory_rejected_model_key_during_work_is_unhealthy` (был xfail) — зелёный без xfail | пройдены |

### 2.5 Пульс при занятых слотах, метка из будущего (ревью р.1 MINOR-3, MINOR-5)

| Критерий | Тест | Результат |
| --- | --- | --- |
| Все слоты заняты: пульс идёт, пока **хотя бы одна** задача моложе аренды | `test_busy_slots_pulse_while_any_one_task_within_lease[pipeline/notify]`: 2 слота, аренда 0,5 с, задачи начаты в 0 и около 0,3 с. На 0,55 с первая уже старше аренды, вторая нет — метка растёт. На 1,0 с обе старше — метка замерла, цикл жив | пройден ×2 |
| Все зависли — пульс устаревает | `test_worker_health.py::test_busy_slots_pulse_only_while_a_task_is_within_lease` (A7) | пройден |
| Метка из будущего, команда compose `python -m app.workers.health` | `test_health_cli_future_timestamp`: +60 с → код 1 `from the future`; +3 с → 0; −3 с → 0; `paused` из будущего → 1 `state=paused` | пройден ×4 |
| Неизвестное состояние печатается как `other` (моё замечание р.1) | `test_worker_health.py::test_check_future_timestamp_and_unknown_state` (A7) | пройден |

### 2.6 Остановка memory/kb (ревью р.1 MINOR-1; моё замечание р.1)

| Критерий | Тест | Результат |
| --- | --- | --- |
| memory после SIGTERM не захватывает новое, сжатие тоже; следующая задача пачки не начинается | `test_worker_health.py::test_memory_stop_between_extraction_and_compaction`, `::test_memory_stopped_before_pass_claims_nothing` (A7) | пройдены |
| kb: остановка между материалами и **до** прохода | `::test_kb_stop_between_documents` (A7); `test_kb_stopped_before_pass_claims_nothing`: `stop` задан — `run_once` и `run_forever` ни разу не вызывают `claim_index` | пройдены |
| «worker stopped» в журнале, код 0 | `test_worker_health.py::test_memory_kb_sigterm_exit_0_and_logged[loop/unavailable × memory/kb]` (A7) | пройден ×4 |

### 2.7 Мутации (по новым веткам; после каждой файл восстановлен `git checkout`, в конце `git status --short app/` пуст)

| № | Мутация | Кто поймал |
| --- | --- | --- |
| M5 | `pipeline._pulse`: `paused` только пока идёт пауза (`self.paused` вместо `_pause_kind`) | `test_pipeline_paused_survives_unavailable_model_until_answer` (A8), `test_pipeline_paused_with_empty_queue_stays_unhealthy` (A7) |
| M6 | `notify._classify`: токен «принят» любым ответом, не только 403/400 (`if True:`) | только `test_notify_paused_kept_by_429_5xx_network_cleared_only_by_accepted_token[403/400]` (A8); тесты A7 зелёные |
| M7 | memory: после паузы полная пачка вместо пробы одной (`_limit` → `batch`) | `test_memory_rejected_key_real_db_probe_one…` (A8), `test_memory_rejected_model_key_pauses_then_probes_one` (A7) |
| M7b | memory: любой исход снимает паузу (`_model_answered` → `True`) | те же два |
| M8 | `any_within`: `any` → `all` | `test_busy_slots_pulse_while_any_one_task_within_lease[pipeline/notify]` (A8), `test_any_within` (A7) |
| M8b | `check`: без проверки метки из будущего | `test_health_cli_future_timestamp[60.0-ok-1-…]` (A8), `test_check_future_timestamp_and_unknown_state` (A7) |
| M8c | `check_bot_token`: 401/404 не ошибка настройки | `test_notify_process_getme_rejected_exits_78_without_token` ×2 (A8), `test_bot_token_check_rejected_is_config_error` ×2 (A7) |
| M8d | `token_check_enabled`: по умолчанию выключено везде | процессные getMe ×4 (A8), `test_token_check_on_stand_and_production_by_default` ×2 (A7) |
| M8e | kb: нет проверки `stop` перед захватом | только `test_kb_stopped_before_pass_claims_nothing` (A8) |
| M8f | `Heartbeat.working`: всегда `ok` | только `test_memory_probe_job_pulse_stays_paused_while_working` (A8) |

M6, M8e и M8f ловят только новые тесты A8: в наборе A7 этих проверок не было.

## 3. Негативные сценарии ТЗ

| Сценарий | Где проверено |
| --- | --- |
| Поддельные данные | метка из будущего (CLI); `state` вне набора → `other` (A7); подделанный файл и причина пульса (р.1) |
| Чужой владелец | memory: в захват не попадает чужой агент (проверка в обёртке, иначе тест падает); notify-запросы — только к чатам пары (р.1) |
| Истёкший срок | аренда memory короче паузы — проба после истечения; занятые слоты старше аренды — пульс замирает |
| Повторное использование | попытка не тратится повторно за паузу (Б-92); getMe — ровно один запрос |
| Утечка к партнёру | к этим изменениям не относится; в файле пульса нет текста реплик (тест memory) |
| Инструкции в тексте сообщения / ответе | описание ошибки от Telegram (`DESCR-A8 Unauthorized`) не попадает в вывод процесса и в текст `ConfigError` |
| Секреты в журналах (ТЗ §9.1 п.5) | выход 78 по getMe и по отказу входа — ни токена, ни пароля, ни ключей; пропуск getMe — только код HTTP |

## 4. Дефекты

Дефектов в `ee8a5ce` не найдено. MAJOR-1 р.1 (тесты отказа входа на trust) и MINOR-2 р.1 (memory
healthy при отклонённом ключе) исправлены и проверены выше.

Замечания без дефекта:

- `paused` держится до подтверждающего ответа, и у этого есть обратная сторона. Если ключ или квоту
  починили без перезапуска (например, пополнили квоту OpenRouter), а очередь пуста, контейнер
  остаётся unhealthy до первого сообщения. Docker unhealthy-контейнер не перезапускает, так что это
  ложная тревога, а не сбой. АР-49 п.5 этот выбор описывает явно. Стоит одной строкой добавить в
  DEPLOY §4: «после исправления ключа без перезапуска unhealthy держится до первого сообщения».
- АР-49 п.5 в `DECISIONS.md` пока со статусом «Предложено… на подтверждение оператора». Тесты
  проверяют предложенное поведение.
- Б-92 остаётся в силе: пока ключ отклонён дольше, чем примерно `max_attempts` пауз, задачи по одной
  уходят в `skipped`. Это записано в BACKLOG (A1 + A7, до 4.7).

## 5. Сверка с ТЗ

| Поведение | ТЗ / решение | Итог |
| --- | --- | --- |
| Тесты отказа входа зелёные на основном кластере, полные — на scram и в CI | CLAUDE.md (кластеры); АР-49 п.2; Б-90 | выполнено |
| Ошибка настройки видна в health check и не исчезает сама при пустой очереди | ТЗ §10; АР-49 п.1, п.5; Б-85 | выполнено (pipeline, notify, memory) |
| Неверный токен бота на стенде/в продуктиве — код 78 при старте; недоступность Telegram — не ошибка настройки | ТЗ §10; АР-49 п.2, п.5; Б-90 | выполнено |
| memory при отклонённом ключе: пауза, проба одной задачи, ≤ 1 попытки на задачу за паузу | АР-49 п.5; Б-92 | выполнено; полное «без расхода» — Б-92, нужна миграция |
| Зависание видно и при занятых слотах; метка из будущего — неисправность | ТЗ §10; АР-49 п.5; Б-85 | выполнено |
| Остановка memory/kb: новое не захватывается, «worker stopped» в журнале | ТЗ §10; АР-49 п.5 | выполнено |
| Журналы, вывод процесса и пульс без текстов, id и секретов | ТЗ §9.1 п.5; CLAUDE.md | выполнено |

ВЕРДИКТ: ПРИНЯТО
