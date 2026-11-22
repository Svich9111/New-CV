"""Долговременная память: удаление владельцем, отпечатки, состояние фоновых задач (задача 2.4, A1).

Revision ID: 0009
Revises: 0008
Create Date: 2026-11-22 18:00:00

ТЗ §2 п.6, §6.3 (все правила), §7, §9.1 п.3, п.5, §9.2; ФТ-12…ФТ-14; DECISIONS.md АР-13, АР-23,
АР-31, АР-35, АР-36; BACKLOG Б-53, Б-54, Б-63, Б-65.

1. **Б-63 — векторы только через функции.** У ``feelings_app`` на ``memory_items``, у
   ``feelings_admin`` на ``kb_chunks`` INSERT и UPDATE — только по колонкам, без ``embedding`` и
   ``embedding_model`` (у памяти — ещё без ``deleted_at``): записать вектор и модель можно только
   ``app_set_memory_embedding`` / ``kb_set_chunk_embedding`` (0008). Обнуление — автоматически:
   триггер ``clear_stale_embedding`` при изменении текста (``memory_items.text_enc``,
   ``kb_chunks.text``) в том же UPDATE обнуляет вектор и модель (вектор старого текста не
   остаётся, АР-35). У ``feelings_app`` на ``agents`` и ``conversations`` INSERT/UPDATE — тоже
   по колонкам, без служебных колонок памяти (п.3): состояние задач меняют только функции.

2. **Удаление записи владельцем и отпечатки** (§2 п.6, §6.3 п.4, ФТ-13).
   ``app_delete_memory(p_item uuid) RETURNS boolean`` — обнуляет ``text_enc``, ``embedding``,
   ``embedding_model``, ``event_date`` (дата события — тоже личное содержание записи), ставит
   ``deleted_at``; ``fingerprint``, тип, источник и уверенность сохраняются. Чужая, уже
   удалённая и несуществующая запись — ``false`` (неразличимо). Политика ``memory_owner``
   (FOR ALL) разделена: ``memory_select`` — свои строки, включая удалённые;
   ``memory_insert``/``memory_update``/``memory_delete`` — только неудалённые (``deleted_at IS
   NULL`` и в WITH CHECK): удалённую запись приложение не восстановит, не изменит, не сотрёт
   физически (отпечаток живёт до удаления аккаунта, АР-23: каскад ``agents → memory_items``).
   ``app_memory_forgotten(p_fingerprints bytea[]) RETURNS TABLE (fingerprint bytea)`` — какие из
   переданных отпечатков принадлежат удалённым записям своего агента. Формат отпечатка — АР-36
   (HMAC-SHA-256 нормализованного текста, считает приложение; ключа в БД нет). Индекс
   ``ix_memory_items_agent_fingerprint (agent_id, fingerprint)``.

3. **Состояние фоновых задач** (Б-54; §6.3 п.1, п.3; догон после перезапуска, без двойной
   работы). Колонки (служебные, вне §7 — АР-36):

   - ``conversations.memory_through_at/_id`` — отметка: реплика, по которую включительно диалог
     обработан извлечением (порядок ``(created_at, id)``, как у сводки 2.1);
   - ``agents.memory_partner_through_at/_id`` — то же для доставленных владельцу сообщений пары
     (ФТ-14, порядок ``(delivered_at, id)``);
   - ``agents.memory_lease_token/_kind/_at/_until``, ``memory_attempts`` — аренда агента одной
     копией воркера (``extract`` или ``compact``: извлечение и сжатие одного агента не идут
     одновременно), число захватов подряд без завершения;
   - ``agents.memory_compacted_at`` — когда (время захвата) прошло последнее сжатие.

   Сессия (Б-54): реплики диалога подряд, пока пауза между соседними не больше ``p_gap``
   (порог — параметр приложения); сессия завершена, если после неё есть реплика через паузу
   больше ``p_gap`` или последняя реплика старше ``now() - p_gap``.

   ``memory_claim_extraction(p_gap, p_lease, p_limit)`` и ``memory_claim_compaction(p_every,
   p_lease, p_limit)`` — ``feelings_worker``: находят агентов с работой, свободных от аренды,
   блокируют строки ``agents`` ``FOR NO KEY UPDATE SKIP LOCKED`` (ничего не ждут — с удалением
   аккаунта, чатом и второй копией воркера не взаимоблокируются) и выдают аренду; отдают
   только id и отметки, без текстов. Обработка — в ``user_scope`` владельца (``feelings_app``,
   RLS): ``app_memory_extraction_done(p_token, p_session_through, p_partner_through)`` и
   ``app_memory_compaction_done(p_token)`` в той же транзакции, что и запись памяти, двигают
   отметки и снимают аренду; ``false`` (аренду перехватили, отметка не своя или не дальше
   текущей) — транзакцию откатить.

4. **Б-65**: ``app_memory_nearest`` считает записи до 1001 (``LIMIT 1001``) вместо ``count(*)``.

Downgrade: функции, триггеры, индексы, колонки 0009 удаляются, права и политика ``memory_owner``
— как в 0008, ``app_memory_nearest`` — тело 0008. Отпечатки и пометки удаления остаются.
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0009"
down_revision: str | Sequence[str] | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _module_0008():  # type: ignore[no-untyped-def]
    path = Path(__file__).with_name("0008_vector_search.py")
    spec = importlib.util.spec_from_file_location("migration_0008", path)
    if spec is None or spec.loader is None:
        raise RuntimeError("нет файла миграции 0008")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


MEMORY_NEAREST_0008: str = _module_0008().MEMORY_NEAREST
_COUNT_0008 = """        SELECT count(*) INTO v_total FROM public.memory_items m
         WHERE m.agent_id = v_agent AND m.deleted_at IS NULL AND m.text_enc IS NOT NULL
           AND m.embedding IS NOT NULL AND m.embedding_model = p_model;"""
_COUNT_0009 = """        -- Б-65: важно только «больше 1000 или нет» — счёт до 1001, а не всех записей.
        SELECT count(*) INTO v_total FROM (
            SELECT 1 FROM public.memory_items m
             WHERE m.agent_id = v_agent AND m.deleted_at IS NULL AND m.text_enc IS NOT NULL
               AND m.embedding IS NOT NULL AND m.embedding_model = p_model
             LIMIT 1001) t;"""
if MEMORY_NEAREST_0008.count(_COUNT_0008) != 1:
    raise RuntimeError("тело app_memory_nearest 0008 не совпадает с ожидаемым")
MEMORY_NEAREST_0009 = MEMORY_NEAREST_0008.replace(_COUNT_0008, _COUNT_0009).replace(
    "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1
)
MEMORY_NEAREST_RESTORE = MEMORY_NEAREST_0008.replace(
    "CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1
)

DELIVERED = "('delivered', 'read', 'answered')"  # как видел владелец (АР-25 п.1)
PARTNER_BATCH = 50  # сообщений пары за один захват извлечения

SCHEMA = (
    "ALTER TABLE agents ADD COLUMN memory_partner_through_at timestamptz",
    "ALTER TABLE agents ADD COLUMN memory_partner_through_id uuid",
    "ALTER TABLE agents ADD COLUMN memory_compacted_at timestamptz",
    "ALTER TABLE agents ADD COLUMN memory_lease_token uuid",
    "ALTER TABLE agents ADD COLUMN memory_lease_kind varchar(8)",
    "ALTER TABLE agents ADD COLUMN memory_lease_at timestamptz",
    "ALTER TABLE agents ADD COLUMN memory_lease_until timestamptz",
    "ALTER TABLE agents ADD COLUMN memory_attempts integer DEFAULT 0 NOT NULL",
    "ALTER TABLE conversations ADD COLUMN memory_through_at timestamptz",
    "ALTER TABLE conversations ADD COLUMN memory_through_id uuid",
    "ALTER TABLE agents ADD CONSTRAINT ck_agents_memory_partner_through_pair "
    "CHECK ((memory_partner_through_at IS NULL) = (memory_partner_through_id IS NULL))",
    "ALTER TABLE agents ADD CONSTRAINT ck_agents_memory_lease_whole "
    "CHECK (memory_lease_token IS NULL OR (memory_lease_kind IS NOT NULL "
    "AND memory_lease_at IS NOT NULL AND memory_lease_until IS NOT NULL))",
    "ALTER TABLE agents ADD CONSTRAINT ck_agents_memory_lease_kind "
    "CHECK (memory_lease_kind IS NULL OR memory_lease_kind IN ('extract', 'compact'))",
    "ALTER TABLE agents ADD CONSTRAINT ck_agents_memory_attempts_nonnegative "
    "CHECK (memory_attempts >= 0)",
    "ALTER TABLE conversations ADD CONSTRAINT ck_conversations_memory_through_pair "
    "CHECK ((memory_through_at IS NULL) = (memory_through_id IS NULL))",
    "CREATE INDEX ix_conversation_messages_conversation_order "
    "ON conversation_messages (conversation_id, created_at, id)",
    "CREATE INDEX ix_partner_messages_recipient_delivered "
    "ON partner_messages (recipient_id, delivered_at, id) WHERE delivered_at IS NOT NULL",
    "CREATE INDEX ix_memory_items_agent_fingerprint ON memory_items (agent_id, fingerprint) "
    "WHERE fingerprint IS NOT NULL",
)

CLEAR_STALE_EMBEDDING = r"""
CREATE FUNCTION clear_stale_embedding() RETURNS trigger
    LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp
    AS $f$
    BEGIN
        -- Б-63, АР-35: вектор старого текста не остаётся; новый пишет только функция 0008.
        IF TG_TABLE_NAME = 'memory_items' THEN
            IF NEW.text_enc IS DISTINCT FROM OLD.text_enc THEN
                NEW.embedding := NULL;
                NEW.embedding_model := NULL;
            END IF;
        ELSIF NEW.text IS DISTINCT FROM OLD.text THEN
            NEW.embedding := NULL;
            NEW.embedding_model := NULL;
        END IF;
        RETURN NEW;
    END $f$
"""

TRIGGERS = (
    "CREATE TRIGGER memory_items_clear_stale_embedding BEFORE UPDATE OF text_enc "
    "ON memory_items FOR EACH ROW EXECUTE FUNCTION clear_stale_embedding()",
    "CREATE TRIGGER kb_chunks_clear_stale_embedding BEFORE UPDATE OF text "
    "ON kb_chunks FOR EACH ROW EXECUTE FUNCTION clear_stale_embedding()",
)

USER_CHECK = """
        IF v_uid IS NULL THEN RAISE EXCEPTION 'no user' USING ERRCODE = '42501'; END IF;
        PERFORM 1 FROM public.users u WHERE u.id = v_uid AND u.deleted_at IS NULL;
        IF NOT FOUND THEN RAISE EXCEPTION 'no user' USING ERRCODE = '42501'; END IF;"""

LEASE_CHECK = """
        IF p_lease IS NULL OR p_lease <= interval '0' OR p_lease > interval '1 day' THEN
            RAISE EXCEPTION 'bad lease' USING ERRCODE = '22023';
        END IF;"""


def _fill(template: str) -> str:
    """Подставить общие фрагменты (без f-строк: тексты функций — константы миграции)."""
    for name, value in (
        ("USER_CHECK", USER_CHECK),
        ("LEASE_CHECK", LEASE_CHECK),
        ("DELIVERED", DELIVERED),
        ("PARTNER_BATCH", str(PARTNER_BATCH)),
    ):
        template = template.replace(f"@{name}@", value)
    return template


DELETE_MEMORY = _fill(
    r"""
CREATE FUNCTION app_delete_memory(p_item uuid) RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE v_uid uuid := public.app_uid();
    BEGIN@USER_CHECK@
        -- Чужая, уже удалённая или несуществующая запись — false (одинаково).
        UPDATE public.memory_items m
           SET text_enc = NULL, embedding = NULL, embedding_model = NULL, event_date = NULL,
               deleted_at = now()
          FROM public.agents a
         WHERE m.id = p_item AND a.id = m.agent_id AND a.user_id = v_uid
           AND m.deleted_at IS NULL;
        RETURN FOUND;
    END $f$
"""
)

MEMORY_FORGOTTEN = _fill(
    r"""
CREATE FUNCTION app_memory_forgotten(p_fingerprints bytea[])
    RETURNS TABLE (fingerprint bytea)
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    #variable_conflict use_column
    DECLARE v_uid uuid := public.app_uid();
    BEGIN@USER_CHECK@
        IF cardinality(p_fingerprints) > 1000 THEN
            RAISE EXCEPTION 'too many fingerprints' USING ERRCODE = '22023';
        END IF;
        RETURN QUERY
        SELECT DISTINCT m.fingerprint FROM public.memory_items m
          JOIN public.agents a ON a.id = m.agent_id
         WHERE a.user_id = v_uid AND m.deleted_at IS NOT NULL
           AND m.fingerprint = ANY (COALESCE(p_fingerprints, '{}'));
    END $f$
"""
)


CLAIM_EXTRACTION = _fill(
    r"""
CREATE FUNCTION memory_claim_extraction(p_gap interval, p_lease interval, p_limit integer)
    RETURNS TABLE (user_id uuid, agent_id uuid, token uuid, attempts integer,
                   conversation_id uuid, session_after_at timestamptz, session_after_id uuid,
                   session_through_at timestamptz, session_through_id uuid,
                   partner_after_at timestamptz, partner_after_id uuid,
                   partner_through_at timestamptz, partner_through_id uuid)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    #variable_conflict use_column
    DECLARE
        v_lim integer := LEAST(GREATEST(COALESCE(p_limit, 1), 1), 100);
        v_cut timestamptz;
        v_n integer := 0;
        r record;
    BEGIN
        IF p_gap IS NULL OR p_gap < interval '1 minute' OR p_gap > interval '7 days' THEN
            RAISE EXCEPTION 'bad gap' USING ERRCODE = '22023';
        END IF;@LEASE_CHECK@
        v_cut := now() - p_gap;
        -- Кандидаты: свободные агенты живых владельцев, у которых после отметки есть реплика
        -- старше паузы или доставленное сообщение пары старше паузы. Строки, занятые чатом,
        -- удалением аккаунта или другой копией воркера, пропускаются (SKIP LOCKED).
        FOR r IN
            SELECT a.id AS agent, a.user_id AS uid,
                   a.memory_partner_through_at AS pat, a.memory_partner_through_id AS pid
              FROM public.agents a JOIN public.users u ON u.id = a.user_id
             WHERE u.deleted_at IS NULL
               AND (a.memory_lease_token IS NULL OR a.memory_lease_until < now())
               AND (EXISTS (
                        SELECT 1 FROM public.conversations c
                          JOIN public.conversation_messages m ON m.conversation_id = c.id
                         WHERE c.agent_id = a.id AND m.created_at < v_cut
                           AND (c.memory_through_at IS NULL
                                OR (m.created_at, m.id) > (c.memory_through_at,
                                                           c.memory_through_id)))
                    OR EXISTS (
                        SELECT 1 FROM public.partner_messages pm
                         WHERE pm.recipient_id = a.user_id AND pm.delivered_at < v_cut
                           AND pm.status IN @DELIVERED@
                           AND (a.memory_partner_through_at IS NULL
                                OR (pm.delivered_at, pm.id) > (a.memory_partner_through_at,
                                                               a.memory_partner_through_id))))
             ORDER BY a.memory_lease_until NULLS FIRST, a.id
             FOR NO KEY UPDATE OF a SKIP LOCKED
        LOOP
            user_id := r.uid;
            agent_id := r.agent;
            conversation_id := NULL; session_after_at := NULL; session_after_id := NULL;
            session_through_at := NULL; session_through_id := NULL;
            partner_through_at := NULL; partner_through_id := NULL;
            partner_after_at := r.pat;
            partner_after_id := r.pid;
            -- Первая завершённая необработанная сессия (самый ранний диалог с такой сессией).
            SELECT c.id, c.memory_through_at, c.memory_through_id, x.created_at, x.id
              INTO conversation_id, session_after_at, session_after_id,
                   session_through_at, session_through_id
              FROM public.conversations c
             CROSS JOIN LATERAL (
                    SELECT y.id, y.created_at FROM (
                        SELECT m.id, m.created_at,
                               lead(m.created_at) OVER (ORDER BY m.created_at, m.id) AS nxt
                          FROM public.conversation_messages m
                         WHERE m.conversation_id = c.id
                           AND (c.memory_through_at IS NULL
                                OR (m.created_at, m.id) > (c.memory_through_at,
                                                           c.memory_through_id))) y
                     WHERE (y.nxt IS NULL AND y.created_at < v_cut) OR y.nxt - y.created_at > p_gap
                     ORDER BY y.created_at, y.id
                     LIMIT 1) x
             WHERE c.agent_id = r.agent
             ORDER BY c.started_at, c.id
             LIMIT 1;
            -- Доставленные владельцу сообщения пары после отметки: до @PARTNER_BATCH@ самых ранних.
            SELECT q.delivered_at, q.id INTO partner_through_at, partner_through_id
              FROM (SELECT pm.delivered_at, pm.id FROM public.partner_messages pm
                     WHERE pm.recipient_id = r.uid AND pm.delivered_at < v_cut
                       AND pm.status IN @DELIVERED@
                       AND (r.pat IS NULL OR (pm.delivered_at, pm.id) > (r.pat, r.pid))
                     ORDER BY pm.delivered_at, pm.id
                     LIMIT @PARTNER_BATCH@) q
             ORDER BY q.delivered_at DESC, q.id DESC
             LIMIT 1;
            IF session_through_id IS NULL AND partner_through_id IS NULL THEN
                CONTINUE;  -- сессия ещё идёт: аренду не выдаём
            END IF;
            token := gen_random_uuid();
            UPDATE public.agents a
               SET memory_lease_token = token, memory_lease_kind = 'extract',
                   memory_lease_at = now(), memory_lease_until = now() + p_lease,
                   memory_attempts = a.memory_attempts + 1
             WHERE a.id = r.agent
            RETURNING a.memory_attempts INTO attempts;
            RETURN NEXT;
            v_n := v_n + 1;
            EXIT WHEN v_n >= v_lim;
        END LOOP;
    END $f$
"""
)

CLAIM_COMPACTION = _fill(
    r"""
CREATE FUNCTION memory_claim_compaction(p_every interval, p_lease interval, p_limit integer)
    RETURNS TABLE (user_id uuid, agent_id uuid, token uuid, attempts integer,
                   compacted_at timestamptz)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    #variable_conflict use_column
    DECLARE
        v_lim integer := LEAST(GREATEST(COALESCE(p_limit, 1), 1), 100);
        r record;
    BEGIN
        IF p_every IS NULL OR p_every < interval '1 minute' THEN
            RAISE EXCEPTION 'bad period' USING ERRCODE = '22023';
        END IF;@LEASE_CHECK@
        -- Пора сжимать (не было или старше периода) и с прошлого сжатия память менялась:
        -- новая или изменённая запись, удаление владельцем (сводка профиля — без удалённого,
        -- АР-31).
        FOR r IN
            SELECT a.id AS agent, a.user_id AS uid, a.memory_compacted_at AS done
              FROM public.agents a JOIN public.users u ON u.id = a.user_id
             WHERE u.deleted_at IS NULL
               AND (a.memory_lease_token IS NULL OR a.memory_lease_until < now())
               AND (a.memory_compacted_at IS NULL OR a.memory_compacted_at < now() - p_every)
               AND EXISTS (
                    SELECT 1 FROM public.memory_items m
                     WHERE m.agent_id = a.id
                       AND (a.memory_compacted_at IS NULL
                            OR COALESCE(m.updated_at, m.created_at) >= a.memory_compacted_at
                            OR m.deleted_at >= a.memory_compacted_at))
             ORDER BY a.memory_compacted_at NULLS FIRST, a.id
             LIMIT v_lim
             FOR NO KEY UPDATE OF a SKIP LOCKED
        LOOP
            user_id := r.uid;
            agent_id := r.agent;
            compacted_at := r.done;
            token := gen_random_uuid();
            UPDATE public.agents a
               SET memory_lease_token = token, memory_lease_kind = 'compact',
                   memory_lease_at = now(), memory_lease_until = now() + p_lease,
                   memory_attempts = a.memory_attempts + 1
             WHERE a.id = r.agent
            RETURNING a.memory_attempts INTO attempts;
            RETURN NEXT;
        END LOOP;
    END $f$
"""
)

EXTRACTION_DONE = _fill(
    r"""
CREATE FUNCTION app_memory_extraction_done(p_token uuid, p_session_through uuid,
                                           p_partner_through uuid) RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE
        v_uid uuid := public.app_uid();
        v_agent uuid;
        v_pat timestamptz; v_pid uuid;
        v_conv uuid; v_at timestamptz; v_cat timestamptz; v_cid uuid;
        v_dat timestamptz;
    BEGIN@USER_CHECK@
        IF p_token IS NULL THEN RETURN false; END IF;
        -- Порядок блокировок (АР-13): агент → диалог; пользователя не блокирует.
        SELECT a.id, a.memory_partner_through_at, a.memory_partner_through_id
          INTO v_agent, v_pat, v_pid
          FROM public.agents a
         WHERE a.user_id = v_uid AND a.memory_lease_token = p_token
           AND a.memory_lease_kind = 'extract'
           FOR NO KEY UPDATE;
        IF NOT FOUND THEN RETURN false; END IF;  -- аренду перехватили или она не своя
        IF p_session_through IS NOT NULL THEN
            SELECT c.id, m.created_at, c.memory_through_at, c.memory_through_id
              INTO v_conv, v_at, v_cat, v_cid
              FROM public.conversation_messages m
              JOIN public.conversations c ON c.id = m.conversation_id
             WHERE m.id = p_session_through AND c.agent_id = v_agent
               FOR NO KEY UPDATE OF c;
            IF NOT FOUND OR (v_cat IS NOT NULL AND (v_at, p_session_through) <= (v_cat, v_cid))
            THEN
                RETURN false;
            END IF;
        END IF;
        IF p_partner_through IS NOT NULL THEN
            SELECT pm.delivered_at INTO v_dat FROM public.partner_messages pm
             WHERE pm.id = p_partner_through AND pm.recipient_id = v_uid
               AND pm.delivered_at IS NOT NULL AND pm.status IN @DELIVERED@;
            IF NOT FOUND OR (v_pat IS NOT NULL AND (v_dat, p_partner_through) <= (v_pat, v_pid))
            THEN
                RETURN false;
            END IF;
        END IF;
        IF v_conv IS NOT NULL THEN
            UPDATE public.conversations
               SET memory_through_at = v_at, memory_through_id = p_session_through
             WHERE id = v_conv;
        END IF;
        UPDATE public.agents a
           SET memory_lease_token = NULL, memory_lease_kind = NULL, memory_attempts = 0,
               memory_partner_through_at = COALESCE(v_dat, a.memory_partner_through_at),
               memory_partner_through_id = CASE WHEN v_dat IS NULL
                                                THEN a.memory_partner_through_id
                                                ELSE p_partner_through END
         WHERE a.id = v_agent;
        RETURN true;
    END $f$
"""
)

COMPACTION_DONE = _fill(
    r"""
CREATE FUNCTION app_memory_compaction_done(p_token uuid) RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE v_uid uuid := public.app_uid();
    BEGIN@USER_CHECK@
        -- Отметка — время захвата: изменения памяти во время сжатия войдут в следующее.
        UPDATE public.agents a
           SET memory_compacted_at = a.memory_lease_at, memory_lease_token = NULL,
               memory_lease_kind = NULL, memory_attempts = 0
         WHERE a.user_id = v_uid AND a.memory_lease_token = p_token
           AND a.memory_lease_kind = 'compact';
        RETURN FOUND;
    END $f$
"""
)

APP_SIGNATURES = (
    "app_delete_memory(uuid)",
    "app_memory_forgotten(bytea[])",
    "app_memory_extraction_done(uuid, uuid, uuid)",
    "app_memory_compaction_done(uuid)",
)
WORKER_SIGNATURES = (
    "memory_claim_extraction(interval, interval, integer)",
    "memory_claim_compaction(interval, interval, integer)",
)

MEMORY_COLUMNS_INSERT = (
    "id, agent_id, type, text_enc, event_date, source, source_message_id, confidence, "
    "fingerprint, created_at, updated_at"
)
MEMORY_COLUMNS_UPDATE = (
    "agent_id, type, text_enc, event_date, source, source_message_id, confidence, "
    "fingerprint, updated_at"
)
AGENT_COLUMNS = "user_id, prompt_version_id, profile_summary_enc, tone_settings"
CONVERSATION_COLUMNS = "agent_id, started_at, summary_enc"
KB_CHUNK_COLUMNS = "document_id, text, topic_tags, problem_level"

OWN_AGENT = "EXISTS (SELECT 1 FROM agents a WHERE a.id = agent_id)"

GRANTS = (
    "REVOKE ALL ON FUNCTION clear_stale_embedding() FROM PUBLIC",
    *(f"REVOKE ALL ON FUNCTION {sig} FROM PUBLIC" for sig in APP_SIGNATURES + WORKER_SIGNATURES),
    *(f"GRANT EXECUTE ON FUNCTION {sig} TO feelings_app" for sig in APP_SIGNATURES),
    *(f"GRANT EXECUTE ON FUNCTION {sig} TO feelings_worker" for sig in WORKER_SIGNATURES),
    # Б-63: векторы, пометка удаления и состояние задач — только функциями.
    "REVOKE INSERT, UPDATE ON memory_items FROM feelings_app",
    f"GRANT INSERT ({MEMORY_COLUMNS_INSERT}) ON memory_items TO feelings_app",
    f"GRANT UPDATE ({MEMORY_COLUMNS_UPDATE}) ON memory_items TO feelings_app",
    "REVOKE INSERT, UPDATE ON agents FROM feelings_app",
    f"GRANT INSERT (id, {AGENT_COLUMNS}, created_at) ON agents TO feelings_app",
    f"GRANT UPDATE ({AGENT_COLUMNS}) ON agents TO feelings_app",
    "REVOKE INSERT, UPDATE ON conversations FROM feelings_app",
    f"GRANT INSERT (id, {CONVERSATION_COLUMNS}) ON conversations TO feelings_app",
    f"GRANT UPDATE ({CONVERSATION_COLUMNS}) ON conversations TO feelings_app",
    "REVOKE INSERT, UPDATE ON kb_chunks FROM feelings_admin",
    f"GRANT INSERT (id, {KB_CHUNK_COLUMNS}) ON kb_chunks TO feelings_admin",
    f"GRANT UPDATE ({KB_CHUNK_COLUMNS}) ON kb_chunks TO feelings_admin",
    # Удалённая запись (отпечаток) приложению не изменить, не восстановить и не стереть.
    "DROP POLICY IF EXISTS memory_owner ON memory_items",
    f"CREATE POLICY memory_select ON memory_items FOR SELECT TO feelings_app USING ({OWN_AGENT})",
    "CREATE POLICY memory_insert ON memory_items FOR INSERT TO feelings_app "
    f"WITH CHECK ({OWN_AGENT} AND deleted_at IS NULL)",
    "CREATE POLICY memory_update ON memory_items FOR UPDATE TO feelings_app "
    f"USING ({OWN_AGENT} AND deleted_at IS NULL) WITH CHECK ({OWN_AGENT} AND deleted_at IS NULL)",
    "CREATE POLICY memory_delete ON memory_items FOR DELETE TO feelings_app "
    f"USING ({OWN_AGENT} AND deleted_at IS NULL)",
)

DOWNGRADE_GRANTS = (
    "DROP POLICY IF EXISTS memory_delete ON memory_items",
    "DROP POLICY IF EXISTS memory_update ON memory_items",
    "DROP POLICY IF EXISTS memory_insert ON memory_items",
    "DROP POLICY IF EXISTS memory_select ON memory_items",
    f"CREATE POLICY memory_owner ON memory_items FOR ALL TO feelings_app "
    f"USING ({OWN_AGENT}) WITH CHECK ({OWN_AGENT})",
    # REVOKE по таблице снимает и права на колонки.
    "REVOKE INSERT, UPDATE ON kb_chunks FROM feelings_admin",
    "GRANT INSERT, UPDATE ON kb_chunks TO feelings_admin",
    "REVOKE INSERT, UPDATE ON conversations FROM feelings_app",
    "GRANT INSERT, UPDATE ON conversations TO feelings_app",
    "REVOKE INSERT, UPDATE ON agents FROM feelings_app",
    "GRANT INSERT, UPDATE ON agents TO feelings_app",
    "REVOKE INSERT, UPDATE ON memory_items FROM feelings_app",
    "GRANT INSERT, UPDATE ON memory_items TO feelings_app",
)


def upgrade() -> None:
    # По одной команде: asyncpg не выполняет несколько команд в одном запросе.
    for stmt in SCHEMA:
        op.execute(stmt)
    for fn in (
        CLEAR_STALE_EMBEDDING,
        DELETE_MEMORY,
        MEMORY_FORGOTTEN,
        CLAIM_EXTRACTION,
        CLAIM_COMPACTION,
        EXTRACTION_DONE,
        COMPACTION_DONE,
        MEMORY_NEAREST_0009,
    ):
        op.execute(fn)
    for stmt in TRIGGERS:
        op.execute(stmt)
    for stmt in GRANTS:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE_GRANTS:
        op.execute(stmt)
    op.execute(MEMORY_NEAREST_RESTORE)
    op.execute("DROP TRIGGER IF EXISTS kb_chunks_clear_stale_embedding ON kb_chunks")
    op.execute("DROP TRIGGER IF EXISTS memory_items_clear_stale_embedding ON memory_items")
    for sig in reversed(APP_SIGNATURES + WORKER_SIGNATURES):
        op.execute(f"DROP FUNCTION IF EXISTS {sig}")
    op.execute("DROP FUNCTION IF EXISTS clear_stale_embedding()")
    op.execute("DROP INDEX IF EXISTS ix_memory_items_agent_fingerprint")
    op.execute("DROP INDEX IF EXISTS ix_partner_messages_recipient_delivered")
    op.execute("DROP INDEX IF EXISTS ix_conversation_messages_conversation_order")
    for table, column in (
        ("conversations", "memory_through_id"),
        ("conversations", "memory_through_at"),
        ("agents", "memory_attempts"),
        ("agents", "memory_lease_until"),
        ("agents", "memory_lease_at"),
        ("agents", "memory_lease_kind"),
        ("agents", "memory_lease_token"),
        ("agents", "memory_compacted_at"),
        ("agents", "memory_partner_through_id"),
        ("agents", "memory_partner_through_at"),
    ):
        # Колонки уходят вместе со своими CHECK.
        op.execute(f"ALTER TABLE {table} DROP COLUMN IF EXISTS {column}")
