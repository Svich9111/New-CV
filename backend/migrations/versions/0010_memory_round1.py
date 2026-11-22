"""Память, раунд 1 ревью 2.4: сжатие после удаления, захват без лишних блокировок (A1).

Revision ID: 0010
Revises: 0009
Create Date: 2026-11-22 22:00:00

ТЗ §2 п.6, §6.3 п.3–4, §7; ФТ-13, ФТ-14; DECISIONS.md АР-13, АР-36, АР-37, АР-38; ревью 2.4
MINOR-4, MINOR-7; BACKLOG Б-68.

1. **АР-37 п.1 — сжатие после удаления вне периода.** Колонка
   ``agents.memory_compaction_requested_at`` (служебная, прав у приложения нет):
   ``app_delete_memory`` в той же транзакции ставит её (``clock_timestamp()``) агенту удалённой
   записи; ``memory_claim_compaction`` выдаёт агента с отметкой независимо от ``p_every`` и от
   изменений с прошлого сжатия; ``app_memory_compaction_done`` снимает отметку, если она не новее
   времени захвата (удаление во время сжатия оставит отметку — следующее сжатие). Удаление,
   поставившее отметку, держит строку агента до фиксации, а захват строки пропускает
   (``SKIP LOCKED``): выданное сжатие всегда видит удаление, отметка которого не новее захвата.
   Порядок блокировок (ревью 2.4 р.2 MAJOR-1) — единый: **агент → записи памяти → диалоги**, как
   каскад ``app_delete_account`` (пользователь → агент → память, диалоги). ``app_delete_memory``
   первым шагом берёт строку агента ``FOR NO KEY UPDATE``, ``app_set_memory_embedding`` (тело
   0008) — ``FOR KEY SHARE``; удаление записи и удаление аккаунта идут по очереди, без 40P01.
2. **MINOR-4 — захват извлечения.** Кандидаты отбираются без блокировки (завершённая
   необработанная сессия или доставленное сообщение текущей пары старше паузы), строка агента
   блокируется ``FOR NO KEY UPDATE SKIP LOCKED`` по одному id — только у тех, кому выдаётся
   аренда; агенты с идущей сессией не блокируются (чат их не ждёт).
3. **ФТ-14 (АР-37).** ``partner_through`` захвата и проверка ``app_memory_extraction_done`` —
   только сообщения текущей активной пары владельца (``couple_members.active`` и
   ``couples.status = 'active'``, как контекст 2.1). Сообщения бывшей пары не выдаются; отметка
   общая на агента, поэтому сообщения новой пары её двигают дальше.
4. **Б-68 / MINOR-7.** CHECK ``ck_memory_items_text_has_fingerprint``: ``text_enc IS NULL OR
   fingerprint IS NOT NULL`` — у каждой записи с текстом есть отпечаток, удаление всегда
   оставляет след.

Downgrade: тела функций 0009, колонка и CHECK удаляются.
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0010"
down_revision: str | Sequence[str] | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _module(name: str) -> ModuleType:
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(f"migration_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"нет файла миграции {name}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


M0009 = _module("0009_memory.py")
USER_CHECK: str = M0009.USER_CHECK
LEASE_CHECK: str = M0009.LEASE_CHECK
DELIVERED: str = M0009.DELIVERED
PARTNER_BATCH: int = M0009.PARTNER_BATCH


def _replace(sql: str) -> str:
    return sql.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)


M0008 = _module("0008_vector_search.py")
RESTORE_0008 = (_replace(M0008.SET_MEMORY_EMBEDDING),)
RESTORE_0009 = tuple(
    _replace(fn)
    for fn in (
        M0009.DELETE_MEMORY,
        M0009.CLAIM_EXTRACTION,
        M0009.CLAIM_COMPACTION,
        M0009.EXTRACTION_DONE,
        M0009.COMPACTION_DONE,
    )
)

# Сообщения текущей активной пары владельца (параметр — выражение с id владельца).
CURRENT_COUPLE = """pm.couple_id IN (
                        SELECT cm.couple_id FROM public.couple_members cm
                          JOIN public.couples cc ON cc.id = cm.couple_id
                         WHERE cm.user_id = @UID@ AND cm.active AND cc.status = 'active')"""


def _fill(template: str) -> str:
    """Подставить общие фрагменты (без f-строк: тексты функций — константы миграции)."""
    for name, value in (
        ("USER_CHECK", USER_CHECK),
        ("LEASE_CHECK", LEASE_CHECK),
        ("DELIVERED", DELIVERED),
        ("PARTNER_BATCH", str(PARTNER_BATCH)),
        ("COUPLE_A", CURRENT_COUPLE.replace("@UID@", "a.user_id")),
        ("COUPLE_R", CURRENT_COUPLE.replace("@UID@", "r.uid")),
        ("COUPLE_V", CURRENT_COUPLE.replace("@UID@", "v_uid")),
    ):
        template = template.replace(f"@{name}@", value)
    return template


SCHEMA = (
    "ALTER TABLE agents ADD COLUMN memory_compaction_requested_at timestamptz",
    "ALTER TABLE memory_items ADD CONSTRAINT ck_memory_items_text_has_fingerprint "
    "CHECK (text_enc IS NULL OR fingerprint IS NOT NULL)",
)

DELETE_MEMORY = _fill(
    r"""
CREATE OR REPLACE FUNCTION app_delete_memory(p_item uuid) RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE v_uid uuid := public.app_uid(); v_agent uuid;
    BEGIN@USER_CHECK@
        -- Порядок блокировок (АР-38, ревью 2.4 р.2 MAJOR-1): агент → записи памяти → диалоги,
        -- как каскад app_delete_account (пользователь → агент → память). Удаление аккаунта,
        -- уже взявшее агента, — ждём его; после ожидания агента нет — false.
        SELECT a.id INTO v_agent FROM public.agents a WHERE a.user_id = v_uid
           FOR NO KEY UPDATE;
        IF NOT FOUND THEN RETURN false; END IF;
        -- Чужая, уже удалённая или несуществующая запись — false (одинаково).
        UPDATE public.memory_items m
           SET text_enc = NULL, embedding = NULL, embedding_model = NULL, event_date = NULL,
               deleted_at = now()
         WHERE m.id = p_item AND m.agent_id = v_agent AND m.deleted_at IS NULL;
        IF NOT FOUND THEN RETURN false; END IF;
        -- АР-37 п.1: сводка профиля пересобирается без удалённого при ближайшем сжатии, вне
        -- периода (строка агента уже заблокирована).
        UPDATE public.agents SET memory_compaction_requested_at = clock_timestamp()
         WHERE id = v_agent;
        RETURN true;
    END $f$
"""
)

SET_MEMORY_EMBEDDING = _fill(
    r"""
CREATE OR REPLACE FUNCTION app_set_memory_embedding(p_item uuid, p_vec text, p_model text)
    RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE
        v_uid uuid := public.app_uid();
        v public.vector;
        v_agent uuid;
    BEGIN@USER_CHECK@
        v := public.embedding_arg(p_vec, p_model);
        -- АР-38: агент (FOR KEY SHARE — не мешает чату и другим записям памяти, но ждёт
        -- удаление аккаунта) → запись памяти.
        SELECT a.id INTO v_agent FROM public.agents a WHERE a.user_id = v_uid FOR KEY SHARE;
        IF NOT FOUND THEN RETURN false; END IF;
        -- Чужая, удалённая, без текста или несуществующая запись — false (одинаково).
        UPDATE public.memory_items m SET embedding = v, embedding_model = p_model
         WHERE m.id = p_item AND m.agent_id = v_agent
           AND m.deleted_at IS NULL AND m.text_enc IS NOT NULL;
        RETURN FOUND;
    END $f$
"""
)

CLAIM_EXTRACTION = _fill(
    r"""
CREATE OR REPLACE FUNCTION memory_claim_extraction(p_gap interval, p_lease interval,
                                                   p_limit integer)
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
        v_pat timestamptz;
        v_pid uuid;
        r record;
    BEGIN
        IF p_gap IS NULL OR p_gap < interval '1 minute' OR p_gap > interval '7 days' THEN
            RAISE EXCEPTION 'bad gap' USING ERRCODE = '22023';
        END IF;@LEASE_CHECK@
        v_cut := now() - p_gap;
        -- MINOR-4: кандидаты — без блокировки и только с готовой работой (завершённая сессия
        -- или доставленное сообщение текущей пары старше паузы); идущие сессии не трогаются.
        FOR r IN
            SELECT a.id AS agent, a.user_id AS uid
              FROM public.agents a JOIN public.users u ON u.id = a.user_id
             WHERE u.deleted_at IS NULL
               AND (a.memory_lease_token IS NULL OR a.memory_lease_until < now())
               AND (EXISTS (
                        SELECT 1 FROM public.conversations c
                         CROSS JOIN LATERAL (
                            SELECT 1 FROM (
                                SELECT m.created_at,
                                       lead(m.created_at) OVER (ORDER BY m.created_at, m.id)
                                           AS nxt
                                  FROM public.conversation_messages m
                                 WHERE m.conversation_id = c.id
                                   AND (c.memory_through_at IS NULL
                                        OR (m.created_at, m.id) > (c.memory_through_at,
                                                                   c.memory_through_id))) y
                             WHERE (y.nxt IS NULL AND y.created_at < v_cut)
                                OR y.nxt - y.created_at > p_gap
                             LIMIT 1) x
                         WHERE c.agent_id = a.id
                           AND EXISTS (
                                SELECT 1 FROM public.conversation_messages m0
                                 WHERE m0.conversation_id = c.id AND m0.created_at < v_cut
                                   AND (c.memory_through_at IS NULL
                                        OR (m0.created_at, m0.id) > (c.memory_through_at,
                                                                     c.memory_through_id))))
                    OR EXISTS (
                        SELECT 1 FROM public.partner_messages pm
                         WHERE pm.recipient_id = a.user_id AND pm.delivered_at < v_cut
                           AND pm.status IN @DELIVERED@
                           AND @COUPLE_A@
                           AND (a.memory_partner_through_at IS NULL
                                OR (pm.delivered_at, pm.id) > (a.memory_partner_through_at,
                                                               a.memory_partner_through_id))))
             ORDER BY a.memory_lease_until NULLS FIRST, a.id
             LIMIT v_lim * 4
        LOOP
            -- Блокировка только выдаваемого: занят чатом, удалением аккаунта или другой
            -- копией воркера — пропуск; после ожидания аренда перепроверяется по свежей строке.
            SELECT a.memory_partner_through_at, a.memory_partner_through_id INTO v_pat, v_pid
              FROM public.agents a
             WHERE a.id = r.agent
               AND (a.memory_lease_token IS NULL OR a.memory_lease_until < now())
               FOR NO KEY UPDATE SKIP LOCKED;
            IF NOT FOUND THEN CONTINUE; END IF;
            user_id := r.uid;
            agent_id := r.agent;
            conversation_id := NULL; session_after_at := NULL; session_after_id := NULL;
            session_through_at := NULL; session_through_id := NULL;
            partner_through_at := NULL; partner_through_id := NULL;
            partner_after_at := v_pat;
            partner_after_id := v_pid;
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
            -- Сообщения текущей пары, доставленные владельцу после отметки (ФТ-14, АР-37).
            SELECT q.delivered_at, q.id INTO partner_through_at, partner_through_id
              FROM (SELECT pm.delivered_at, pm.id FROM public.partner_messages pm
                     WHERE pm.recipient_id = r.uid AND pm.delivered_at < v_cut
                       AND pm.status IN @DELIVERED@
                       AND @COUPLE_R@
                       AND (v_pat IS NULL OR (pm.delivered_at, pm.id) > (v_pat, v_pid))
                     ORDER BY pm.delivered_at, pm.id
                     LIMIT @PARTNER_BATCH@) q
             ORDER BY q.delivered_at DESC, q.id DESC
             LIMIT 1;
            IF session_through_id IS NULL AND partner_through_id IS NULL THEN
                CONTINUE;  -- работу успели сделать между отбором и блокировкой
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
CREATE OR REPLACE FUNCTION memory_claim_compaction(p_every interval, p_lease interval,
                                                   p_limit integer)
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
        -- Удаление записи владельцем (АР-37 п.1) — сразу, вне периода. Иначе — пора сжимать и
        -- память с прошлого сжатия менялась.
        FOR r IN
            SELECT a.id AS agent, a.user_id AS uid, a.memory_compacted_at AS done
              FROM public.agents a JOIN public.users u ON u.id = a.user_id
             WHERE u.deleted_at IS NULL
               AND (a.memory_lease_token IS NULL OR a.memory_lease_until < now())
               AND (a.memory_compaction_requested_at IS NOT NULL
                    OR ((a.memory_compacted_at IS NULL
                         OR a.memory_compacted_at < now() - p_every)
                        AND EXISTS (
                            SELECT 1 FROM public.memory_items m
                             WHERE m.agent_id = a.id
                               AND (a.memory_compacted_at IS NULL
                                    OR COALESCE(m.updated_at, m.created_at)
                                       >= a.memory_compacted_at
                                    OR m.deleted_at >= a.memory_compacted_at))))
             ORDER BY a.memory_compaction_requested_at NULLS LAST,
                      a.memory_compacted_at NULLS FIRST, a.id
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
CREATE OR REPLACE FUNCTION app_memory_extraction_done(p_token uuid, p_session_through uuid,
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
            -- ФТ-14 (АР-37): только сообщение текущей активной пары, доставленное владельцу.
            SELECT pm.delivered_at INTO v_dat FROM public.partner_messages pm
             WHERE pm.id = p_partner_through AND pm.recipient_id = v_uid
               AND pm.delivered_at IS NOT NULL AND pm.status IN @DELIVERED@
               AND @COUPLE_V@;
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
CREATE OR REPLACE FUNCTION app_memory_compaction_done(p_token uuid) RETURNS boolean
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE v_uid uuid := public.app_uid();
    BEGIN@USER_CHECK@
        -- Отметка — время захвата: изменения памяти во время сжатия войдут в следующее.
        -- Отметка «нужно сжатие» снимается, только если удаление было до захвата (АР-37 п.1).
        UPDATE public.agents a
           SET memory_compacted_at = a.memory_lease_at, memory_lease_token = NULL,
               memory_lease_kind = NULL, memory_attempts = 0,
               memory_compaction_requested_at =
                   CASE WHEN a.memory_compaction_requested_at <= a.memory_lease_at THEN NULL
                        ELSE a.memory_compaction_requested_at END
         WHERE a.user_id = v_uid AND a.memory_lease_token = p_token
           AND a.memory_lease_kind = 'compact';
        RETURN FOUND;
    END $f$
"""
)


def upgrade() -> None:
    for stmt in SCHEMA:
        op.execute(stmt)
    for fn in (
        DELETE_MEMORY,
        SET_MEMORY_EMBEDDING,
        CLAIM_EXTRACTION,
        CLAIM_COMPACTION,
        EXTRACTION_DONE,
        COMPACTION_DONE,
    ):
        op.execute(fn)


def downgrade() -> None:
    for fn in RESTORE_0009 + RESTORE_0008:
        op.execute(fn)
    op.execute(
        "ALTER TABLE memory_items DROP CONSTRAINT IF EXISTS ck_memory_items_text_has_fingerprint"
    )
    op.execute("ALTER TABLE agents DROP COLUMN IF EXISTS memory_compaction_requested_at")
