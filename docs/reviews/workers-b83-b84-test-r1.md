# Тестирование: воркеры (Б-85, Б-88, Б-90, Б-91), Б-84, Б-83 — A8, раунд 1

Что проверялось: ветка `claude/nifty-maxwell-7geamm`, HEAD `0e02c17`:

- (A) коммит `66dc14e` — пульс и health check, выход 78, повтор при старте, notify 429/остановка,
  `stop_grace_period` (BACKLOG Б-85, Б-88, Б-90, Б-91; DECISIONS АР-49);
- (B) коммит `4e477a4` — причина задержки в ленте только автору (Б-84; АР-44, АР-45);
- (C) коммит `841776e` — миграция 0016, снятие функций конвейера без аренды, помощник тестов
  `tests/_pipeline_helpers.py` (Б-83; АР-48).

Источники: `docs/TZ.md` §2 п.2, §4.4 п.5, §4.5, §6.2, §6.3, §9.1 п.5, §9.3, §10; ФТ-09; `docs/PROCESS.md`;
`docs/BACKLOG.md` Б-83…Б-91; `docs/DECISIONS.md` АР-44, АР-45, АР-48, АР-49. Отчёт ревью A6 здесь не использовался.

Новые тесты A8 (продуктовый код не менялся, коммитов нет):

- `backend/tests/test_workers_a8.py` — 30 случаев (29 passed, 1 `xfail(strict=True)` — дефект MINOR-2);
- `backend/tests/test_b83_a8.py` — 14 случаев (14 passed).

Основной кластер — 5432 (`trust`, `/var/tmp/feelings-pgdata`); на 5433 (проверка пароля) — только 3 целевых теста
отказа входа (`-k rejected`), 2,4 с. Роли `feelings_*` не менялись; downgrade/upgrade 0016 — только тестовая БД
`feelings_test` на 5432, в конце `alembic current` = `0016 (head)`.

## 1. Прогоны

| Команда | Итог |
| --- | --- |
| `make lint` | ruff `All checks passed!`, `212 files already formatted`, mypy `Success: no issues found in 98 source files`; eslint + tsc без ошибок, `Типы API соответствуют api/openapi.yaml.` |
| `ruff check` / `ruff format --check` / `mypy` (strict) по двум новым файлам | без ошибок |
| `npx vitest run` (frontend; там работает A4, файлы не трогались) | `Test Files 15 passed (15)`, `Tests 327 passed (327)` |
| Целевые на 5432: `test_worker_health.py test_notify_worker.py test_pipeline_worker.py test_deploy_a8.py test_feed_withheld.py` | **2 failed**: `test_rejected_worker_login_is_config_error_not_retry` (`TimeoutError` через 20,00 с), `test_process_rejected_db_login_exits_78` (`TimeoutExpired … after 60 seconds`) — MAJOR-1; остальные зелёные |
| Те же 2 теста на 5433 (пароль) | `3 passed` (`-k rejected`) |
| `test_workers_a8.py` на 5432 | `29 passed, 1 xfailed`, 5 повторов подряд (один с `-W error::PytestUnraisableExceptionWarning`) |
| `test_b83_a8.py` на 5432 | `14 passed` |
| Полный backend `pytest tests` на 5432 (один раз, 11 мин 18 с) | `2 failed, 6096 passed, 17 xfailed, 2 warnings`: 2 FAILED — MAJOR-1 (те же два теста); среди 17 xfailed — MINOR-2 |

## 2. Критерии → тест / команда → результат

### (A) Воркеры — коммит `66dc14e` (ТЗ §10, §9.1 п.5; АР-49; Б-85, Б-88, Б-90, Б-91)

| Критерий | Тест / команда | Результат |
| --- | --- | --- |
| Пульс устаревает при **недоступной БД** — все 4 воркера | `test_workers_a8.py::test_pulse_goes_stale_for_all_four_workers_when_db_down_or_hung[unreachable]`: у memory, kb, pipeline, notify записан «старый» `ok` (−100 с); цикл 1 с против `127.0.0.1:1`; метка не сдвинулась; `python -m app.workers.health --max-age 60` → код 1, `unhealthy: stale` | пройден |
| Пульс устаревает при **зависшем цикле** | `…[silent]`: «БД» принимает TCP и молчит — захват висит у всех 4 (задачи не завершены), метка стоит, health check → 1. `test_pulse_fresh_then_stale_after_db_goes_down`: на живой БД метка растёт и health check = 0, захват повис — метка замерла, `--max-age 1` → 1. `test_memory_job_hung_beyond_lease_pulse_stops`, `test_kb_hung_index_beyond_lease_pulse_stops`: задача дольше аренды — пульс останавливается | пройдены |
| Тест действительно ловит | мутация: `self._pulse()` в ветке `except` захвата `pipeline.py` → `…[unreachable]` FAILED; файл восстановлен (`git status` чист) | ловит |
| Пауза из-за настройки → health check **exit 1** | `test_paused_by_config_error_health_cli_exit_1`: конвейер (`LLMAuthError`) — пульс свежий (< 5 с), CLI → 1, `unhealthy: state=paused reason=auth`; notify (401) — CLI → 1, `state=paused reason=http_401` | пройден |
| Файл пульса без текстов/id; подделанная причина → `other` | `test_forged_reason_replaced_by_other` (10 случаев): текст сообщения, uuid, `user 1234`, 33 символа, `http_401\nInjected`, путь `.env`, пустая строка → `other`; `"  AUTH  "` → `auth`; ключи файла только `ts/state/reason`. `test_forged_file_with_text_reason_health_output_has_no_text`: подделанный файл с текстом в `reason` — вывод CLI `reason=other`, текста нет. `test_real_pass_heartbeat_has_no_text_or_ids`: настоящий проход конвейера и notify с секретным текстом — в файлах нет текста, id сообщения (оба вида), пользователей, пары, chat_id | пройдены |
| Выход **78 у каждого из 4** воркеров без значений секретов | `test_each_worker_exits_78_on_rejected_login_without_secret_values[memory/kb/pipeline/notify]`: заданы пароль БД, `OPENROUTER_API_KEY`, ключ шифрования, ключ отпечатков, токен бота; роль входа не существует (SQLSTATE 28000 — работает на любом кластере) → код 78, `configuration error, exit 78 (EX_CONFIG)`, `SQLSTATE 28000`; ни одного значения секрета, нет `Traceback` | пройден ×4 |
| Повтор при старте; SIGTERM во время ожидания → 0 | `test_startup_retry_backoff_then_sigterm_exits_0[memory/kb]`: каталог моделей недоступен — процесс жив 3,3 с, паузы `1, 2, 4` с, пульс `waiting`/`models_unavailable`, CLI → 1; SIGTERM → код 0. `test_notify_db_unreachable_at_start_sigterm_exits_0`: БД недоступна — не 78, пульс `starting`, CLI → 1, SIGTERM → 0. pipeline — `test_worker_health.py::test_process_waits_for_model_at_start_unhealthy_not_exiting` (зелёный) | пройдены |
| Серия коротких 429 не исчерпывает попытки | `test_short_429_series_with_real_pauses_never_spends_attempts`: настоящие паузы `retry_after=1`, аренда 3 с (окно 1,5 с), `max_attempts=2`, 4 захвата — каждый раз аренда снята, `work_attempts = 0`, не помечено; затем — `sent`, попытка 1, одно уведомление нужному чату. Плюс `test_notify_worker.py::test_long_flood_of_short_429_never_exhausts_attempts` (ветка `max_sends`) | пройден |
| Остановка notify снимает аренду | `test_stop_releases_every_unsent_lease`: 3 уведомления ждут повтора после 502 (пауза 30 с) — остановка не начала повторов (запросов 3, отправлено 0, все к чатам этой пары), у всех трёх аренда снята, попытки 0; новый процесс сразу отправляет 3, по одному | пройден |
| `stop_grace_period` ≥ аренды | `test_pipeline_stop_grace_not_less_than_lease[infra/docker-compose.yml, …prod.yml]`: `200s` ≥ `FEELINGS_PIPELINE_LEASE_S` сервиса или значения по умолчанию `Settings` (180); у всех 4 воркеров health check — `python -m app.workers.health`, файл пульса не в общем томе | пройден ×2 |
| Отказ входа в БД на trust-кластере 5432 | факт: два теста `test_worker_health.py` **падают** (не висят бесконечно): 20,00 с и 60,03 с — неверный пароль на `trust` принимается, цикл работает, ожидаемого `ConfigError` нет | дефект **MAJOR-1** |

### (B) Б-84 — коммит `4e477a4` (ТЗ §9.3, §4.5 п.2, §4.4 п.5, §6.3; АР-44, АР-45)

| Критерий | Тест / команда | Результат |
| --- | --- | --- |
| Причина и тип кризиса видит только автор | `test_feed_withheld.py` (A2, 16 случаев: каждое значение CHECK и тип кризиса) — зелёные на 5432 | пройден |
| Получатель/посторонний не видят — **на уровне функции БД** | `test_b83_a8.py::test_feed_function_hides_withheld_from_recipient_and_outsider`: `app_couple_feed_page` под ролью приложения от имени Игоря и Ольги — строки остановленного нет, в их строках причина/тип `null`; у автора — `safety`/`violence`. Остановка — настоящим путём воркера | пройден |
| Вторая граница (`couple_messages._withheld`) | `test_second_boundary_nulls_fields` (5 случаев): не-автор, не-`withheld`, `processing` → `null`; тип кризиса при `model_refusal` → `null` | пройден |
| Постраничная лента получателя | `test_recipient_paged_feed_never_shows_withheld`: `limit=1`, проход по курсору — 3 страницы, остановленных нет, поля `null`, текстов остановленных нет; у автора — 3 с `pipeline_error` | пройден |
| Утечка к партнёру через выгрузку | `test_recipient_export_has_no_withheld_message`: `/me/export` Игоря — нет id, текста, слов `withheld`/`violence` | пройден |
| Поддельная ссылка ответа | `test_recipient_cannot_reply_to_withheld`: `reply_target_ok` (проверка `POST /drafts`) для остановленного = `False` у Игоря, Ольги и Анны — так же, как для случайного id; оригинал — 404 | пройден |

### (C) Б-83 — коммит `841776e` (ТЗ §2 п.2, §6.2 п.3–7, §10; ФТ-09; АР-48)

| Критерий | Тест / команда | Результат |
| --- | --- | --- |
| Старых функций нет ни у одной роли | `test_no_unleased_function_under_any_name_or_schema`: в `pg_proc` нет `pipeline_original*`/`pipeline_deliver`/`pipeline_withhold` ни с какой сигнатурой ни в какой схеме, `pipeline_withhold_leased` — только 5 аргументов; по каждой роли `feelings*` и `PUBLIC` — ни одной исполнимой; 4 вызова под `feelings_worker` → 42883 | пройден |
| Новых прав ни у одной роли; данные не тронуты; downgrade рабочий | `test_0016_round_trip_rights_data_and_working_downgrade`: 0016 → 0015 → 0016 → head; у каждой роли набор исполнимых функций на 0016 = на 0015 минус ровно 4 снятых (у `feelings_worker`), у остальных ролей без изменений, приобретений нет; права на таблицы и столбцы равны; `partner_messages` побайтно та же (md5 всех строк); на 0015 прежние функции исполнимы воркером (не 42883/42501), на 0016 — 42883; `alembic check` чист; `alembic heads` = `0016 (head)` | пройден |
| Точное восстановление при downgrade | `test_pipeline_db.py::test_migration_0016_reversible_restores_functions_exactly` (A1) — зелёный | пройден |
| В коде приложения старых имён нет | `test_app_code_has_no_removed_names`: `app/**/*.py` без вызовов; в `texts` нет `pipeline_read_original`/`pipeline_deliver` | пройден |
| Помощник не крадёт чужие строки | `test_helper_does_not_steal_neighbour_processing_rows`: два соседних `processing` после `withhold` и `deliver_blob` нужных — аренда, срок, счётчик, статус те же; у остановленного аренда и срок сняты | пройден |
| Помощник не перехватывает чужую аренду | `test_helper_does_not_take_over_foreign_lease`: под живой арендой — `withhold` → `False`, `deliver_blob` → `lost`, аренда прежняя; владелец токена завершает | пройден |
| **Мутация M1**: 0016 не снимает `pipeline_original` (`REMOVED_SIGNATURES[1:]` в `upgrade`) | downgrade 0015 → мутация → upgrade head → 5 FAILED: `test_b83_a8::test_no_unleased_function_under_any_name_or_schema`, `::test_0016_round_trip…`, `test_rls_a8::test_unleased_pipeline_functions_removed`, `test_pipeline_db::test_unleased_functions_removed`, `::test_migration_0016_reversible…`; файл восстановлен, лишняя функция снята `DROP FUNCTION`, затем 17 passed | ловят |
| **Мутация M2**: downgrade не возвращает `GRANT` на `pipeline_withhold(uuid)` | 2 FAILED: `test_0016_round_trip…` (`42501`), `test_migration_0016_reversible…`; файл восстановлен | ловят |
| **Мутация H1**: помощник без удержания чужих строк | FAILED `test_helper_does_not_steal_neighbour_processing_rows` (`захват выдал чужое сообщение`) | ловит |
| **Мутация H2**: помощник молча отбрасывает чужие строки вместо ошибки | FAILED `test_helper_does_not_steal_neighbour_processing_rows` (соседи остались под арендой) | ловит |
| **Мутация H3**: помощник истекает чужую аренду перед захватом | FAILED `test_helper_does_not_take_over_foreign_lease` | ловит |

После всех мутаций `git status --short app/ migrations/ tests/_pipeline_helpers.py` пуст, `alembic current` — `0016 (head)`.

## 3. Негативные сценарии ТЗ

| Сценарий | Где проверено |
| --- | --- |
| Поддельные данные | подделанная причина и подделанный файл пульса (A); поддельная ссылка ответа на остановленное (B); `test_feed_withheld.py::test_forged_ids_are_404` |
| Чужой владелец | лента и функция БД от имени получателя и постороннего (B); помощник и чужая аренда (C); выгрузка получателя (B) |
| Истёкший срок | задача memory/kb дольше аренды — пульс замирает (A); отсрочка снятой аренды notify (A) |
| Повторное использование | остановка notify — ровно одно уведомление после перезапуска (A); завершение по чужому токену — `lost`/`False` (C) |
| Утечка к партнёру | B целиком; в файле пульса нет id/текстов (A) |
| Инструкции в тексте сообщения | `http_401\nInjected: x` в причине пульса → `other` (A); остальное к этим коммитам не относится |
| Секреты в журналах | выход 78 — ни одного значения секрета в выводе 4 воркеров (A) |

## 4. Дефекты

- **[MAJOR-1] Два теста Б-90 падают на основном тестовом кластере 5432 (`trust`), `make test` красный.**
  `test_worker_health.py::test_rejected_worker_login_is_config_error_not_retry` — `TimeoutError` через 20,00 с;
  `::test_process_rejected_db_login_exits_78` — `subprocess.TimeoutExpired … after 60 seconds`. Причина: оба
  ожидают 28P01 на неверный пароль, а `trust` пароль не проверяет — воркер входит и работает. Они не висят дольше
  своих тайм-аутов, но падают. В коммите это признано («2 теста health требуют кластер с паролем — на 5433
  зелёные»), а CLAUDE.md называет основным для тестов кластер 5432 (`trust`). На 5433 — `3 passed`. Что
  предлагаю: для отказа входа брать несуществующую роль — SQLSTATE 28000 приходит при любом способе входа, это
  показывает `test_workers_a8.py::test_each_worker_exits_78_on_rejected_login_without_secret_values` (4× зелёный
  на 5432). Случай 28P01 оставить, но пропускать, если кластер принимает неверный пароль (пробное подключение).
  Править тесты — A7. A8 их не ослаблял.
- **[MINOR-2] memory остаётся healthy, если ключ модели отклонён во время работы.** Конвейер в этом случае
  встаёт на паузу (`paused`, unhealthy), а memory только записывает исход задачи `error=auth` и продолжает
  писать `ok`. Задачи тратят попытки до `skipped` (потеря памяти, журнал `skipped after max attempts`), и по `dc ps` этого не
  видно. В Б-85 сказано: «пауза из-за ошибки настройки (модель: auth/…) — unhealthy». Тест
  `test_workers_a8.py::test_memory_rejected_model_key_during_work_is_unhealthy` — `xfail(strict=True)`. Что
  предлагаю: исход `error` с видом `auth`/`quota`/`config`/`model_not_found` → пульс `paused` (по образцу
  выключателя конвейера АР-45 п.2) или пауза memory. Ответственный — A3/A7.

Замечания без дефекта:

- `health.check` выводит `state` из файла как есть, без `_safe_reason`. Файл пишет только сам воркер, значение
  проверено по `STATES`, так что текста там не бывает. При защите в глубину стоило бы печатать
  `state` ∉ `STATES` как `other`.
- memory и kb, остановленные SIGTERM во время повтора при старте, выходят с кодом 0, но без строки
  `… worker stopped` (у pipeline и notify она есть). Это мелочь для журнала.
- Если «БД» молчит (TCP принят, ответа нет), цикл воркера не видит `stop`, пока не истечёт тайм-аут подключения
  asyncpg. У notify `stop_grace_period: 30s`, так что Docker пошлёт SIGKILL. Данным это не вредит: захвата ещё
  не было.

## 5. Сверка с ТЗ

| Поведение | ТЗ / решение | Итог |
| --- | --- | --- |
| Сбой воркера виден в health check, ошибка настройки отличима от недоступности | ТЗ §10; АР-49 п.1–3; Б-85, Б-90 | выполнено; дефекты MAJOR-1 (тесты на 5432) и MINOR-2 (memory) |
| Журналы, файл пульса и вывод при выходе без текстов, id и секретов | ТЗ §9.1 п.5; CLAUDE.md | выполнено |
| Остановка не теряет и не дублирует уведомления, попытки не тратятся на лимит бота | ТЗ §10; Б-88, Б-91; АР-49 п.4 | выполнено |
| Причина задержки — только автору, партнёру ничего | ТЗ §9.3, §4.5 п.2; АР-44, АР-45; Б-84 | выполнено |
| Нет обхода одобрения и аренды; новых прав никому; миграция обратима | ТЗ §2 п.2, §6.2 п.3–7; ФТ-09; АР-48; Б-83 | выполнено |
| Применение 0016 вне локальной и тестовой БД | RoadMap §4; АР-4 | не проверялось: только после подтверждения оператора |

ВЕРДИКТ: НА ИСПРАВЛЕНИЕ

Нужно исправить: MAJOR-1, иначе `make test` красный на основном тестовом кластере. Б-84 и Б-83 по существу
приняты, дефектов в них нет. MINOR-2 можно перенести в BACKLOG, если оператор согласится.
