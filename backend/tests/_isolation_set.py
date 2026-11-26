"""Набор выпуска «изоляция агентов и устойчивость к инструкциям» (задача 4.3; ТЗ §9.2 п.4).

Существующие тесты помечаются маркером ``isolation`` отсюда (``conftest.pytest_collection_modifyitems``),
без правки их тел и файлов; новые файлы ``test_isolation_release*.py`` помечены ``pytestmark``.

Запись — префикс id теста: ``файл::`` — весь файл, ``файл::имя`` — тест и все его параметры,
``файл::test_injection_`` — все тесты с этим началом имени. Запись, которая ничего не собрала из
собранного файла (тест переименован или удалён), — ошибка сбора: набор не худеет молча.

Шаг CI ``pytest -m isolation --isolation-min=N`` (A7): падает при 0 собранных, при числе меньше
``N`` и при любом пропуске (``skip``) — «нет БД» не считается зелёным."""

from __future__ import annotations

# Число тестов набора, зафиксированное при закрытии 4.3 (карточка, критерий §9.2 п.4). Шаг CI
# передаёт его в --isolation-min; уменьшать — только вместе с карточкой и решением A0.
ISOLATION_MIN = 349

ISOLATION_SET: tuple[str, ...] = (
    # RLS: личные таблицы видит только владелец; партнёр, бывший, посторонний, аноним — нет
    "tests/test_rls_isolation.py::",
    "tests/test_rls_a8.py::",
    "tests/test_rls_messages.py::",
    # сообщения пары — только функциями; без «Отправить» строки нет (ФТ-08, АР-11)
    "tests/test_drafts_feed_a8.py::test_no_path_without_send_creates_partner_message",
    # контекст ИИ (чат) — только данные владельца; изоляция обеспечивает БД (§6.1, §9.2 п.1)
    "tests/test_chat_context.py::test_end_to_end_model_requests_have_no_foreign_data",
    "tests/test_chat_context.py::test_isolation_by_database_not_by_code",
    "tests/test_chat_a8.py::test_isolation_is_enforced_by_database_not_code",
    "tests/test_chat_api_a8.py::test_isolation_partner_ex_outsider",
    "tests/test_chat_api_a8.py::test_background_compression_does_not_leak",
    "tests/test_chat_api.py::test_partner_does_not_see_or_get_other_chat",
    "tests/test_chat_a8_round2.py::test_pair_via_drafts_isolation",
    "tests/test_chat_a8_round2.py::test_pair_incoming_never_reads_partner_draft",
    # контекст черновика — только владелец
    "tests/test_drafts_ai.py::test_generate_draft_owner_only_context_and_encrypted",
    # конвейер получателя — только одобренный оригинал (§6.2)
    "tests/test_pipeline_a8.py::test_isolation_no_foreign_data_in_any_model_request",
    "tests/test_ai_pipeline_a8.py::test_owner_context_has_no_partner_fields",
    # память (§6.3 п.5)
    "tests/test_memory_worker.py::test_isolation_partner_chat_memory_and_undelivered",
    "tests/test_memory_worker.py::test_partner_call_source_set_by_code",
    "tests/test_memory_a8.py::test_extraction_isolation",
    "tests/test_vector_search_a8.py::test_memory_isolation_and_exact_order",
    "tests/test_memory_api.py::test_partner_and_outsider_neither_see_nor_delete",
    # лента, оригинал, выгрузка
    "tests/test_couple_messages_api.py::test_feed_isolation_outsider_and_ex_partner",
    "tests/test_account_api.py::test_export_isolation_partner_and_stranger",
    "tests/test_account_api_a8.py::test_export_isolation_oracle_for_every_person",
    "tests/test_account_export_db.py::test_isolation_partner_stranger_anonymous",
    # шифрование: чужой ключ/контекст — ошибка, не текст
    "tests/test_crypto.py::",
    "tests/test_crypto_a8.py::test_outsider_and_partner_do_not_read_foreign_texts",
    # текст — данные, не инструкция (§9.2 п.3)
    "tests/test_ai_pipeline.py::test_message_text_is_data_not_instruction",
    "tests/test_ai_pipeline_a8.py::test_injection_",
    "tests/test_pipeline_worker.py::test_prompt_injection_does_not_change_code_decisions",
    "tests/test_pipeline_a8.py::test_injection_honest_model_",
    "tests/test_pipeline_a8.py::test_injection_obedient_model_",
    "tests/test_chat_a8.py::test_instructions_in_texts_stay_data",
    "tests/test_ai_round1.py::test_injection_through_hint_is_dropped",
    "tests/test_ai_round2.py::test_hint_injection_situation_and_check",
    "tests/test_notify_a8.py::test_injected_text_never_reaches_telegram_on_any_retry",
    "tests/test_account_api_a8.py::test_injection_text_is_data",
    "tests/test_memory_worker.py::test_memory_prompts_are_drafts_with_data_rule",
    # база знаний: метки — данные, запрет цитирования кодом (§6.4)
    "tests/test_kb_index.py::test_kb_tag_prompt_is_draft",
    "tests/test_kb_index.py::test_stream_reply_blocks_quote_before_showing",
    "tests/test_kb_index.py::test_quote_blocked_in_agent_crisis_reply",
    "tests/test_kb_index.py::test_product_dialog_reply_also_blocks_quotes",
    # модель не пересказывает чужое (§9.2 п.2): ситуации есть, проверки срабатывают на заглушке
    # (прогон на настоящей модели — ⛔ кредиты, гейт Г4/Г5, не в CI)
    "tests/test_ai_dialog.py::test_partner_private_context_never_reaches_model",
    "tests/test_kb_index.py::test_eval_no_quote_check_catches_quote",
    "tests/test_evals.py::test_privacy_situations_have_canaries",
    "tests/test_evals.py::test_injection_situations_embed_instruction_to_agent",
    "tests/test_evals.py::test_request_contains_only_owner_data",
    "tests/test_evals.py::test_delivery_step_gets_only_original",
    "tests/test_evals.py::test_owner_steps_do_not_see_approved_original",
    "tests/test_evals.py::test_privacy_markers_ignore_negation",
    "tests/test_evals_a8.py::test_fact_privacy_canaries_are_not_in_owner_data",
    "tests/test_evals_a8.py::test_fact_injection_situations_contain_instruction",
    "tests/test_evals_a8.py::test_model_input_contains_only_owner_data",
    "tests/test_evals_a8.py::test_echo_leak_is_caught_by_markers",
    "tests/test_evals_a8.py::test_mutation_broken_private_filter_is_detected",
    "tests/test_evals_a8.py::test_owner_private_chat_in_partner_adaptation_is_caught",
    "tests/test_evals_a8.py::test_r2_deliver_gets_only_original",
    "tests/test_evals_a8.py::test_r2_respond_only_author_leak_is_caught",
    "tests/test_evals_a8.py::test_r2_converse_request_has_no_approved_original",
    "tests/test_evals_a8.py::test_r3_privacy_canary_ignores_negation",
)
