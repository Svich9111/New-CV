"""Подписка на пару, пробный период, оплата Telegram Stars (задача 4.4, этап 1, A1).

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-26 18:00:00

ФТ-19; ТЗ §4 (роли: «управляет подпиской»), §7 (``subscriptions``, ``payments``), §8 «Подписка»,
§11 п.1–8; DECISIONS.md Д6, Д9, АР-12, АР-13, АР-23, АР-24, АР-38, АР-50.

**Модель (АР-50).** ``subscriptions`` — строка на период доступа пары: пробный (``plan =
'trial'``, ``status = 'trial'``) или оплаченный (``plan`` = ``month``/``year``, ``status =
'active'``); возврат — ``status = 'canceled'``. ``expired`` в строках не хранится: истечение —
``period_end <= now()``, его вычисляют функции статуса. Плательщик периода — ``payer_id``
(0001, ``SET NULL`` при удалении аккаунта). ``payments`` — журнал платежей: счёт создаётся
строкой ``pending`` (``couple_id``, ``plan``, ``period_days``, ``expires_at``; ``id`` — payload
счёта Telegram), зачисление переводит её в ``succeeded`` (``subscription_id`` оплаченного периода,
``provider_charge_id`` = ``telegram_payment_charge_id``, ``paid_at``), возврат — ``refunded``
(``refunded_at``). Личных данных в ``payments`` нет: ни id пользователя, ни Telegram ID, ни
текстов; ``provider_payment_charge_id`` (для Stars пуст) не хранится.

Новые колонки ``payments`` (все NULL — прежние строки и тесты остаются в силе):
``couple_id`` (FK ``couples`` CASCADE, индекс), ``plan``, ``period_days``, ``expires_at``,
``paid_at``, ``refunded_at``; ``subscription_id`` становится NULL-допустимым (счёт ещё без
периода). CHECK: ``couple_or_subscription`` — строка привязана к паре или к периоду;
``invoice_complete`` — ожидающий счёт без периода имеет пару, тариф, срок и срок действия;
``period_days`` 1…400; ``refunded_at`` только у ``refunded``; ``currency`` — три заглавные буквы.
``subscriptions``: уникальный частичный индекс ``uq_subscriptions_trial_per_couple``
(``couple_id`` WHERE ``plan = 'trial'``) — пробный период один раз на пару.

Прав на таблицы приложению не добавляется: у ``feelings_app`` по-прежнему только SELECT на
``subscriptions``/``payments`` под политиками 0001 (``subs_member`` — участники пары, в т.ч.
бывшие; ``payments_member`` — платежи видимых периодов). Ожидающие счета (без периода) напрямую
не видны никому — только функциям. Запись — только функциями ниже (SECURITY DEFINER,
``search_path = public, pg_temp``, ``REVOKE ALL … FROM PUBLIC``, ``EXECUTE`` только
``feelings_app``).

Функции пользователя (``app.user_id`` задан; нет, неизвестен или ``deleted_at`` — ``42501``):

1. ``app_subscription_status()`` — статус подписки текущей пары (ожидающей или активной) для
   экрана «Подписка» и ``GET /couple/subscription``: ``status`` (``none`` — периодов нет или
   пары нет; ``trial``/``active`` — период покрывает ``now()``, оплаченный главнее пробного;
   ``expired``; ``canceled`` — последний период возвращён), ``plan`` и ``period_end`` (доступ до
   конца непрерывной цепочки периодов; без доступа — последнего периода), ``trial_ends_at``,
   ``trial_used``, ``paid_by_me`` (плательщик последнего оплаченного периода — я; NULL — оплат
   не было), ``has_access`` — как ``app_has_access()``. Без блокировок.
2. ``app_has_access() RETURNS boolean`` — у пользователя есть доступ: активная пара с периодом,
   покрывающим ``now()``, или распавшаяся пара, где он был участником, у которой непрерывная
   цепочка периодов от ``now()`` доходит до **оплаченного** (ТЗ §11 п.6: при расторжении
   подписка прекращается в конце оплаченного периода; один пробный период доступа не даёт,
   оплаченный, начинающийся после пробного, — даёт и на остаток пробного). Кризисный протокол и помощь от доступа не зависят (§11 п.5) — это проверка
   API, не функции.
3. ``app_subscription_start_trial(p_days integer)`` → ``(subscription_id, period_end, created)``
   — пробный период активной пары, один раз на пару (индекс + блокировка пары). Отсчёт — от
   ``couples.activated_at`` (ТЗ §11 п.2 «с момента создания пары»: до принятия приглашения пары
   из двух нет, п.3), поэтому повторный или поздний вызов даёт тот же срок; уже истёкший пробный
   период тоже записывается (отметка «использован»). Повтор — та же строка, ``created = false``.
   ``FC001`` — нет активного членства или пара не ``active``; ``22023`` — ``p_days`` вне 1…30.
   Длина — из настроек (Д6: 7 дней, [уточняется] АР-50).
4. ``app_subscription_create_invoice(p_plan, p_amount, p_period_days, p_ttl)`` →
   ``(invoice_id, expires_at, reused)`` — счёт на оплату для ``createInvoiceLink``: строка
   ``payments`` ``pending``, ``currency = 'XTR'``, ``provider = 'telegram_stars'``. Цена и срок —
   из настроек сервера (Д6), не от клиента. Любой активный участник активной пары (§11 п.1).
   Действующий счёт той же пары с теми же условиями и не меньше половины срока действия
   переиспользуется (``reused``); счета пары, истёкшие больше суток назад и не оплаченные,
   удаляются. ``FC001`` — как выше; ``FC002`` — у пары уже 20 действующих счетов (API — 429);
   ``22023`` — тариф не ``month``/``year``, сумма вне 1…100000, срок вне 1…400 дней, срок действия
   вне (0, 1 день].

Функции бота (служебный контекст вебхука Telegram: ``user_scope(…, None)``, ``app.user_id``
пуст; задан — ``42501``, чтобы код сессии пользователя не мог зачислить или вернуть платёж):

5. ``app_bot_payment_precheck(p_invoice, p_telegram_id, p_amount, p_currency)`` → код для
   ответа на ``pre_checkout_query``: ``ok``, ``invoice_not_found``, ``invoice_used``,
   ``invoice_expired``, ``amount_mismatch``, ``couple_not_active``, ``not_member``. Только чтение.
6. ``app_bot_payment_credit(p_invoice, p_telegram_id, p_amount, p_currency, p_charge_id)`` →
   ``(result, payment_id, subscription_id, period_end)`` — зачисление по ``successful_payment``,
   идемпотентно по ``telegram_payment_charge_id`` (уникальный ``provider_charge_id``):
   ``credited``; ``duplicate`` — этот платёж уже зачислен (ничего не меняется, возвращаются его
   id); ``couple_ended`` — зачислено, но пара уже не активна (деньги получены — период
   записывается, бот предлагает возврат по политике АР-50); без зачисления — ``invoice_not_found``
   (пара удалена) и ``amount_mismatch``. Новый период начинается в
   ``greatest(now(), конец последнего действующего периода пары)`` — оплата во время пробного или
   оплаченного периода его не сокращает. Повторная оплата того же счёта другим платежом (гонка
   двух ``pre_checkout``) — новая строка ``payments`` и ещё один период. Плательщик — по
   ``p_telegram_id`` (неизвестен или удалён — ``payer_id`` NULL). ``22023`` — пустой или длиннее
   128 ``p_charge_id``, сумма ≤ 0.
7. ``app_bot_payment_refunded(p_charge_id)`` → ``(result, payment_id)`` — отметка возврата
   (после ``refundStarPayment`` или обновления ``refunded_payment``): ``refunded``,
   ``already_refunded``, ``not_found``. Платёж — ``refunded``, его период — ``canceled``;
   последующие периоды не сдвигаются (АР-50).
8. ``app_bot_payment_lookup(p_charge_id)`` → ``(payment_id, status, amount, currency,
   payer_telegram_id, paid_at)`` — данные для ``refundStarPayment`` (нужен Telegram ID
   плательщика); после удаления аккаунта плательщика ``payer_telegram_id`` NULL.

``audit_log`` (без текстов и сумм): ``subscription.trial`` (actor — вызвавший, объект — период),
``payment.credit`` (actor — плательщик или NULL, объект — платёж), ``payment.refund`` (actor
NULL, объект — платёж).

**Блокировки (АР-13, АР-38).** Порядок пользователь → пара → строки подписки/платежей.
Пользователь — ``FOR KEY SHARE`` (ждёт удаление аккаунта, совместима с остальными функциями
пары), пара — ``FOR NO KEY UPDATE`` у функций, меняющих периоды (пробный, зачисление,
возврат): они идут по очереди между собой и с выходом/удалением (``FOR UPDATE`` пары), но не
ждут конвейер (``FOR KEY SHARE`` пары). Счёт берёт пару ``FOR KEY SHARE``. Строки других
пользователей не блокируются. Проверка повтора ``provider_charge_id`` повторяется после
блокировки пары: два одновременных зачисления одного платежа дают ``credited`` и ``duplicate``.

**Распад пары и удаление аккаунта (4.5).** Выход (``app_leave_couple``) периоды не трогает:
партнёры сохраняют доступ к личному чату до конца оплаченного периода (§11 п.6), пробный
период распавшейся пары доступа не даёт; новая пара — новый пробный период (злоупотребление —
вопрос АР-50). ``app_delete_account`` не меняется: ``payer_id`` → NULL, у пары без участников
каскадом удаляются периоды, счета и платежи (АР-23 (а), на подтверждении оператора).

Downgrade удаляет функции, индекс, CHECK и колонки; не оплаченные счета (``subscription_id``
NULL) удаляются — иначе колонку нельзя вернуть в NOT NULL.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0017"
down_revision: str | Sequence[str] | None = "0016"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Конец непрерывной цепочки действующих (trial/active) периодов пары от p_at; NULL — p_at не покрыт.
# Не SECURITY DEFINER и без EXECUTE у ролей: вызывается только функциями ниже от имени владельца.
COVERED_UNTIL = r"""
CREATE FUNCTION subscription_covered_until(p_couple uuid, p_at timestamptz) RETURNS timestamptz
    LANGUAGE plpgsql STABLE SET search_path = public, pg_temp
    AS $f$
    DECLARE v_t timestamptz := p_at; v_next timestamptz;
    BEGIN
        LOOP
            SELECT max(s.period_end) INTO v_next FROM public.subscriptions s
             WHERE s.couple_id = p_couple AND s.status IN ('trial', 'active')
               AND s.period_start <= v_t AND s.period_end > v_t;
            EXIT WHEN v_next IS NULL;
            v_t := v_next;
        END LOOP;
        RETURN CASE WHEN v_t > p_at THEN v_t END;
    END $f$
"""

# Доступ пользователя (п.2 docstring). Внутренняя, как COVERED_UNTIL.
USER_HAS_ACCESS = r"""
CREATE FUNCTION subscription_user_has_access(p_user uuid) RETURNS boolean
    LANGUAGE sql STABLE SET search_path = public, pg_temp
    AS $f$
    SELECT EXISTS (
        SELECT 1
          FROM public.couple_members cm
          JOIN public.couples c ON c.id = cm.couple_id
         CROSS JOIN LATERAL (SELECT public.subscription_covered_until(c.id, now()) AS until) cov
         WHERE cm.user_id = p_user AND cov.until IS NOT NULL
           AND ((cm.active AND c.status = 'active')
                -- Распавшаяся пара: цепочка от now() должна дойти до оплаченного периода —
                -- пробный сам по себе доступа не даёт, оплаченный после пробного — даёт.
                OR (c.status = 'dissolved'
                    AND EXISTS (SELECT 1 FROM public.subscriptions s
                                 WHERE s.couple_id = c.id AND s.status = 'active'
                                   AND s.plan <> 'trial' AND s.period_end > now()
                                   AND s.period_start < cov.until))))
    $f$
"""

# Пользователь известен, существует и не удалён (строку не блокирует).
_USER_KNOWN = """
        IF v_uid IS NULL THEN RAISE EXCEPTION 'no user' USING ERRCODE = '42501'; END IF;
        PERFORM 1 FROM public.users u WHERE u.id = v_uid AND u.deleted_at IS NULL;
        IF NOT FOUND THEN RAISE EXCEPTION 'no user' USING ERRCODE = '42501'; END IF;
"""

# То же с блокировкой строки пользователя FOR KEY SHARE (ждёт удаление аккаунта).
_USER_KEY_SHARE = """
        IF v_uid IS NULL THEN RAISE EXCEPTION 'no user' USING ERRCODE = '42501'; END IF;
        PERFORM 1 FROM public.users u WHERE u.id = v_uid AND u.deleted_at IS NULL FOR KEY SHARE;
        IF NOT FOUND THEN RAISE EXCEPTION 'no user' USING ERRCODE = '42501'; END IF;
"""

# Служебный контекст бота: app.user_id пуст.
_SERVICE_ONLY = """
        IF public.app_uid() IS NOT NULL THEN
            RAISE EXCEPTION 'service context only' USING ERRCODE = '42501';
        END IF;
"""

STATUS = r"""
CREATE FUNCTION app_subscription_status()
    RETURNS TABLE (couple_id uuid, couple_status couple_status, status text, plan varchar,
                   period_end timestamptz, trial_ends_at timestamptz, trial_used boolean,
                   paid_by_me boolean, has_access boolean)
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE
        v_uid uuid := public.app_uid(); v_couple uuid; v_cstatus public.couple_status;
        v_until timestamptz; v_plan varchar; v_status text; v_end timestamptz;
        v_last record; v_payer uuid; v_paid boolean;
    BEGIN
--USER_KNOWN--
        has_access := public.subscription_user_has_access(v_uid);
        SELECT cm.couple_id, c.status INTO v_couple, v_cstatus
          FROM public.couple_members cm JOIN public.couples c ON c.id = cm.couple_id
         WHERE cm.user_id = v_uid AND cm.active;
        IF v_couple IS NULL THEN
            couple_id := NULL; couple_status := NULL; status := 'none'; plan := NULL;
            period_end := NULL; trial_ends_at := NULL; trial_used := false; paid_by_me := NULL;
            RETURN NEXT; RETURN;
        END IF;
        v_until := public.subscription_covered_until(v_couple, now());
        IF v_until IS NOT NULL THEN
            SELECT s.plan INTO v_plan FROM public.subscriptions s
             WHERE s.couple_id = v_couple AND s.status IN ('trial', 'active')
               AND s.period_start <= now() AND s.period_end > now()
             ORDER BY (s.plan = 'trial'), s.period_end DESC LIMIT 1;
            v_status := CASE WHEN v_plan = 'trial' THEN 'trial' ELSE 'active' END;
            v_end := v_until;
        ELSE
            SELECT s.status, s.plan, s.period_end INTO v_last FROM public.subscriptions s
             WHERE s.couple_id = v_couple ORDER BY s.period_end DESC, s.created_at DESC LIMIT 1;
            IF NOT FOUND THEN
                v_status := 'none'; v_plan := NULL; v_end := NULL;
            ELSE
                v_status := CASE WHEN v_last.status = 'canceled' THEN 'canceled' ELSE 'expired' END;
                v_plan := v_last.plan; v_end := v_last.period_end;
            END IF;
        END IF;
        SELECT s.payer_id, true INTO v_payer, v_paid FROM public.subscriptions s
         WHERE s.couple_id = v_couple AND s.plan <> 'trial' AND s.status = 'active'
         ORDER BY s.period_end DESC, s.created_at DESC LIMIT 1;
        couple_id := v_couple; couple_status := v_cstatus; status := v_status; plan := v_plan;
        period_end := v_end;
        SELECT s.period_end INTO trial_ends_at FROM public.subscriptions s
         WHERE s.couple_id = v_couple AND s.plan = 'trial';
        trial_used := trial_ends_at IS NOT NULL;
        paid_by_me := CASE WHEN v_paid THEN v_payer IS NOT DISTINCT FROM v_uid END;
        RETURN NEXT;
    END $f$
"""

HAS_ACCESS = r"""
CREATE FUNCTION app_has_access() RETURNS boolean
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE v_uid uuid := public.app_uid();
    BEGIN
--USER_KNOWN--
        RETURN public.subscription_user_has_access(v_uid);
    END $f$
"""

START_TRIAL = r"""
CREATE FUNCTION app_subscription_start_trial(p_days integer)
    RETURNS TABLE (subscription_id uuid, period_end timestamptz, created boolean)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE
        v_uid uuid := public.app_uid(); v_couple uuid; v_cstatus public.couple_status;
        v_activated timestamptz; v_id uuid; v_end timestamptz;
    BEGIN
        IF p_days IS NULL OR p_days < 1 OR p_days > 30 THEN
            RAISE EXCEPTION 'bad trial length' USING ERRCODE = '22023';
        END IF;
--USER_KEY_SHARE--
        SELECT cm.couple_id INTO v_couple FROM public.couple_members cm
         WHERE cm.user_id = v_uid AND cm.active;
        IF v_couple IS NULL THEN
            RAISE EXCEPTION 'no active couple' USING ERRCODE = 'FC001';
        END IF;
        SELECT c.status, c.activated_at INTO v_cstatus, v_activated FROM public.couples c
         WHERE c.id = v_couple FOR NO KEY UPDATE;
        IF v_cstatus IS DISTINCT FROM 'active' OR v_activated IS NULL THEN
            RAISE EXCEPTION 'couple not active' USING ERRCODE = 'FC001';
        END IF;
        SELECT s.id, s.period_end INTO v_id, v_end FROM public.subscriptions s
         WHERE s.couple_id = v_couple AND s.plan = 'trial';
        IF FOUND THEN
            subscription_id := v_id; period_end := v_end; created := false;
            RETURN NEXT; RETURN;
        END IF;
        INSERT INTO public.subscriptions (couple_id, payer_id, plan, status, period_start,
                                          period_end)
        VALUES (v_couple, NULL, 'trial', 'trial', v_activated,
                v_activated + make_interval(days => p_days))
        RETURNING id, subscriptions.period_end INTO v_id, v_end;
        INSERT INTO public.audit_log (actor_id, action, object_type, object_id)
        VALUES (v_uid, 'subscription.trial', 'subscription', v_id);
        subscription_id := v_id; period_end := v_end; created := true;
        RETURN NEXT;
    END $f$
"""

CREATE_INVOICE = r"""
CREATE FUNCTION app_subscription_create_invoice(p_plan varchar, p_amount integer,
                                                p_period_days integer, p_ttl interval)
    RETURNS TABLE (invoice_id uuid, expires_at timestamptz, reused boolean)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE
        v_uid uuid := public.app_uid(); v_couple uuid; v_cstatus public.couple_status;
        v_id uuid; v_exp timestamptz; v_open integer;
    BEGIN
        IF p_plan IS NULL OR p_plan NOT IN ('month', 'year')
           OR p_amount IS NULL OR p_amount < 1 OR p_amount > 100000
           OR p_period_days IS NULL OR p_period_days < 1 OR p_period_days > 400
           OR p_ttl IS NULL OR p_ttl <= interval '0' OR p_ttl > interval '1 day' THEN
            RAISE EXCEPTION 'bad invoice parameters' USING ERRCODE = '22023';
        END IF;
--USER_KEY_SHARE--
        SELECT cm.couple_id INTO v_couple FROM public.couple_members cm
         WHERE cm.user_id = v_uid AND cm.active;
        IF v_couple IS NULL THEN
            RAISE EXCEPTION 'no active couple' USING ERRCODE = 'FC001';
        END IF;
        SELECT c.status INTO v_cstatus FROM public.couples c WHERE c.id = v_couple FOR KEY SHARE;
        IF v_cstatus IS DISTINCT FROM 'active' THEN
            RAISE EXCEPTION 'couple not active' USING ERRCODE = 'FC001';
        END IF;
        DELETE FROM public.payments p
         WHERE p.couple_id = v_couple AND p.status = 'pending' AND p.subscription_id IS NULL
           AND p.expires_at < now() - interval '1 day';
        SELECT p.id, p.expires_at INTO v_id, v_exp FROM public.payments p
         WHERE p.couple_id = v_couple AND p.status = 'pending' AND p.subscription_id IS NULL
           AND p.plan = p_plan AND p.amount = p_amount AND p.period_days = p_period_days
           AND p.currency = 'XTR' AND p.expires_at > now() + p_ttl / 2
         ORDER BY p.expires_at DESC LIMIT 1;
        IF FOUND THEN
            invoice_id := v_id; expires_at := v_exp; reused := true;
            RETURN NEXT; RETURN;
        END IF;
        SELECT count(*) INTO v_open FROM public.payments p
         WHERE p.couple_id = v_couple AND p.status = 'pending' AND p.subscription_id IS NULL
           AND p.expires_at > now();
        IF v_open >= 20 THEN
            RAISE EXCEPTION 'too many open invoices' USING ERRCODE = 'FC002';
        END IF;
        INSERT INTO public.payments (couple_id, amount, currency, provider, status, plan,
                                     period_days, expires_at)
        VALUES (v_couple, p_amount, 'XTR', 'telegram_stars', 'pending', p_plan, p_period_days,
                now() + p_ttl)
        RETURNING id, payments.expires_at INTO v_id, v_exp;
        invoice_id := v_id; expires_at := v_exp; reused := false;
        RETURN NEXT;
    END $f$
"""

PRECHECK = r"""
CREATE FUNCTION app_bot_payment_precheck(p_invoice uuid, p_telegram_id bigint, p_amount integer,
                                         p_currency varchar)
    RETURNS varchar
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE p record; v_cstatus public.couple_status;
    BEGIN
--SERVICE_ONLY--
        SELECT x.status, x.subscription_id, x.couple_id, x.amount, x.currency, x.expires_at
          INTO p FROM public.payments x
         WHERE x.id = p_invoice AND x.couple_id IS NOT NULL AND x.plan IS NOT NULL;
        IF NOT FOUND THEN RETURN 'invoice_not_found'; END IF;
        IF p.status <> 'pending' OR p.subscription_id IS NOT NULL THEN RETURN 'invoice_used'; END IF;
        IF p.expires_at IS NULL OR p.expires_at <= now() THEN RETURN 'invoice_expired'; END IF;
        IF p_amount IS DISTINCT FROM p.amount OR p_currency IS DISTINCT FROM p.currency THEN
            RETURN 'amount_mismatch';
        END IF;
        SELECT c.status INTO v_cstatus FROM public.couples c WHERE c.id = p.couple_id;
        IF v_cstatus IS DISTINCT FROM 'active' THEN RETURN 'couple_not_active'; END IF;
        IF NOT EXISTS (SELECT 1 FROM public.users u JOIN public.couple_members cm
                         ON cm.user_id = u.id AND cm.couple_id = p.couple_id AND cm.active
                        WHERE u.telegram_id = p_telegram_id AND u.deleted_at IS NULL) THEN
            RETURN 'not_member';
        END IF;
        RETURN 'ok';
    END $f$
"""

CREDIT = r"""
CREATE FUNCTION app_bot_payment_credit(p_invoice uuid, p_telegram_id bigint, p_amount integer,
                                       p_currency varchar, p_charge_id varchar)
    RETURNS TABLE (result varchar, payment_id uuid, subscription_id uuid,
                   period_end timestamptz)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE
        v_payer uuid; inv record; v_cstatus public.couple_status; v_istatus public.payment_status;
        v_start timestamptz; v_sub uuid; v_end timestamptz; v_pay uuid;
    BEGIN
--SERVICE_ONLY--
        IF p_charge_id IS NULL OR p_charge_id = '' OR length(p_charge_id) > 128
           OR p_amount IS NULL OR p_amount < 1 THEN
            RAISE EXCEPTION 'bad payment' USING ERRCODE = '22023';
        END IF;
        -- Повтор того же successful_payment (повторная доставка обновления) — без блокировок.
        SELECT x.id, x.subscription_id, s.period_end INTO v_pay, v_sub, v_end
          FROM public.payments x LEFT JOIN public.subscriptions s ON s.id = x.subscription_id
         WHERE x.provider_charge_id = p_charge_id;
        IF FOUND THEN
            result := 'duplicate'; payment_id := v_pay; subscription_id := v_sub;
            period_end := v_end; RETURN NEXT; RETURN;
        END IF;
        -- Порядок блокировок: пользователь → пара → платёж (АР-13, АР-38).
        SELECT u.id INTO v_payer FROM public.users u
         WHERE u.telegram_id = p_telegram_id AND u.deleted_at IS NULL FOR KEY SHARE;
        SELECT x.couple_id, x.plan, x.period_days, x.amount, x.currency INTO inv
          FROM public.payments x
         WHERE x.id = p_invoice AND x.couple_id IS NOT NULL AND x.plan IS NOT NULL;
        IF NOT FOUND THEN
            result := 'invoice_not_found'; RETURN NEXT; RETURN;
        END IF;
        IF p_amount IS DISTINCT FROM inv.amount OR p_currency IS DISTINCT FROM inv.currency THEN
            result := 'amount_mismatch'; RETURN NEXT; RETURN;
        END IF;
        SELECT c.status INTO v_cstatus FROM public.couples c
         WHERE c.id = inv.couple_id FOR NO KEY UPDATE;
        IF NOT FOUND THEN
            result := 'invoice_not_found'; RETURN NEXT; RETURN;
        END IF;
        -- После ожидания пары: одновременное зачисление того же платежа уже прошло.
        SELECT x.id, x.subscription_id, s.period_end INTO v_pay, v_sub, v_end
          FROM public.payments x LEFT JOIN public.subscriptions s ON s.id = x.subscription_id
         WHERE x.provider_charge_id = p_charge_id;
        IF FOUND THEN
            result := 'duplicate'; payment_id := v_pay; subscription_id := v_sub;
            period_end := v_end; RETURN NEXT; RETURN;
        END IF;
        SELECT x.status INTO v_istatus FROM public.payments x WHERE x.id = p_invoice FOR UPDATE;
        IF NOT FOUND THEN
            result := 'invoice_not_found'; RETURN NEXT; RETURN;
        END IF;
        SELECT greatest(now(), max(s.period_end)) INTO v_start FROM public.subscriptions s
         WHERE s.couple_id = inv.couple_id AND s.status IN ('trial', 'active')
           AND s.period_end > now();
        v_start := coalesce(v_start, now());
        INSERT INTO public.subscriptions (couple_id, payer_id, plan, status, period_start,
                                          period_end)
        VALUES (inv.couple_id, v_payer, inv.plan, 'active', v_start,
                v_start + make_interval(days => inv.period_days))
        RETURNING id, subscriptions.period_end INTO v_sub, v_end;
        IF v_istatus = 'pending' THEN
            UPDATE public.payments x SET status = 'succeeded', subscription_id = v_sub,
                   provider_charge_id = p_charge_id, paid_at = now()
             WHERE x.id = p_invoice;
            v_pay := p_invoice;
        ELSE
            INSERT INTO public.payments (subscription_id, couple_id, amount, currency, provider,
                                         provider_charge_id, status, plan, period_days, paid_at)
            VALUES (v_sub, inv.couple_id, inv.amount, inv.currency, 'telegram_stars',
                    p_charge_id, 'succeeded', inv.plan, inv.period_days, now())
            RETURNING id INTO v_pay;
        END IF;
        INSERT INTO public.audit_log (actor_id, action, object_type, object_id)
        VALUES (v_payer, 'payment.credit', 'payment', v_pay);
        result := CASE WHEN v_cstatus = 'active' THEN 'credited' ELSE 'couple_ended' END;
        payment_id := v_pay; subscription_id := v_sub; period_end := v_end;
        RETURN NEXT;
    END $f$
"""

REFUNDED = r"""
CREATE FUNCTION app_bot_payment_refunded(p_charge_id varchar)
    RETURNS TABLE (result varchar, payment_id uuid)
    LANGUAGE plpgsql VOLATILE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    DECLARE v_pay uuid; v_sub uuid; v_couple uuid; v_status public.payment_status;
    BEGIN
--SERVICE_ONLY--
        IF p_charge_id IS NULL OR p_charge_id = '' OR length(p_charge_id) > 128 THEN
            RAISE EXCEPTION 'bad charge id' USING ERRCODE = '22023';
        END IF;
        SELECT x.id, x.subscription_id, s.couple_id INTO v_pay, v_sub, v_couple
          FROM public.payments x JOIN public.subscriptions s ON s.id = x.subscription_id
         WHERE x.provider_charge_id = p_charge_id;
        IF NOT FOUND THEN
            result := 'not_found'; RETURN NEXT; RETURN;
        END IF;
        PERFORM 1 FROM public.couples c WHERE c.id = v_couple FOR NO KEY UPDATE;
        SELECT x.status INTO v_status FROM public.payments x WHERE x.id = v_pay FOR UPDATE;
        IF NOT FOUND THEN
            result := 'not_found'; RETURN NEXT; RETURN;
        END IF;
        IF v_status = 'refunded' THEN
            result := 'already_refunded'; payment_id := v_pay; RETURN NEXT; RETURN;
        END IF;
        UPDATE public.payments x SET status = 'refunded', refunded_at = now() WHERE x.id = v_pay;
        UPDATE public.subscriptions s SET status = 'canceled' WHERE s.id = v_sub;
        INSERT INTO public.audit_log (actor_id, action, object_type, object_id)
        VALUES (NULL, 'payment.refund', 'payment', v_pay);
        result := 'refunded'; payment_id := v_pay;
        RETURN NEXT;
    END $f$
"""

LOOKUP = r"""
CREATE FUNCTION app_bot_payment_lookup(p_charge_id varchar)
    RETURNS TABLE (payment_id uuid, status payment_status, amount integer, currency varchar,
                   payer_telegram_id bigint, paid_at timestamptz)
    LANGUAGE plpgsql STABLE SECURITY DEFINER SET search_path = public, pg_temp
    AS $f$
    BEGIN
--SERVICE_ONLY--
        RETURN QUERY
        SELECT x.id, x.status, x.amount, x.currency, u.telegram_id, x.paid_at
          FROM public.payments x
          JOIN public.subscriptions s ON s.id = x.subscription_id
          LEFT JOIN public.users u ON u.id = s.payer_id AND u.deleted_at IS NULL
         WHERE x.provider_charge_id = p_charge_id;
    END $f$
"""

INTERNAL_SIGNATURES = (
    "subscription_covered_until(uuid, timestamp with time zone)",
    "subscription_user_has_access(uuid)",
)
APP_SIGNATURES = (
    "app_subscription_status()",
    "app_has_access()",
    "app_subscription_start_trial(integer)",
    "app_subscription_create_invoice(character varying, integer, integer, interval)",
    "app_bot_payment_precheck(uuid, bigint, integer, character varying)",
    "app_bot_payment_credit(uuid, bigint, integer, character varying, character varying)",
    "app_bot_payment_refunded(character varying)",
    "app_bot_payment_lookup(character varying)",
)


def _expand(sql: str) -> str:
    return (
        sql.replace("--USER_KNOWN--", _USER_KNOWN)
        .replace("--USER_KEY_SHARE--", _USER_KEY_SHARE)
        .replace("--SERVICE_ONLY--", _SERVICE_ONLY)
    )


def upgrade() -> None:
    op.alter_column("payments", "subscription_id", existing_type=sa.UUID(), nullable=True)
    op.add_column("payments", sa.Column("couple_id", sa.UUID(), nullable=True))
    op.add_column("payments", sa.Column("plan", sa.String(length=32), nullable=True))
    op.add_column("payments", sa.Column("period_days", sa.Integer(), nullable=True))
    op.add_column("payments", sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("payments", sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("payments", sa.Column("refunded_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        op.f("fk_payments_couple_id_couples"),
        "payments",
        "couples",
        ["couple_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index(op.f("ix_payments_couple_id"), "payments", ["couple_id"], unique=False)
    op.create_check_constraint(
        op.f("ck_payments_couple_or_subscription"),
        "payments",
        "couple_id IS NOT NULL OR subscription_id IS NOT NULL",
    )
    op.create_check_constraint(
        op.f("ck_payments_invoice_complete"),
        "payments",
        "status <> 'pending' OR subscription_id IS NOT NULL OR (couple_id IS NOT NULL "
        "AND plan IS NOT NULL AND period_days IS NOT NULL AND expires_at IS NOT NULL)",
    )
    op.create_check_constraint(
        op.f("ck_payments_period_days_range"),
        "payments",
        "period_days IS NULL OR period_days BETWEEN 1 AND 400",
    )
    op.create_check_constraint(
        op.f("ck_payments_refunded_at_status"),
        "payments",
        "refunded_at IS NULL OR status = 'refunded'",
    )
    op.create_check_constraint(
        op.f("ck_payments_currency_code"), "payments", "currency ~ '^[A-Z]{3}$'"
    )
    op.create_index(
        "uq_subscriptions_trial_per_couple",
        "subscriptions",
        ["couple_id"],
        unique=True,
        postgresql_where=sa.text("plan = 'trial'"),
    )
    # По одной команде: asyncpg не выполняет несколько команд в одном запросе.
    for stmt in (
        COVERED_UNTIL,
        USER_HAS_ACCESS,
        STATUS,
        HAS_ACCESS,
        START_TRIAL,
        CREATE_INVOICE,
        PRECHECK,
        CREDIT,
        REFUNDED,
        LOOKUP,
    ):
        op.execute(_expand(stmt))
    for sig in INTERNAL_SIGNATURES + APP_SIGNATURES:
        op.execute(f"REVOKE ALL ON FUNCTION {sig} FROM PUBLIC")
    for sig in APP_SIGNATURES:
        op.execute(f"GRANT EXECUTE ON FUNCTION {sig} TO feelings_app")


def downgrade() -> None:
    for sig in APP_SIGNATURES + tuple(reversed(INTERNAL_SIGNATURES)):
        op.execute(f"DROP FUNCTION IF EXISTS {sig}")
    op.drop_index(
        "uq_subscriptions_trial_per_couple",
        table_name="subscriptions",
        postgresql_where=sa.text("plan = 'trial'"),
    )
    # Не оплаченные счета без периода — иначе subscription_id не вернуть в NOT NULL.
    op.execute("DELETE FROM payments WHERE subscription_id IS NULL")
    for name in (
        "ck_payments_currency_code",
        "ck_payments_refunded_at_status",
        "ck_payments_period_days_range",
        "ck_payments_invoice_complete",
        "ck_payments_couple_or_subscription",
    ):
        op.drop_constraint(op.f(name), "payments", type_="check")
    op.drop_index(op.f("ix_payments_couple_id"), table_name="payments")
    op.drop_constraint(op.f("fk_payments_couple_id_couples"), "payments", type_="foreignkey")
    for col in ("refunded_at", "paid_at", "expires_at", "period_days", "plan", "couple_id"):
        op.drop_column("payments", col)
    op.alter_column("payments", "subscription_id", existing_type=sa.UUID(), nullable=False)
