# Тестирование: 0021 / АР-57 / Б-103 — A8, раунд 2

Что проверялось: ветка `claude/nifty-maxwell-7geamm`. Исправления A1 лежат в рабочей копии и не закоммичены:

- `backend/app/ai/crisis.py`: `CrisisPlan.lead`, `CrisisResponse.ordered_types`;
- `ai/pipeline.py`: при категории вне строк конвейера вторичные упорядочиваются по `PRIORITY`;
- `workers/pipeline.py`: в БД идёт `ordered_types`, при отсутствии ответа — `None`;
- `repositories/pipeline_work.py`: пустой или неизвестный набор записывается как NULL;
- миграция 0021: `NOT VALID` + `VALIDATE`, первая строка набора сохраняется как передана;
- `tests/test_crisis_blocks.py`, `tests/test_crisis_blocks_a8.py` (сняты два strict-xfail), `tests/test_memory_a8.py`
  (паркуются только истёкшие аренды);
- `docs/DECISIONS.md` (АР-57 п.2, п.4), `docs/BACKLOG.md` (Б-103), `docs/DEPLOY.md` §6.

Решение оператора по A8-0021-1 / ревью MAJOR-1 — вариант «б». Б-103 не меняет ни блоки, ни их порядок ни для одного
ответа классификатора. Меняются только главная строка и тип события.

Источники: ТЗ §2 п.4 и п.7, §7, §9.2 п.3–4, §9.3; ФТ-11; `docs/DECISIONS.md` АР-13, АР-38, АР-45, АР-57; Б-102,
Б-103; карточка 3.4 (вопрос 6). Отчёты раунда 1: `docs/reviews/0021-crisis-blocks-test-r1.md`,
`docs/reviews/0021-crisis-blocks-review-r1.md`.

Продуктовый код не менялся, коммитов нет. БД: кластер 5432, база `feelings_test_a8cb`. Роли `feelings_*` не
менялись, кроме того что делает общая фикстура `migrated_db`.

## 0. Изменение ожидания собственного теста (по решению «б»)

`test_b103_changes_block_order_for_exactly_one_set` (раунд 1) закреплял ошибочное поведение: при
{violence, self_harm, life_threat} и категории `self_harm` порядок блоков менялся. После исправления A1 тест красный,
как и должен. Он **удалён и заменён** на `test_b103_no_block_change_for_any_verdict_in_any_order`, где ожидание —
«расхождений с поведением до Б-103 нет». Эталон снят независимо от текущего кода (п. 1 ниже). Тест
`test_b103_does_not_change_blocks_as_decision_says` (раунд 1, strict-xfail) теперь зелёный. Его `_b103_diff` перебирает
все 651 ответ, а не 112, и упорядочивает вторичные так же, как конвейер. `test_empty_types_is_unknown_set` тоже
зелёный, он расширен (п. 6).

## 1. Независимый эталон до Б-103

Скрипт A8 (вне репозитория, в рабочей папке сессии) загружает `crisis.py` из `git show 1df7495~1` отдельным модулем.
Прежний конвейер воспроизведён так: `types` — категории в `MESSAGE_CRISIS`, `primary` — `MESSAGE_CRISIS.get(category)`.
Перебраны **651 ответ классификатора**: 6 категорий × любое подмножество прочих × **любой порядок** вторичных.
Каждый ответ прошёл через текущий `pipeline.process`. Результат:

```
verdict orderings 651 withheld 648 mismatches 0
distinct stored (ordered, main): 24
```

Сравнивались `text`, `actions` и `template_version` ответа автору, а также набор признаков. Лента проверялась так:
`couple_messages.crisis_blocks(главная, хранимый массив)`, где первая строка — `ordered_types[0]`, а остальные идут в
**любом** порядке (все перестановки). Её результат совпал с `notice.blocks`.

Самопроверка эталона: тот же скрипт на коде `HEAD` (`git archive HEAD`, без исправлений A1) находит расхождения ровно
у `self_harm` + {violence, life_threat} (± `underage`) во всех порядках. Это дефект раунда 1.

Эталон закреплён литералом `BEFORE_B103` (32 строки «набор × главная до Б-103») в
`backend/tests/test_crisis_blocks_a8.py`, поэтому тест от истории git не зависит. Эталон совпадает с таблицей
`crisisCases.ts` раунда 1.

## 2. Прогоны

| Команда | Итог |
| --- | --- |
| `make backend-lint` | ruff `All checks passed!`, `234 files already formatted`, mypy `Success: no issues found in 98 source files` |
| `mypy tests/test_crisis_blocks_a8.py tests/test_memory_a8.py` (дополнительно, в `make lint` тесты не входят) | в моих файлах ошибок нет. Есть одна чужая: `tests/contract.py:7` — нет заглушек типов `jsonschema` |
| `pytest tests/test_crisis_blocks*.py tests/test_ai_*.py tests/test_pipeline*.py tests/test_memory_a8.py tests/test_b83_a8.py tests/test_rls_a8.py` | `3409 passed, 9 xfailed in 104.82s` (9 xfailed — прежние, в других файлах; в моих файлах xfail нет) |
| `pytest -m isolation --isolation-min=auto` | `isolation: собрано 354 (минимум 354), пропущено 0, xfail 0`, `354 passed, 6317 deselected` |
| `pytest -q`, полный backend | `6653 passed, 2 skipped, 16 xfailed, 2 warnings in 939.49s` (16 xfailed — прежние, оба strict-xfail A8 сняты) |
| `pytest tests/test_crisis_blocks_a8.py tests/test_memory_a8.py tests/test_crisis_blocks.py` (после правки типов в тесте) | `137 passed in 36.47s` |
| `npm run lint` (frontend) | eslint и `tsc -b` без ошибок, `Типы API соответствуют api/openapi.yaml.` |
| `npx vitest run` | `Test Files 69 passed (69)`, `Tests 1771 passed (1771)` |

## 3. Критерий → тест/команда → результат

| # | Критерий (источник) | Тест / команда | Результат |
| --- | --- | --- | --- |
| 1 | Б-103 не меняет блоки и их порядок ни для одного ответа классификатора, меняется только главная (АР-57 п.4, решение «б»; §9.3) | `test_b103_no_block_change_for_any_verdict_in_any_order`: 651 ответ через настоящий `pipeline.process`. `notice.blocks` = `BEFORE_B103[(набор, главная до Б-103)]`. Главная — `life_threat` при этом признаке, иначе прежняя. Доставлено 3, остановлено 648. Плюс независимый скрипт по `1df7495~1` (раздел 1): текст, действия и версия шаблона совпали | ✅ 648/648, расхождений 0 |
| 2 | Лента по хранимому набору даёт тот же порядок (АР-57 п.1, п.3) | тот же тест: хранимый массив `_stored(ordered_types)` (первая — как передана, остальные — по перечислению, как пишет 0021), остальные строки во всех перестановках, `crisis_blocks` = эталон. `test_worker_stores_lead_first_and_feed_matches`: по одному ответу на каждый из 24 различных хранимых наборов прошёл через настоящий воркер и БД. Массив в БД **ровно** `_stored(...)` с порядком, `withheld_crisis` = главная, `read_page` автора = эталон, получатель не видит ни остановленных, ни блоков. Каждое третье сообщение несёт инструкцию в тексте | ✅ |
| 3 | Первая строка ≠ главной только в сочетаниях Б-103 и равна категории модели | тот же тест: `leads == LEAD_KEYS`, 4 случая: {v,l}@v, {s,l}@s, {v,s,l}@v, {v,s,l}@s | ✅ |
| 4 | Хранимая «первая строка» не открывает новых путей: любой порядок массива даёт только прежний ответ | `test_any_stored_order_gives_only_pre_b103_answers`: все наборы × главная × **все** перестановки массива, в том числе такие, каких конвейер не пишет. Лента = `BEFORE_B103[(набор, главная)]`, а при главной `life_threat` без угрозы отправителя и первой `violence`/`self_harm` — `BEFORE_B103[(набор, первая)]`. Порядок массива выбирает только между ответами, которые прежний конвейер уже давал на этот набор | ✅ |
| 5 | Недоверенный порядок от модели на ответ не влияет (§9.2 п.3) | п. 1: все порядки вторичных дают эталон. Первая строка — категория модели (`SafetyVerdict.categories = (category, *secondary)`), она и до Б-103 задавала главную. Если категория вне строк конвейера (`none`/`underage`), вторичные идут по `PRIORITY` (`pipeline.py:422-425`). Писать массив может только `feelings_worker` через функцию (раунд 1: `test_no_role_reads_or_writes_column`, 42501 у `feelings_app`) | ✅ |
| 6 | Исключение `lead` не задевает чат и черновики (`withheld=False`) | `test_chat_and_drafts_order_is_not_significant`: 5 строк, включая `underage`, × все перестановки. Без главной блоки равны блокам по упорядоченному набору. С главной первой (так передают `dialog.py:415`, `chat.py:596`) `lead is None`. Вызовы без главной — `chat.py:396,481,565`, `drafts.py:134` | ✅ |
| 7 | Функция 0021 сохраняет первую строку, CHECK принимает (АР-57 п.2) | `test_function_keeps_first_as_passed`, 4 случая под `feelings_worker`. Повтор первой дальше по массиву схлопывается, а первая остаётся первой. Первая `threat_outgoing` при главной `life_threat`: правило «А», порядок не значим. Первая — главная. Главная не `life_threat`: первая порядок не меняет. Лента = эталон. Раунд 1 `test_check_constraint_cases` (11 случаев) и `test_function_rejects_more_bad_sets` зелёные без правки | ✅ |
| 8 | CHECK `NOT VALID` + `VALIDATE` (ревью MINOR-4) | A1: `test_migration_0021_upgrade_downgrade_upgrade` (`convalidated = true`). A8: `test_downgrade_0021_restores_whole_catalog_of_fresh_0020`: весь каталог `downgrade 0020` = чистая 0020, `upgrade head` = голова | ✅ |
| 9 | MINOR-2: пустой набор → NULL → консервативный набор на клиенте (АР-57 п.3) | `test_empty_types_is_unknown_set` ×3: `()`, `("underage",)`, `("underage","a8_unknown")`. В БД NULL, главный тип записан, в ленте `withheld_crisis_blocks: null`. `test_worker_without_notice_passes_unknown_set`: конвейер подменён, `author_notice=None`, воркер пишет NULL, главная берётся по событиям. Клиент: `crisis.a8.test.ts` «null — сервер не принят, набор по withheld_crisis, источник conservative» (раунд 1) | ✅ |
| 10 | MINOR-6: `release_others` не трогает действующие чужие аренды (Б-102) | `test_release_others_keeps_active_foreign_leases`: аренда на 3 часа и аренда, истекающая через минуту, остаются как были (токен, вид, начало, срок), в запись парковки не попадают, `unpark` их не меняет. Истёкшая аренда и агент без аренды паркуются и возвращаются. При прежнем условии `< now() + 1 hour` тест красный (аренда «через минуту» паркуется). `test_release_others_parks_expired_leases_and_restores` зелёный | ✅ |
| 11 | Страж клиента сравнивает и варианты с разным первым элементом | `crisisCases.ts`: добавлены 4 строки с полем `first` (`types` — в порядке хранения). Среди них «главная life_threat, первым self_harm» — `not_sent, life_threat, self_harm, violence`. `test_frontend_crisis_cases_match_server_table` (async): 36 строк = таблица сервера, то есть 32 «набор × главная» и все хранимые наборы с иной первой, которые пишет конвейер (по 651 ответу). Внутренняя согласованность каждой строки с `first` — `crisis_blocks(main, types) == blocks`. `test_guard_detects_divergence`: подмена блока, пропажа строки, пропажа строки Б-103, снятый `first`, противоречивый порядок — видны | ✅ |
| 12 | Клиент показывает серверный список как есть, в том числе с разным первым | `crisis.a8.test.ts`: 32 строки без `first`, как раньше. Новый тест на 4 строки Б-103: `not_sent, life_threat, <first>` в начале, блоки те же, что у строки «набор × главная», два разных порядка для {v,s,l}. Правила §9.3 проверены по всем 36. `Messages.crisis.a8.test.tsx`: все 36 на экране — ровно этот порядок, вне набора ничего, 112 после блоков. `couple.test.ts` (A4) прогоняет все 36 через `withheldCrisisBlocks` | ✅ |
| 13 | Утечка к партнёру, права, атомарность аренды — без регресса (принцип 4; §9.3; АР-13, АР-38) | тесты раунда 1 в `test_crisis_blocks_a8.py` зелёные без правки. `isolation` 354/354 | ✅ |

## 4. Правки A1 в чужих и моих тестах (рабочая копия)

| Файл | Правка | Вывод |
| --- | --- | --- |
| `test_crisis_blocks_a8.py` (мой) | сняты `xfail(strict=True)` у `test_b103_does_not_change_blocks_as_decision_says` и `test_empty_types_is_unknown_set`. Тела не тронуты | верно: дефекты исправлены, тесты зелёные |
| `test_memory_a8.py` (мой) | `memory_lease_until < now() + interval '1 hour'` → `< now()`, докстрока | соответствует MINOR-6. Закреплено тестом из п. 10 |
| `test_crisis_blocks.py` (A1) | новые `test_b103_keeps_blocks_and_order_for_every_verdict` (V0 и V2, 651 ответ) и `test_b103_lead_only_when_promoted`. В `test_function_normalizes...` ожидание порядка изменено на «первая как передана». В `_state` добавлены ограничения таблицы | не ослаблено. Эталон A1 (`crisis.respond` с прежней главной плюс `_old_plan`) не независим от текущего `plan` — это закрыто эталоном A8 из раздела 1 |

## 5. Дефекты

**[MINOR] A8-0021-4. `docs/DECISIONS.md` АР-57 п.4 ↔ `docs/tasks/3.4.md:338-341` (вопрос 6б).** АР-57 п.4 пишет: «Какой
порядок верен при {violence, self_harm, life_threat} … — вопрос психологу (карточка 3.4, вопрос 6б); до ответа порядок
прежний». В карточке вопрос 6б про порядок блоков ничего не говорит, там только тексты блоков и достаточность
`outgoing_threat_brief`/`author_safety_life`. Вопрос, на который ссылается решение, в карточке не записан. Ожидание:
дописать в вопрос 6б карточки 3.4 строку о порядке поддержки при самоповреждении относительно блока о насилии при
угрозе жизни. Владелец — A0 или A1 через карточку. На код и тесты не влияет.

Наблюдения, которые дефектами не являются:

- MINOR-6: после исправления `release_others` не паркует и аренды, которые истекают во время теста. Если такая
  оставшаяся аренда истечёт в окне теста с `run_once`, воркер может её подхватить. Окно — секунды, раньше на той же
  базе такого не было. Это осознанная цена не трогать чужие действующие аренды.
- Ревью MINOR-3 (`array_position` на двумерном массиве): A1 его не менял. На PostgreSQL 16 по-прежнему 22023 и 23514,
  что закреплено тестами раунда 1.
- `NOT VALID` + `VALIDATE` в одной транзакции Alembic выгоды по блокировке не даёт. A1 сам оговорил это в комментарии
  миграции. На объёме запуска это несущественно.
- Строки 2-элементного набора с `first` (`{v,l}`, `{s,l}`) по блокам совпадают с базовыми. В таблицу они включены,
  потому что это различные хранимые массивы, которые пишет конвейер. Страж требует полного совпадения с сервером.
- В `couple.test.ts` (A4) имена двух случаев `test.each` теперь повторяются: «violence+life_threat@life_threat» и
  «violence+self_harm+life_threat@life_threat» встречаются по два раза, а различаются полем `first`. Vitest это
  допускает. В тестах A8 имя содержит `^first`.

## 6. Новые и изменённые тесты A8 (только тестовые файлы)

- `backend/tests/test_crisis_blocks_a8.py`:
  - `BEFORE_B103`, `VERDICT_ORDERS` (651), `_stored`, `LEAD_KEYS`;
  - тесты: `test_b103_no_block_change_for_any_verdict_in_any_order`, `test_any_stored_order_gives_only_pre_b103_answers`,
    `test_chat_and_drafts_order_is_not_significant`, `test_worker_stores_lead_first_and_feed_matches`,
    `test_function_keeps_first_as_passed` (×4), `test_empty_types_is_unknown_set` (×3),
    `test_worker_without_notice_passes_unknown_set`;
  - страж клиента переписан: строки с `first`, таблица сервера по конвейеру, 36 строк;
  - удалён `test_b103_changes_block_order_for_exactly_one_set` (раздел 0).
- `backend/tests/test_memory_a8.py`: `test_release_others_keeps_active_foreign_leases`.
- `frontend/src/test/crisisCases.ts`: поле `first?` и 4 строки Б-103.
- `frontend/src/couple/crisis.a8.test.ts`: 32 строки без `first`, новый тест на 4 строки Б-103, имена с `^first`.
- `frontend/src/screens/Messages.crisis.a8.test.tsx`: все 36 строк на экране.

## 7. Итоговый полный прогон

Backend: `6653 passed, 2 skipped, 16 xfailed`, падений нет (`TEST_DATABASE_URL=…/feelings_test_a8cb`; в раунде 1 было
`6637 passed, 18 xfailed`). Набор `isolation`: 354/354, пропусков и xfail нет. Frontend: `Tests 1771 passed (1771)`,
lint чистый. Красных тестов нет. Дефекты раунда 1 A8-0021-1 и A8-0021-2 закрыты и закреплены тестами. A8-0021-3 вне
объёма: заведён как Б-105 и закрыт коммитом `c09044a`. `test.fails` в `datetime.a8.test.ts` снят, тест зелёный.

### Дополнение: свежая база (замечание координатора)

`test_downgrade_0021_restores_whole_catalog_of_fresh_0020` падал на свежей базе, как в CI: у `alembic_version` ACL
`NULL` (права владельца по умолчанию), а на переиспользованной базе — явный ACL владельца. Это дефект теста, не
продукта. Исправление: в снимке функций и таблиц NULL-ACL приводится к `acldefault(…, владелец)`. Сравнение остаётся
точным, ни один объект из него не исключён.

| Прогон | Итог |
| --- | --- |
| свежая база, `CI=true`, 6 файлов из замечания | `247 passed` |
| переиспользованная база (`feelings_test_a8cb`), те же 6 файлов | `247 passed` |
| свежая база, `CI=true`, полный backend | `6653 passed, 16 xfailed, 2 errors` |

Обе ошибки полного прогона — в `test_worker_health` (`кластер без проверки пароля (trust)`): при `CI=true` пропуск
становится ошибкой. Это свойство локального кластера с `trust` (CLAUDE.md), с 0021 не связано. Временная база
`feelings_test_a8fresh` удалена.

## 8. Сверка с ТЗ

| ТЗ / решение | Проверено | Итог |
| --- | --- | --- |
| §9.3 — протокол без смягчения, по всему набору, «звони» в начале, поддержка при самоповреждении | п. 1–4, 11–12 | ✅ ответ как до Б-103 по всем 651 ответу |
| §9.3 «не сообщает партнёру»; принцип 4 | п. 2, 13 | ✅ |
| §9.2 п.3 — текст сообщения и порядок от модели — данные | п. 2, 5 | ✅ |
| §9.2 п.4 — набор `isolation` зелёный | раздел 2 | ✅ 354/354 |
| §7 — журналы и набор без текстов | раунд 1, без изменений; колонка — `safety_event_type[]` | ✅ |
| АР-13, АР-38 — строка меняется один раз, атомарно | п. 13 (раунд 1) | ✅ |
| АР-57 п.1–3 — хранение, функция, контракт, NULL → консервативный | п. 2, 7–9 | ✅ |
| АР-57 п.4 — Б-103, решение «б» | п. 1–6 | ✅; ссылка на вопрос 6б — A8-0021-4 (MINOR, документ) |
| Б-102 — парковка только истёкших | п. 10 | ✅ |

Дефекты раунда 1: A8-0021-1 (MAJOR) закрыт решением «б» и подтверждён независимым эталоном. A8-0021-2 (MINOR) закрыт.
Ревью MINOR-6 закрыт. Открыт только A8-0021-4: MINOR, документ, на код не влияет.

ВЕРДИКТ: ПРИНЯТО
