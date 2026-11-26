# Трассировка: требование ТЗ → задача → тесты

Заполняется A0 при закрытии каждой задачи. Пустая колонка «Тесты» = требование не подтверждено.

| ФТ | Требование (кратко) | Задача | Тесты | Статус |
| --- | --- | --- | --- | --- |
| ФТ-01 | Авторизация Telegram, проверка подписи initData | 1.1 | `test_telegram_init_data.py`, `test_auth_api.py`, `test_session.py`, `test_auth_a8.py`; клиент 1.5 — `frontend/src/api/client*.test.ts`, `telegram/webapp.test.ts` | ✅ |
| ФТ-02 | Онбординг, согласия, 18+ | 1.2 | `test_onboarding.py`, `test_onboarding_a8.py`, `test_contract.py`; клиент 1.5 — `screens/Onboarding.test.tsx`, `app/App*.test.tsx` | ✅ (⚠️ АР-14) |
| ФТ-03 | Ссылка-приглашение 7 дней, подтверждение обеими сторонами | 0.7b, 1.3, 1.4 | `test_couple_functions*.py`, `test_couple_api*.py` | 🔄 БД, API, бот ✅; живой бот — Г2 |
| ФТ-04 | Одна активная пара, выход, повторное создание | 0.7b, 1.3 | `test_couple_functions*.py`, `test_couple_api*.py` | ✅ |
| ФТ-05 | Чат с потоковым выводом | 2.1, 2.2 | `test_chat_*.py` | ✅ сервер (2.1), экран (2.2); ⛔ телефон — Г2 |
| ФТ-06 | Голосовые с расшифровкой | 2.3 | | ⬜ |
| ФТ-07 | Черновик: генерация, правка, перегенерация, отказ | 3.1, 3.4 | `test_drafts_*.py`, `test_drafts_feed_a8.py`; экран — `screens/Draft.test.tsx` (3.4, план) | ✅ сервер; ⬜ экран — 3.4 |
| ФТ-08 | Отправка только по подтверждению | 3.1, 3.4 | `test_drafts_api.py`, `test_drafts_feed_a8.py`; экран — страж «без „Отправить“ нет `send`» (3.4, план) | ✅ сервер; ⬜ экран — 3.4 |
| ФТ-09 | Проверка и адаптация входящего, пометка | 3.2, 3.4 | `test_pipeline_*.py`; пометка на экране — `screens/Messages.test.tsx` (3.4, план) | ✅ код (качество — после прогона на модели); ⬜ экран — 3.4 |
| ФТ-10 | Оригинал по запросу | 3.3, 3.4 | `test_couple_messages_api.py`, `test_drafts_feed_a8.py` | ✅ сервер; ⬜ экран — 3.4 |
| ФТ-11 | Лента пары и статусы | 3.3, 3.4 | `test_couple_messages_api.py`, `test_couple_feed_db.py` | ✅ сервер; ⬜ экран — 3.4 |
| ФТ-12 | Долговременная память | 2.4 | `test_memory_*.py` | ✅ код (качество — после кредитов) |
| ФТ-13 | Экран памяти, удаление | 2.4 (сервер), 2.5 | `test_memory_api.py` (удаление — проверка строки в БД), `test_memory_a8*.py`; экран — `screens/Memory*.test.tsx`, `api/memory*.test.ts` (2.5) | ✅ сервер, экран; ⛔ телефон — Г2 |
| ФТ-14 | Память из входящих — только одобренные сообщения | 2.4 | `test_memory_worker.py`, `test_memory_a8*.py` | ✅ |
| ФТ-15 | База знаний | 2.0v, 2.6 | `test_kb_*.py`, `test_admin_kb_api.py`, `test_vector_search_*.py` | ✅ код (модель эмбеддингов — Б-66) |
| ФТ-16 | Уровень проблемы 1–5 | 2.7 | | ⬜ |
| ФТ-17 | Кризисный протокол | 4.1 | | ⬜ |
| ФТ-18 | Уведомления без текста | 1.4 (каркас), 3.5 | `test_bot.py`, `test_bot_a8.py`, `test_notify_worker.py`, `test_notify_a8.py`, `test_notify_release_db.py`, `test_notify_release_a8.py` (0015: снятие аренды уведомления без расхода попытки, АР-46) | ✅ (АР-47 п.3 — оператор) |
| ФТ-19 | Подписка на пару, пробный период | 4.4 | | ⬜ |
| ФТ-20 | Админ-панель | 2.6 (API БЗ), 2.8 | `test_admin_kb_api.py`, `test_kb_db.py`, `test_kb_a8.py` (API БЗ); промпты, метрики, экраны — 2.8 | 🔄 API БЗ ✅; ⬜ панель, промпты (АР-53), метрики (АР-54, Б-4) |
| ФТ-21 | Удаление аккаунта, выгрузка | 4.5 | `test_account_delete*.py`, `test_account_export_db.py`, `test_account_api*.py` | ✅ |
| ТЗ §7 | 17 таблиц, RLS, шифрование | 0.7, 4.2 | `test_migrations.py`, `test_rls_*.py`, `test_crypto*.py` | ✅ |
| ТЗ §9.2 | Изоляция агентов, тесты при каждом выпуске | 0.7, 2.1, 2.4, 3.1, 3.2, 3.5, 4.3 | `test_rls_*.py`, `test_chat_context.py`, `test_chat_a8*.py`, `test_drafts_ai.py`, `test_pipeline_a8.py`, `test_memory_worker.py`, `test_notify_a8.py` (перечень — `tasks/4.3.md`) | 🔄 по каналам ✅; ⬜ единый набор `-m isolation` в CI, сквозной сценарий, внедрение в память/ответ/БЗ — 4.3 (АР-55) |
