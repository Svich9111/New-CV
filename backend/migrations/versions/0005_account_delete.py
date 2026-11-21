"""Удаление аккаунта: app_delete_account(), отзыв прямых DELETE (задача 4.5, этап 1, A1).

Revision ID: 0005
Revises: 0004
Create Date: 2026-11-21 23:30:00

ФТ-21; ТЗ §4.5 п.3, §7 («удаление в течение 30 дней, включая резервные копии»), §9.1 п.6;
DECISIONS.md АР-13, АР-23; BACKLOG Б-3, Б-19, Б-20.

1. ``app_delete_account() RETURNS void`` — SECURITY DEFINER, только для текущего пользователя
   (``app.user_id``). Удаление полное и сразу (АР-23):

   - строка пользователя блокируется первой (``FOR UPDATE``; нет, неизвестен или помечен
     ``deleted_at`` — ``42501``, как в остальных функциях пары);
   - выход из активной/ожидающей пары — вызовом ``app_leave_couple()``, то есть тем же
     путём и в том же порядке блокировок (пользователь → приглашения → пара, АР-13): пара
     распускается для обоих, ``active``-приглашения отзываются, в ``audit_log`` —
     ``couple.leave``. Если партнёр успел выйти раньше (``FB007`` после ожидания блокировки
     пары), удаление продолжается: пара уже распущена;
   - ссылки других строк на сообщения удаляемого разрываются заранее, до каскада:
     ``partner_messages.reply_to_id`` и ``message_drafts.reply_to_id`` партнёра → NULL,
     записи памяти партнёра, созданные из сообщений удаляемого
     (``memory_items.source_message_id``, тип ``from_partner``; CHECK не допускает NULL), —
     удаляются. Это те же действия, что выполнил бы каскад FK, но строки партнёра
     блокируются раньше строк удаляемого — в том же порядке, что у конвейера
     (``pipeline_deliver``: ответ → исходное сообщение), без взаимоблокировки с ним;
   - ``audit_log`` обезличивается: ``actor_id`` и ``object_id``, равные id удаляемого, → NULL
     (таблица без FK на ``users``, каскад её не затрагивает);
   - ``DELETE FROM users``; каскадом FK удаляются агент, диалоги и их реплики, память,
     черновики, членство в парах, созданные им приглашения (в т.ч. действующие — Б-20),
     написанные им сообщения пары, события безопасности. ``SET NULL``: сообщения партнёра
     удаляемому (``partner_messages.recipient_id`` — остаются у автора), приглашения, которые он
     принял (``invites.used_by``), плательщик подписки пары (``subscriptions.payer_id``);
   - пары, в которых после этого не осталось ни одного участника (ожидавшая партнёра пара;
     пара, второй участник которой удалил аккаунт раньше), удаляются вместе с подпиской и
     платежами (каскад FK): они больше ничьи, а пара без участников нарушала бы инвариант
     «у пары есть участник». Пара, в которой остался бывший партнёр, остаётся (в ней нет id
     пользователей): партнёру видна история (допущение Д7) — его собственные сообщения —
     а подписка и платежи пары остаются для учёта (``payer_id`` → NULL);
   - запись ``audit_log``: ``account.delete``, ``object_type = 'user'``, ``actor_id`` и
     ``object_id`` — NULL. Запись подтверждает, что удаление выполнено (кто удалён — не
     хранится: id после удаления ни на что не указывает, а оставленный id был бы
     персональным идентификатором в журнале на весь срок хранения; ТЗ §9.1 п.5).

   Повторный вход тем же Telegram ID (``app_upsert_user``) после удаления создаёт нового
   пользователя: прежней строки нет (Б-3).

2. ``app_send_draft`` (0001) — тот же контракт и коды (``FA001``…``FA004``, ``42501``), новый
   порядок блокировок: строки пользователей (свой, затем получателя) ``FOR KEY SHARE`` →
   черновик ``FOR UPDATE`` → пара (ключевая блокировка при вставке). Раньше отправка
   блокировала черновик и пару (FK вставки), а строку получателя — последней, в обратном
   порядке относительно ``app_delete_account`` / ``app_leave_couple`` получателя
   (пользователь → пара): одновременная отправка и удаление получателя заканчивались
   взаимоблокировкой (40P01) или ошибкой FK (23503) вместо ``FA003``. ``FOR KEY SHARE`` совместима с другими отправками и
   изменением профиля, но ждёт удаления аккаунта (``FOR UPDATE``). После ожидания пара и
   получатель перечитываются: получатель удалён или вышел — ``FA003``. Неизвестный или
   помеченный удалённым отправитель — ``42501`` (раньше — ``FA001``: черновиков у него нет).

3. Б-19. У ``feelings_app`` отзывается ``DELETE ON users``: прямое удаление строки каскадом
   оставляло пару ``active`` с одним участником. Политика ``users_self`` (``FOR ALL``) заменена
   на ``users_select`` (SELECT) и ``users_update`` (UPDATE) с тем же условием: разрешающей
   политики для DELETE нет, и при случайном возврате права удаление отклонит RLS.

4. Б-38 (ревью 4.2, MAJOR-1). Оригинал сообщения пары расшифровывается с AAD черновика
   (``message_drafts|text_enc|<draft_id>``; ``partner_messages.draft_id`` — FK ``SET NULL``):
   удаление отправленного черновика или агента (каскадом — его черновиков) делало оригинал
   нечитаемым навсегда (ТЗ §7, ФТ-10, принцип 3). Теперь у ``feelings_app``:

   - политика ``drafts_delete`` не пропускает черновики в статусе ``sent`` (удалить можно
     неотправленный и остановленный безопасностью — на них сообщения не ссылаются);
     изменить отправленный черновик нельзя и раньше (``drafts_update``);
   - ``DELETE ON agents`` отозван; ``agents_owner`` (``FOR ALL``) заменена на
     ``agents_select``, ``agents_insert``, ``agents_update`` с тем же условием.

   Агент и черновики удаляются только вместе с аккаунтом (``app_delete_account``, каскад от
   ``users``) — и тогда же удаляются сообщения удаляемого, ссылавшиеся на эти черновики
   (АР-23). Сообщения партнёра ссылаются на черновики партнёра и удалением не затрагиваются.

Коды ошибок новых и изменённых функций — DECISIONS.md, АР-13 и АР-23.

Downgrade возвращает права DELETE на ``users`` и ``agents``, политики ``users_self``,
``agents_owner`` и ``drafts_delete`` из 0001, определение
``app_send_draft`` из 0001 и удаляет ``app_delete_account``.
"""

import importlib.util
from collections.abc import Sequence
from pathlib import Path
from types import ModuleType

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0005"
down_revision: str | Sequence[str] | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _rev(name: str) -> ModuleType:
    """Ревизия-источник общих фрагментов (0003) и определений для downgrade (0001)."""
    path = Path(__file__).with_name(name)
    spec = importlib.util.spec_from_file_location(f"_rev_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"не найдена ревизия: {path}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --LOCK_USER-- — фрагмент 0003: 42501, если пользователя нет или он помечен удалённым;
# строка пользователя блокируется FOR UPDATE первой (АР-13).
DELETE_ACCOUNT = r"""
CREATE FUNCTION app_delete_account() RETURNS void
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE v_uid uuid := public.app_uid(); v_onboarded boolean; v_couples uuid[];
    BEGIN
--LOCK_USER--
        v_couples := ARRAY(SELECT cm.couple_id FROM public.couple_members cm
                            WHERE cm.user_id = v_uid);
        -- 1. Пара — тем же путём, что выход (АР-13: пользователь → приглашения → пара).
        BEGIN
            PERFORM public.app_leave_couple();
        EXCEPTION WHEN SQLSTATE 'FB007' THEN
            NULL;  -- не в паре, или партнёр вышел раньше: пара уже распущена
        END;
        -- 2. Ссылки партнёра на сообщения удаляемого — до каскада (строки партнёра первыми).
        UPDATE public.partner_messages SET reply_to_id = NULL
         WHERE reply_to_id IN (SELECT m.id FROM public.partner_messages m
                                WHERE m.author_id = v_uid)
           AND author_id <> v_uid;
        UPDATE public.message_drafts SET reply_to_id = NULL
         WHERE reply_to_id IN (SELECT m.id FROM public.partner_messages m
                                WHERE m.author_id = v_uid);
        DELETE FROM public.memory_items
         WHERE source_message_id IN (SELECT m.id FROM public.partner_messages m
                                      WHERE m.author_id = v_uid);
        -- 3. Журнал без персональных данных: id удаляемого не остаётся (ТЗ §9.1 п.5).
        UPDATE public.audit_log SET actor_id = NULL WHERE actor_id = v_uid;
        UPDATE public.audit_log SET object_id = NULL WHERE object_id = v_uid;
        -- 4. Строка пользователя; остальное — каскадом FK (CASCADE / SET NULL).
        DELETE FROM public.users WHERE id = v_uid;
        -- 5. Пары, в которых не осталось участников (ожидавшая партнёра; партнёр удалил
        --    аккаунт раньше), — вместе с их подпиской и платежами (АР-23).
        DELETE FROM public.couples c
         WHERE c.id = ANY (v_couples)
           AND NOT EXISTS (SELECT 1 FROM public.couple_members cm WHERE cm.couple_id = c.id);
        -- 6. Факт удаления, без указания, кого (АР-23).
        INSERT INTO public.audit_log (actor_id, action, object_type, object_id)
        VALUES (NULL, 'account.delete', 'user', NULL);
    END $f$
"""

SEND_DRAFT = r"""
CREATE OR REPLACE FUNCTION app_send_draft(p_draft uuid, p_expected_version integer) RETURNS uuid
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE
        v_uid uuid := public.app_uid();
        d record; v_couple uuid; v_recipient uuid; v_locked uuid; v_id uuid;
    BEGIN
        IF v_uid IS NULL THEN RAISE EXCEPTION 'no user' USING ERRCODE = '42501'; END IF;
        -- Блокировки в порядке АР-13: пользователи → черновик → пара. FOR KEY SHARE не мешает
        -- другим отправкам и правке профиля, но ждёт удаления аккаунта (FOR UPDATE).
        PERFORM 1 FROM public.users WHERE id = v_uid AND deleted_at IS NULL FOR KEY SHARE;
        IF NOT FOUND THEN RAISE EXCEPTION 'no user' USING ERRCODE = '42501'; END IF;
        SELECT cm.user_id INTO v_recipient FROM public.couple_members cm
            JOIN public.couples c ON c.id = cm.couple_id AND c.status = 'active'
            JOIN public.couple_members me ON me.couple_id = cm.couple_id
                 AND me.user_id = v_uid AND me.active
            WHERE cm.active AND cm.user_id <> v_uid;
        IF v_recipient IS NOT NULL THEN
            SELECT u.id INTO v_locked FROM public.users u
                WHERE u.id = v_recipient FOR KEY SHARE;
        END IF;
        SELECT md.* INTO d FROM public.message_drafts md
            JOIN public.agents a ON a.id = md.agent_id
            WHERE md.id = p_draft AND a.user_id = v_uid FOR UPDATE OF md;
        IF NOT FOUND OR d.status NOT IN ('draft', 'approved') THEN
            RAISE EXCEPTION 'draft not sendable' USING ERRCODE = 'FA001';
        END IF;
        IF d.version <> p_expected_version THEN
            RAISE EXCEPTION 'version conflict' USING ERRCODE = 'FA002';
        END IF;
        -- Перечитать после ожиданий: получатель мог выйти из пары или удалить аккаунт.
        v_couple := NULL; v_recipient := NULL;
        SELECT cm.couple_id INTO v_couple FROM public.couple_members cm
            JOIN public.couples c ON c.id = cm.couple_id
            WHERE cm.user_id = v_uid AND cm.active AND c.status = 'active';
        SELECT cm.user_id INTO v_recipient FROM public.couple_members cm
            WHERE cm.couple_id = v_couple AND cm.active AND cm.user_id <> v_uid;
        IF v_couple IS NULL OR v_recipient IS NULL
           OR v_recipient IS DISTINCT FROM v_locked THEN
            RAISE EXCEPTION 'no active couple' USING ERRCODE = 'FA003';
        END IF;
        -- Статус «отвечено» ставит конвейер при доставке ответа (pipeline_deliver), а не здесь:
        -- иначе остановленный безопасностью ответ выдал бы срабатывание партнёру (ТЗ §9.3).
        IF d.reply_to_id IS NOT NULL AND NOT EXISTS (
            SELECT 1 FROM public.partner_messages
             WHERE id = d.reply_to_id AND recipient_id = v_uid AND couple_id = v_couple
               AND status IN ('delivered', 'read', 'answered')) THEN
            RAISE EXCEPTION 'bad reply target' USING ERRCODE = 'FA004';
        END IF;
        INSERT INTO public.partner_messages
            (couple_id, author_id, recipient_id, reply_to_id, draft_id, original_enc, status)
        VALUES (v_couple, v_uid, v_recipient, d.reply_to_id, d.id, d.text_enc, 'processing')
        RETURNING id INTO v_id;
        UPDATE public.message_drafts SET status = 'sent', updated_at = now() WHERE id = d.id;
        RETURN v_id;
    END $f$
"""

UPGRADE_GRANTS = (
    "REVOKE ALL ON FUNCTION app_delete_account() FROM PUBLIC",
    "GRANT EXECUTE ON FUNCTION app_delete_account() TO feelings_app",
    # Б-19: удаление строки users — только функцией, распускающей пару.
    "REVOKE DELETE ON users FROM feelings_app",
    "DROP POLICY IF EXISTS users_self ON users",
    """CREATE POLICY users_select ON users FOR SELECT TO feelings_app
    USING (id = app_uid())""",
    """CREATE POLICY users_update ON users FOR UPDATE TO feelings_app
    USING (id = app_uid()) WITH CHECK (id = app_uid())""",
    # Б-38: оригинал сообщения расшифровывается с контекстом черновика — отправленный черновик
    # и агента (каскадом — его черновики) приложение не удаляет; только app_delete_account.
    "DROP POLICY IF EXISTS drafts_delete ON message_drafts",
    """CREATE POLICY drafts_delete ON message_drafts FOR DELETE TO feelings_app
    USING (EXISTS (SELECT 1 FROM agents a WHERE a.id = agent_id) AND status <> 'sent')""",
    "REVOKE DELETE ON agents FROM feelings_app",
    "DROP POLICY IF EXISTS agents_owner ON agents",
    """CREATE POLICY agents_select ON agents FOR SELECT TO feelings_app
    USING (user_id = app_uid())""",
    """CREATE POLICY agents_insert ON agents FOR INSERT TO feelings_app
    WITH CHECK (user_id = app_uid())""",
    """CREATE POLICY agents_update ON agents FOR UPDATE TO feelings_app
    USING (user_id = app_uid()) WITH CHECK (user_id = app_uid())""",
)

# Права и политики 0001 — для downgrade (текст политики совпадает с 0001).
DOWNGRADE_GRANTS = (
    "DROP POLICY IF EXISTS agents_update ON agents",
    "DROP POLICY IF EXISTS agents_insert ON agents",
    "DROP POLICY IF EXISTS agents_select ON agents",
    """CREATE POLICY agents_owner ON agents FOR ALL TO feelings_app
    USING (user_id = app_uid()) WITH CHECK (user_id = app_uid())""",
    "GRANT DELETE ON agents TO feelings_app",
    "DROP POLICY IF EXISTS drafts_delete ON message_drafts",
    """CREATE POLICY drafts_delete ON message_drafts FOR DELETE TO feelings_app
    USING (EXISTS (SELECT 1 FROM agents a WHERE a.id = agent_id))""",
    "DROP POLICY IF EXISTS users_update ON users",
    "DROP POLICY IF EXISTS users_select ON users",
    """CREATE POLICY users_self ON users FOR ALL TO feelings_app
    USING (id = app_uid()) WITH CHECK (id = app_uid())""",
    "GRANT DELETE ON users TO feelings_app",
    "DROP FUNCTION IF EXISTS app_delete_account()",
)


def _send_draft_0001() -> str:
    """Определение app_send_draft из SECURITY_SQL ревизии 0001 (CREATE OR REPLACE)."""
    rev = _rev("0001_initial_schema.py")
    for stmt in rev._statements(rev.SECURITY_SQL):
        if stmt.startswith("CREATE FUNCTION app_send_draft("):
            return stmt.replace("CREATE FUNCTION", "CREATE OR REPLACE FUNCTION", 1)
    raise RuntimeError("в 0001 не найдено определение app_send_draft")


def upgrade() -> None:
    # По одной команде: asyncpg не выполняет несколько команд в одном запросе.
    op.execute(_rev("0003_couple_functions_fixes.py")._expand(DELETE_ACCOUNT))
    op.execute(SEND_DRAFT)
    for stmt in UPGRADE_GRANTS:
        op.execute(stmt)


def downgrade() -> None:
    for stmt in DOWNGRADE_GRANTS:
        op.execute(stmt)
    op.execute(_send_draft_0001())
